# Command line

> Reference · [Documentation](../../README.md#documentation)

```
python -m gitops_check --repo DIR --base REV --head REV [--server URL] [options]
```

| Option | Meaning |
|---|---|
| `--repo DIR` | A clone holding both revisions (default `.`) |
| `--base REV`, `--head REV` | The target branch's commit (before) and the pull request's (after): anything `git rev-parse` resolves |
| `--server URL` (or `$ARGOCD_SERVER`) | Render with this render-only Argo CD (token: `$ARGOCD_TOKEN`). Without it, `argocd-render` renders in process (it must be on `PATH`, with Argo CD's `kustomize`, `helm`, `git-verify-wrapper.sh` and the `ksops` stand-in: the image has them) |
| `--config PATH` | The configuration's path in the repository (default `.gitops-check.yaml`) |
| `--run-id ID` | Prefix of the Applications the run creates (default `gc-<head commit>`; the pipeline passes its PipelineRun's name) |
| `--no-schemas` | Skip kubeconform |
| `--report FILE` | Also write the whole report there |
| `--forgejo-url`, `--forgejo-repo OWNER/NAME`, `--pr N`, `--git-credentials FILE`, `--cafile FILE` | Post the report on that pull request, with the token found in a `.git-credentials` file (as Pipelines-as-Code writes it) and the CA of Forgejo's certificate |

Environment: `KUBECONFORM_CACHE` (schema cache, default `/tmp/kubeconform-cache`);
`ARGOCD_LOG_LEVEL` (argocd-render's log, default `warn`).
Progress goes to stderr, the report to stdout.

**Exit status**: `0` passed; `1` errors (the merge is blocked); `2` the check
itself could not run (an unknown revision, no configuration, `argocd-render`
missing or dying, the preview unreachable or refusing the token). The commit status fails on 1 and 2.
