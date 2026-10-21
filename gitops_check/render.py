"""What is submitted is rewritten so that it renders and can never deploy:
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
ORIGIN_LABEL = "gitops-check/app"     # the original name, cut to 63 characters (a label value's limit)
HELM_HOOK = "helm.sh/hook"
# Applications one revision may render at most, a guard against cycles.
MAX_APPS = 500


def app_key(app: dict, home: tuple[str, ...] = ("argocd",)) -> str:
    """An Application's key: its name, prefixed with "<namespace>/" unless its
    namespace is one of home (no namespace counts as "argocd")."""
    md = app.get("metadata") or {}
    ns, name = md.get("namespace") or "argocd", md.get("name", "")
    return name if ns in home else f"{ns}/{name}"


@dataclasses.dataclass
class App:
    """One Application of a rendering and what it rendered to."""
    name: str                        # key: [namespace/]name
    entrypoint: str
    parent: str | None               # original name of the Application that rendered it; None: a stand-in
    spec: dict                       # as submitted (rewritten): its sources tell where it came from
    resources: list[dict] = dataclasses.field(default_factory=list)
    error: str | None = None         # Argo CD's error when it couldn't render; resources is empty then
    external: bool = False           # sources all on external repositories: not rendered
    project: str = "default"         # as written (spec is submitted with 'default')


@dataclasses.dataclass
class Rendering:
    """What one revision renders to: its Applications by key, plus the errors and warnings of the walk."""
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
    # Cut to 54 characters, then "-" and 8 hex of the original name's sha256: 63 in all.
    if len(name) > 63:
        h = hashlib.sha256(original.encode()).hexdigest()[:8]
        name = f"{name[:54]}-{h}"
    return name


def _project(app: dict) -> str:
    """The Application's project as written, "default" when unset."""
    return (app.get("spec") or {}).get("project") or "default"


def _sources(spec: dict) -> list[dict]:
    """The sources of an Application spec: spec.sources, else [spec.source], else [].

    The dicts are the spec's own, so changing them changes the spec.
    """
    if spec.get("sources"):
        return spec["sources"]
    return [spec["source"]] if spec.get("source") else []


def pin_revision(source: dict, cfg: Config, revision: str) -> bool:
    """Set source's targetRevision to revision, in place, when it is on this repository
    and tracks one of trackedRevisions (no targetRevision means HEAD); return whether it did."""
    if same_repo(source.get("repoURL", ""), cfg.repo_url) and \
            (source.get("targetRevision") or "HEAD") in cfg.tracked_revisions:
        source["targetRevision"] = revision
        return True
    return False


def rewrite_app(app: dict, *, run: str, cfg: Config, revision: str, namespace: str) -> dict:
    """A copy of app to submit to the preview, as the module docstring describes; app is left unchanged.

    The new name is preview_name() of its key, "/" replaced with "-", and its
    metadata keeps only the name, namespace and the two labels (finalizers go).
    """
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
    """Pin, in place, the git generators on this repository that track a branch of
    trackedRevisions to revision, recursing into matrix and merge generators."""
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
    puts the written one back on the generated Applications)."""
    s = copy.deepcopy(appset)
    s["metadata"] = {"name": s["metadata"]["name"], "namespace": namespace}
    s.pop("status", None)
    _walk_generators(s.get("spec", {}).get("generators", []), cfg, revision)
    tspec = s.get("spec", {}).get("template", {}).get("spec")
    if isinstance(tspec, dict) and "project" in tspec:
        tspec["project"] = "default"
    return s


def entry_app(entry: Entrypoint, cfg: Config) -> dict:
    """The stand-in Application "entrypoint-<name>" that renders an entry point's directory,
    as the root Application syncing it would (recursively with recurse)."""
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
        """run prefixes the preview names (and is their run label); revision is the commit to render.

        home: the namespaces whose Applications are keyed by name alone.
        """
        self.argo, self.cfg, self.run, self.revision = argo, cfg, run, revision
        self.created: list[str] = []
        self.home = ("argocd", argo.namespace)

    def render(self, entrypoints: list[Entrypoint] | None = None) -> Rendering:
        """Walk from the entry points (default: all of the configuration's;
        __main__ passes only those that exist at the base revision) and
        return what this revision renders to. Argo CD errors are recorded in
        the result, never raised: one broken Application doesn't stop the walk."""
        out = Rendering(self.revision)
        # ApplicationSets already generated, by key, across all entry points.
        generated: set[str] = set()
        stopped = False
        for entry in entrypoints if entrypoints is not None else self.cfg.entrypoints:
            if stopped:
                break
            synthetic = entry_app(entry, self.cfg)
            # Breadth-first walk: each rendered Application queues the Applications
            # it renders and those its ApplicationSets generate, with its key as
            # their parent. An Application already rendered (by key) is skipped,
            # so one reached twice or from two entry points is rendered once.
            # Reaching MAX_APPS ends the whole walk with one error.
            queue: list[tuple[dict, str | None]] = [(synthetic, None)]
            while queue:
                if len(out.apps) >= MAX_APPS:
                    out.errors.append(f"more than {MAX_APPS} Applications: stopped (a cycle?)")
                    stopped = True
                    break
                app, parent = queue.pop(0)
                name = app_key(app, self.home)
                if name in out.apps:
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
            # Drop the stand-in when a real Application renders the same path:
            # its resources are then that Application's, not a duplicate's.
            sname = synthetic["metadata"]["name"]
            if any(a.name != sname and self._covers(a, entry.path) for a in out.apps.values()):
                out.apps.pop(sname, None)
        return out

    def _external(self, app: dict) -> bool:
        """Whether every source of app is on a repository under an `external` prefix (case-insensitive)."""
        srcs = _sources(app.get("spec", {}))
        return bool(srcs) and bool(self.cfg.external) and all(
            (s.get("repoURL") or "").strip().lower().startswith(tuple(self.cfg.external)) for s in srcs)

    def _covers(self, app: App, path: str) -> bool:
        """Whether app has a source on this repository at path."""
        return any(same_repo(s.get("repoURL", ""), self.cfg.repo_url) and (s.get("path") or "").strip("/") == path
                   for s in _sources(app.spec.get("spec", {})))

    def _render_app(self, app: dict, entry: str, parent: str | None) -> App:
        """Submit a rewritten copy of app to the preview and return it with its resources
        (Helm tests left out), or with the error when Argo CD refuses or fails to render it.

        The copy is remembered for cleanup() once created.
        """
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
        """The Applications appset generates, from the preview's generate API.

        Records appset in out.appsets, and in out.errors / out.warnings a failure
        or an empty result. The template's project, sent as 'default', is put
        back on the generated Applications unless it is templated ("{{").
        """
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
