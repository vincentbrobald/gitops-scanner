"""Render a revision of the repository the way Argo CD would.

The rendering itself is Argo CD's: nothing here templates Helm charts or
builds kustomizations. This module decides *what* to submit to the
render-only Argo CD (argocd-preview), in which form, and in which order.

The walk (Renderer.render), per entry point of .gitops-check.yaml:

    queue = [stand-in Application for the entry point's directory]
    while queue:
        app = queue.pop(0)                    # breadth first
        skip it if its key (namespace/name) was rendered already
        skip it (no render) if its sources are all on external repositories
        rewrite it, submit it, read back its manifests
        Application in the manifests    -> queue it (a child of app)
        ApplicationSet in the manifests -> generate it once, queue its Applications

So exactly what Argo CD would reach from the entry points is rendered, and
nothing else: an overlay nobody points at is never looked at (the
"unreached directories" check reports it instead).

What is submitted is rewritten so that it renders and can never deploy:
- metadata: a run-specific name in the preview's namespace (the original name
  is kept as a label, the key in App.name, for the report);
- no syncPolicy, operation, status or finalizers (the preview has no
  controller anyway: a cascade finalizer would never be removed);
- sources and git generators on this repository that track a branch of
  trackedRevisions are pinned to the commit under test, so before and after
  really render two different commits (other repositories, tags and charts
  are left as written);
- project 'default' (the preview has no other).
"""
from __future__ import annotations

import copy
import dataclasses
import hashlib

from .argocd import ArgoCD, ArgoError
from .config import Config, Entrypoint, same_repo

RUN_LABEL = "gitops-check/run"        # on every submitted Application: <run>-b / <run>-a
ORIGIN_LABEL = "gitops-check/app"     # the Application's name in the repository
HELM_HOOK = "helm.sh/hook"
# Original names already rendered are skipped, which ends the usual cycles
# (an app-of-apps listing itself); this bound ends any other.
MAX_APPS = 500


def app_key(app: dict, home: tuple[str, ...] = ("argocd",)) -> str:
    """An Application's identity: its name, prefixed with its namespace
    unless that is a home namespace (argocd, where the platform's
    Applications live, and the preview's, where an ApplicationSet's
    Applications come out), so that Applications of the same name in other
    namespaces (each team's Argo CD has a "root") don't hide one another."""
    md = app.get("metadata") or {}
    ns, name = md.get("namespace") or "argocd", md.get("name", "")
    return name if ns in home else f"{ns}/{name}"


@dataclasses.dataclass
class App:
    """One Application as rendered, identified by its key (app_key: the
    name in the repository, which is what before and after have in common)."""
    name: str                        # key: [namespace/]name
    entrypoint: str                  # the entry point whose walk reached it
    parent: str | None               # original name of the Application that rendered it; None: a stand-in
    spec: dict                       # as submitted (rewritten): its sources tell where it came from
    resources: list[dict] = dataclasses.field(default_factory=list)   # what Argo CD rendered
    error: str | None = None         # Argo CD's error when it couldn't render; resources is empty then
    external: bool = False           # sources all on external repositories: not rendered
    project: str = "default"         # as written (spec is submitted with 'default')


@dataclasses.dataclass
class Rendering:
    """Everything one revision renders to: the input of the checks and the diff."""
    revision: str
    apps: dict[str, App] = dataclasses.field(default_factory=dict)
    errors: list[str] = dataclasses.field(default_factory=list)     # not tied to one app (ApplicationSets)
    warnings: list[str] = dataclasses.field(default_factory=list)
    appsets: list[dict] = dataclasses.field(default_factory=list)   # as found, for the reachability check


def is_helm_test(r: dict) -> bool:
    """Helm test hooks: Argo CD renders them but never runs them (and charts
    often name their test Pods randomly, which would show in every diff)."""
    hooks = ((r.get("metadata") or {}).get("annotations") or {}).get(HELM_HOOK, "")
    return any(h.strip().startswith("test") for h in hooks.split(","))


def preview_name(run: str, original: str) -> str:
    """A DNS-safe, run-specific name; long names keep a hash for uniqueness."""
    name = f"{run}-{original}"
    if len(name) > 63:
        h = hashlib.sha256(original.encode()).hexdigest()[:8]
        name = f"{name[:54]}-{h}"
    return name


def _project(app: dict) -> str:
    """An Application's project as written."""
    return (app.get("spec") or {}).get("project") or "default"


def _sources(spec: dict) -> list[dict]:
    """An Application spec's sources: 'sources' (multi-source) or the single 'source'."""
    if spec.get("sources"):
        return spec["sources"]
    return [spec["source"]] if spec.get("source") else []


def pin_revision(source: dict, cfg: Config, revision: str) -> bool:
    """Pin a source on this repository that tracks a branch to the commit under test."""
    if same_repo(source.get("repoURL", ""), cfg.repo_url) and \
            (source.get("targetRevision") or "HEAD") in cfg.tracked_revisions:
        source["targetRevision"] = revision
        return True
    return False


def rewrite_app(app: dict, *, run: str, cfg: Config, revision: str, namespace: str) -> dict:
    """A copy of an Application, made safe to submit to the preview and
    pinned to the revision under test (see the module docstring)."""
    a = copy.deepcopy(app)
    original = a["metadata"]["name"]
    a["metadata"] = {"name": preview_name(run, app_key(app, ("argocd", namespace)).replace("/", "-")), "namespace": namespace,
                     "labels": {RUN_LABEL: run, ORIGIN_LABEL: original[:63]}}
    for k in ("operation", "status"):
        a.pop(k, None)
    spec = a.setdefault("spec", {})
    spec.pop("syncPolicy", None)
    spec["project"] = "default"
    for s in _sources(spec):
        pin_revision(s, cfg, revision)
    return a


def _walk_generators(gens: list[dict], cfg: Config, revision: str) -> None:
    """Pin the git generators on this repository to the revision under test,
    including those nested in matrix and merge generators (in place)."""
    for g in gens or []:
        if "git" in g:
            git = g["git"]
            if same_repo(git.get("repoURL", ""), cfg.repo_url) and \
                    (git.get("revision") or "HEAD") in cfg.tracked_revisions:
                git["revision"] = revision
        for nested in ("matrix", "merge"):
            if nested in g:
                _walk_generators(g[nested].get("generators", []), cfg, revision)


def rewrite_appset(appset: dict, *, cfg: Config, revision: str, namespace: str) -> dict:
    """A copy of an ApplicationSet for the preview's generate API: in the
    preview's namespace (it refuses any other), git generators pinned, the
    template's project 'default' (the preview has no other; Renderer._generate
    puts the written one back on the generated Applications). The rest of the
    template is left alone: the Applications it generates are rewritten like
    any other before they are submitted."""
    s = copy.deepcopy(appset)
    s["metadata"] = {"name": s["metadata"]["name"], "namespace": namespace}
    s.pop("status", None)
    _walk_generators(s.get("spec", {}).get("generators", []), cfg, revision)
    tspec = s.get("spec", {}).get("template", {}).get("spec")
    if isinstance(tspec, dict) and "project" in tspec:
        tspec["project"] = "default"
    return s


def entry_app(entry: Entrypoint, cfg: Config) -> dict:
    """The stand-in for an entry point: the Application Argo CD would need to
    deploy the entry point's directory (a directory source on this
    repository, rewritten and pinned like any other; recursive when the entry
    point says so). Its output is what the walk starts from."""
    source = {"repoURL": cfg.repo_url, "targetRevision": cfg.tracked_revisions[0], "path": entry.path}
    if entry.recurse:
        source["directory"] = {"recurse": True}
    return {"apiVersion": "argoproj.io/v1alpha1", "kind": "Application",
            "metadata": {"name": f"entrypoint-{entry.name}"},
            "spec": {"project": "default",
                     "source": source,
                     "destination": {"server": cfg.server(entry.cluster), "namespace": "argocd"}}}


class Renderer:
    """Renders one revision with the preview. Every Application it creates
    there is remembered, so that cleanup() can delete them all (call it in a
    finally: a run that dies leaves them for remove_stale in __main__)."""

    def __init__(self, argo: ArgoCD, cfg: Config, run: str, revision: str):
        self.argo, self.cfg, self.run, self.revision = argo, cfg, run, revision
        self.created: list[str] = []          # preview names, for cleanup()
        self.home = ("argocd", argo.namespace)   # namespaces whose Applications are keyed by name alone

    def render(self, entrypoints: list[Entrypoint] | None = None) -> Rendering:
        """Walk from the entry points (default: all of the configuration's;
        __main__ passes only those that exist at the base revision) and
        return what this revision renders to. Argo CD errors are recorded in
        the result, never raised: one broken Application doesn't stop the walk."""
        out = Rendering(self.revision)
        generated: set[str] = set()
        for entry in entrypoints if entrypoints is not None else self.cfg.entrypoints:
            synthetic = entry_app(entry, self.cfg)
            queue: list[tuple[dict, str | None]] = [(synthetic, None)]
            while queue:
                if len(out.apps) >= MAX_APPS:
                    out.errors.append(f"more than {MAX_APPS} Applications: stopped (a cycle?)")
                    break
                app, parent = queue.pop(0)
                name = app_key(app, self.home)
                if name in out.apps:      # the app-of-apps lists itself
                    continue
                if self._external(app):
                    out.apps[name] = App(name, entry.name, parent, app, external=True, project=_project(app))
                    continue
                rendered = self._render_app(app, entry.name, parent)
                out.apps[name] = rendered
                for r in rendered.resources:
                    if r.get("apiVersion", "").startswith("argoproj.io/") and r.get("kind") == "Application":
                        queue.append((r, name))
                    elif r.get("apiVersion", "").startswith("argoproj.io/") and r.get("kind") == "ApplicationSet":
                        set_name = app_key(r, self.home)
                        if set_name not in generated:
                            generated.add(set_name)
                            queue.extend((a, name) for a in self._generate(r, name, out))
            # An Application of the entry point's own directory (the app-of-apps
            # listing itself) is the real one: the stand-in would duplicate it.
            sname = synthetic["metadata"]["name"]
            if any(a.name != sname and self._covers(a, entry.path) for a in out.apps.values()):
                out.apps.pop(sname, None)
        return out

    def _external(self, app: dict) -> bool:
        """Whether every source is on an external repository (.gitops-check.yaml
        external: e.g. the teams' repositories, which the preview can't read
        and a platform change doesn't touch)."""
        srcs = _sources(app.get("spec", {}))
        return bool(srcs) and bool(self.cfg.external) and all(
            (s.get("repoURL") or "").strip().lower().startswith(tuple(self.cfg.external)) for s in srcs)

    def _covers(self, app: App, path: str) -> bool:
        """Whether an Application deploys this repository's directory `path`."""
        return any(same_repo(s.get("repoURL", ""), self.cfg.repo_url) and (s.get("path") or "").strip("/") == path
                   for s in _sources(app.spec.get("spec", {})))

    def _render_app(self, app: dict, entry: str, parent: str | None) -> App:
        """Rewrite, submit (Argo CD validates it, which renders its sources:
        a bad path or chart fails here) and read back one Application."""
        original = app_key(app, self.home)
        spec = rewrite_app(app, run=self.run, cfg=self.cfg, revision=self.revision, namespace=self.argo.namespace)
        a = App(original, entry, parent, spec, project=_project(app))
        try:
            self.argo.create_app(spec)
            self.created.append(spec["metadata"]["name"])
            a.resources = [r for r in self.argo.manifests(spec["metadata"]["name"]) if not is_helm_test(r)]
        except ArgoError as e:
            a.error = str(e)
        return a

    def _generate(self, appset: dict, parent: str, out: Rendering) -> list[dict]:
        """The Applications an ApplicationSet generates (Argo CD's own
        generators and templates; nothing is stored in the preview). The set
        is also kept for the reachability check (what its git generators read)."""
        name = appset.get("metadata", {}).get("name", "?")
        out.appsets.append(appset)
        try:
            apps = self.argo.generate(rewrite_appset(appset, cfg=self.cfg, revision=self.revision,
                                                     namespace=self.argo.namespace))
        except ArgoError as e:
            out.errors.append(f"ApplicationSet {name} (from {parent}): {e}")
            return []
        if not apps:
            out.warnings.append(f"ApplicationSet {name} (from {parent}) generates no Application")
        # The project as written, for the projects check (unless templated).
        project = str(((appset.get("spec") or {}).get("template") or {}).get("spec", {}).get("project") or "default")
        if "{{" not in project:
            for a in apps:
                a.setdefault("spec", {})["project"] = project
        return apps

    def cleanup(self) -> list[str]:
        """Delete what this run created; returns the failures."""
        failed = []
        for name in self.created:
            try:
                self.argo.delete_app(name)
            except ArgoError as e:
                failed.append(f"{name}: {e}")
        self.created.clear()
        return failed
