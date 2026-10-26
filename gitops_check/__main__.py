"""gitops-check: render a pull request of a gitops repository before and after,
with Argo CD's own code, check what comes out, and report the differences.

  python -m gitops_check --repo . --base origin/main --head HEAD

renders in process with argocd-render; with --server (token: $ARGOCD_TOKEN),
a render-only Argo CD renders instead:

  python -m gitops_check ... --server http://argocd-preview-server.argocd-preview.svc

Exit status: 0 no error, 1 errors (the merge is blocked), 2 the check itself
could not run (unknown revision, no configuration, argocd-render missing, the
preview unreachable or refusing the token): the status fails as well, with the cause in the log.
"""
from __future__ import annotations

import argparse
import datetime
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from . import checks, config, diff, report
from .argocd import ArgoCD, ArgoError
from .forgejo import Forgejo, token_from_git_credentials
from .local import LocalArgo
from .render import RUN_LABEL, Renderer

# A run takes a minute or two: an Application of another run older than this
# was left by a run that died before its cleanup.
STALE_AFTER = datetime.timedelta(hours=2)


def git(repo: Path, *args: str) -> str:
    """Run git in repo and return its stripped stdout; raises CalledProcessError if it fails."""
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()


def log(msg: str) -> None:
    """Progress on stderr (stdout carries the report)."""
    print(f"==> {msg}", file=sys.stderr, flush=True)


def remove_stale(argo: ArgoCD, run: str) -> None:
    """Delete the preview's Applications that other runs left behind, older than STALE_AFTER.

    An Application whose run label starts with run is this run's and kept; one
    without a creationTimestamp counts as new. A failed deletion is ignored
    (the next run tries again); listing errors propagate as ArgoError.
    """
    now = datetime.datetime.now(datetime.UTC)
    for a in argo.list_apps(RUN_LABEL):
        md = a.get("metadata", {})
        created = datetime.datetime.fromisoformat(md.get("creationTimestamp", now.isoformat()))
        if not md.get("labels", {}).get(RUN_LABEL, "").startswith(run) and now - created > STALE_AFTER:
            try:
                argo.delete_app(md["name"])
                log(f"removed stale {md['name']}")
            except ArgoError:
                pass


def main(argv: list[str] | None = None) -> int:
    """The command line (see the module docstring); returns the exit status.

    1. Resolve --base and --head to commits and check both out as worktrees.
    2. Read the configuration at head; an error if head doesn't contain base (outdated pull request).
    3. Delete stale Applications of dead runs (remove_stale).
    4. Render before (entry points that exist at base) and after (all of them).
    5. Run the checks on 'after' and diff the two renderings.
    6. Delete this run's Applications and the worktrees, whatever happened.
    7. Print the report; write it to --report and post it on the pull request when asked.
    """
    p = argparse.ArgumentParser(prog="gitops-check", description=__doc__.split("\n\n")[0])
    p.add_argument("--repo", type=Path, default=Path("."), help="clone holding both revisions")
    p.add_argument("--base", required=True, help="target branch's commit (before)")
    p.add_argument("--head", required=True, help="pull request's commit (after)")
    p.add_argument("--config", default=".gitops-check.yaml", help="path in the repository (read at --head)")
    p.add_argument("--server", default=os.environ.get("ARGOCD_SERVER", ""),
                   help="render-only Argo CD URL (default: render in process with argocd-render)")
    p.add_argument("--run-id", default=None, help="prefix of the Applications this run creates")
    p.add_argument("--no-schemas", action="store_true", help="skip kubeconform")
    p.add_argument("--report", type=Path, help="also write the report to this file")
    p.add_argument("--forgejo-url", help="post the report on the pull request (with --forgejo-repo, --pr)")
    p.add_argument("--forgejo-repo", help="owner/name")
    p.add_argument("--pr", type=int)
    p.add_argument("--git-credentials", type=Path, help="file holding the Forgejo token (Pipelines-as-Code's)")
    p.add_argument("--cafile", help="CA for Forgejo's certificate")
    a = p.parse_args(argv)

    token = os.environ.get("ARGOCD_TOKEN", "")
    if a.server and not token:
        p.error("--server (or $ARGOCD_SERVER) needs $ARGOCD_TOKEN")
    repo = a.repo.resolve()
    try:
        base, head = git(repo, "rev-parse", f"{a.base}^{{commit}}"), git(repo, "rev-parse", f"{a.head}^{{commit}}")
    except subprocess.CalledProcessError as e:
        print(f"ERROR: {e.stderr.strip()}", file=sys.stderr)
        return 2
    # Prefix of every Application this run creates in the preview; the
    # pipeline passes its PipelineRun's name, so parallel runs never collide.
    run = a.run_id or f"gc-{head[:7]}"
    argo: ArgoCD | LocalArgo | None = None
    errors: list[str] = []
    warnings: list[str] = []

    # Both revisions are checked out as detached worktrees of repo, so that the
    # local checks read files at each revision; they are removed in the finally,
    # as are the Applications created in the preview.
    with tempfile.TemporaryDirectory(prefix="gitops-check-") as tmp:
        trees = {"before": Path(tmp) / "before", "after": Path(tmp) / "after"}
        git(repo, "worktree", "add", "--detach", "-q", str(trees["before"]), base)
        git(repo, "worktree", "add", "--detach", "-q", str(trees["after"]), head)
        renderers: list[Renderer] = []
        try:
            try:
                cfg = config.load(trees["after"] / a.config)
            except config.ConfigError as e:
                print(f"ERROR: {e}", file=sys.stderr)
                return 2
            argo = ArgoCD(a.server, token) if a.server else LocalArgo(cfg, repo)
            # 'after' is the merge result only when the target's commit is in head.
            if subprocess.run(["git", "-C", str(repo), "merge-base", "--is-ancestor", base, head],
                              check=False).returncode:
                errors.append(f"the pull request is not up to date with its target ({base[:10]}): "
                              f"update the branch, so that 'after' is what the merge gives")
            try:
                remove_stale(argo, run)
            except ArgoError as e:
                warnings.append(f"stale Applications not checked: {e}")

            log(f"rendering before ({base[:10]})")
            rb = Renderer(argo, cfg, f"{run}-b", base)
            renderers.append(rb)
            # The configuration is head's: entry points the pull request adds don't exist before.
            before = rb.render([e for e in cfg.entrypoints if (trees["before"] / e.path).is_dir()])
            log(f"rendering after ({head[:10]})")
            ra = Renderer(argo, cfg, f"{run}-a", head)
            renderers.append(ra)
            after = ra.render()
            log(f"rendered {len(before.apps)} and {len(after.apps)} Applications")

            broken_before = sum(1 for x in before.apps.values() if x.error) + len(before.errors)
            if broken_before:
                warnings.append(f"the target branch itself has {broken_before} render error(s)")
            changed = git(repo, "diff", "--name-only", base, head).splitlines()
            results = [checks.render_errors(after), checks.duplicates(after),
                       checks.plaintext_secrets(trees["after"], cfg),
                       checks.unreached(after, cfg, trees["after"],
                                        checks.git_generator_globs(cfg, after.appsets)),
                       checks.generated_edits(trees["after"], changed),
                       checks.projects(after, cfg)]
            if not a.no_schemas:
                log("validating schemas")
                results.append(checks.schemas(after))
            for e, w in results:
                errors += e
                warnings += w
            diffs = diff.compare(before, after)
        except ArgoError as e:
            print(f"ERROR: {'render-only Argo CD' if a.server else 'argocd-render'}: {e}", file=sys.stderr)
            return 2
        finally:
            for r in renderers:
                for f in r.cleanup():
                    print(f"WARNING: not removed: {f}", file=sys.stderr)
            if isinstance(argo, LocalArgo):
                argo.close()
            for t in trees.values():
                subprocess.run(["git", "-C", str(repo), "worktree", "remove", "--force", str(t)],
                               capture_output=True, check=False)

    args = {"errors": errors, "warnings": warnings, "diffs": diffs, "base": base, "head": head,
            "counts": (len(before.apps), len(after.apps))}
    # The log and --report get the whole report; the pull request comment is cut to fit.
    full = report.markdown(**args, limit=None)
    print(full)
    if a.report:
        a.report.write_text(full)
    if a.forgejo_url and a.forgejo_repo and a.pr and a.git_credentials:
        try:
            Forgejo(a.forgejo_url, a.forgejo_repo, token_from_git_credentials(a.git_credentials),
                    a.cafile).upsert_comment(a.pr, report.markdown(**args))
            log(f"report posted on pull request #{a.pr}")
        except (OSError, ValueError) as e:
            print(f"WARNING: report not posted: {e}", file=sys.stderr)
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
