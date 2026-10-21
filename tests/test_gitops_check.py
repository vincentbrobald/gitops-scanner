import copy

import pytest

from gitops_check import checks, config, diff, render, report
from gitops_check.argocd import ArgoError
from gitops_check.forgejo import token_from_git_credentials
from gitops_check.render import (
    ORIGIN_LABEL,
    RUN_LABEL,
    App,
    Renderer,
    Rendering,
    preview_name,
    rewrite_app,
    rewrite_appset,
)

REPO = "ssh://git@forge.example:2222/org/gitops.git"
CONFIG = f"""
apiVersion: gitops-check.example/v1alpha1
kind: CheckConfig
repository: {{url: {REPO}, trackedRevisions: [main, HEAD]}}
clusters: [{{name: lab, server: https://kubernetes.default.svc}}]
entrypoints:
  - {{name: lab, cluster: lab, path: clusters/lab/addons/}}
ignore: [{{path: old, reason: kept}}]
"""


@pytest.fixture
def cfg(tmp_path):
    p = tmp_path / ".gitops-check.yaml"
    p.write_text(CONFIG)
    return config.load(p)


def app(name, path="x", repo=REPO, rev="main", **spec):
    return {"apiVersion": "argoproj.io/v1alpha1", "kind": "Application",
            "metadata": {"name": name, "namespace": "argocd", "finalizers": ["resources-finalizer.argocd.argoproj.io"]},
            "spec": {"project": "infra", "source": {"repoURL": repo, "targetRevision": rev, "path": path},
                     "destination": {"server": "https://kubernetes.default.svc"},
                     "syncPolicy": {"automated": {}}, **spec}}


def test_config(cfg):
    assert cfg.entrypoints[0].path == "clusters/lab/addons"
    assert cfg.server("lab") == "https://kubernetes.default.svc"
    assert cfg.ignore == ["old"]


def test_entrypoint_recurse(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text(CONFIG.replace("path: clusters/lab/addons/}", "path: clusters/lab/addons/, recurse: true}"))
    c = config.load(p)
    assert c.entrypoints[0].recurse
    assert render.entry_app(c.entrypoints[0], c)["spec"]["source"]["directory"] == {"recurse": True}
    t = tmp_path / "repo"
    (t / "clusters/lab/addons/generated").mkdir(parents=True)
    (t / "clusters/lab/addons/generated/root.yaml").write_text("{}")
    assert checks.unreached(rendering(), c, t, [])[0] == []


def test_config_unknown_cluster(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text(CONFIG.replace("cluster: lab,", "cluster: nope,"))
    with pytest.raises(config.ConfigError, match="unknown cluster"):
        config.load(p)


def test_same_repo():
    assert config.same_repo(REPO, REPO.upper().removesuffix(".GIT") + "/")
    assert not config.same_repo(REPO, "https://forge.example/org/gitops.git")


def test_preview_name():
    assert preview_name("r1", "addons") == "r1-addons"
    long = preview_name("r1", "x" * 80)
    assert len(long) <= 63 and long != preview_name("r1", "x" * 79 + "y")


def test_rewrite_app(cfg):
    a = rewrite_app(app("web", rev="main"), run="r1", cfg=cfg, revision="abc", namespace="prev")
    assert a["metadata"] == {"name": "r1-web", "namespace": "prev", "labels": {RUN_LABEL: "r1", ORIGIN_LABEL: "web"}}
    assert "syncPolicy" not in a["spec"] and a["spec"]["project"] == "default"
    assert a["spec"]["source"]["targetRevision"] == "abc"


@pytest.mark.parametrize("repo,rev,pinned", [(REPO, "HEAD", True), (REPO, "v1.2", False),
                                            ("https://charts.example", "main", False)])
def test_rewrite_app_pins_tracked_sources_only(cfg, repo, rev, pinned):
    a = rewrite_app(app("web", repo=repo, rev=rev), run="r", cfg=cfg, revision="abc", namespace="p")
    assert (a["spec"]["source"]["targetRevision"] == "abc") is pinned


def test_rewrite_app_multi_source(cfg):
    src = app("web")
    src["spec"].pop("source")
    src["spec"]["sources"] = [{"repoURL": "https://charts.example", "chart": "c", "targetRevision": "1.0"},
                              {"repoURL": REPO, "targetRevision": "main", "ref": "values"}]
    s = rewrite_app(src, run="r", cfg=cfg, revision="abc", namespace="p")["spec"]["sources"]
    assert [x["targetRevision"] for x in s] == ["1.0", "abc"]


def test_rewrite_appset_nested_generators(cfg):
    s = {"metadata": {"name": "set", "namespace": "argocd"}, "spec": {"generators": [
        {"matrix": {"generators": [{"clusters": {}}, {"git": {"repoURL": REPO, "revision": "main"}}]}},
        {"git": {"repoURL": REPO, "revision": "v1"}}]}}
    r = rewrite_appset(s, cfg=cfg, revision="abc", namespace="p")
    assert r["metadata"]["namespace"] == "p"
    assert r["spec"]["generators"][0]["matrix"]["generators"][1]["git"]["revision"] == "abc"
    assert r["spec"]["generators"][1]["git"]["revision"] == "v1"
    assert s["spec"]["generators"][1]["git"]["revision"] == "v1"


class FakeArgo:
    """Renders from a dict: path -> resources; a path 'broken' fails."""
    namespace = "prev"

    def __init__(self, tree, appsets=None):
        self.tree, self.appsets, self.apps, self.deleted = tree, appsets or {}, {}, []

    def create_app(self, a):
        if a["spec"]["source"]["path"] == "broken":
            raise ArgoError("InvalidSpecError: Unable to generate manifests in broken")
        self.apps[a["metadata"]["name"]] = a

    def manifests(self, name):
        return self.tree[self.apps[name]["spec"]["source"]["path"]]

    def delete_app(self, name):
        self.deleted.append(name)

    def generate(self, s):
        self.generated = s
        return copy.deepcopy(self.appsets[s["metadata"]["name"]])


def test_render_follows_apps_and_appsets(cfg):
    cm = {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "c", "namespace": "n"}}
    tree = {"clusters/lab/addons": [app("addons", "clusters/lab/addons"), app("web", "web"),
                                    {"apiVersion": "argoproj.io/v1alpha1", "kind": "ApplicationSet",
                                     "metadata": {"name": "set"},
                                     "spec": {"generators": [], "template": {"spec": {"project": "tenants"}}}}],
            "web": [cm], "gen": [cm | {"metadata": {"name": "g", "namespace": "n"}}]}
    argo = FakeArgo(tree, {"set": [app("from-set", "gen")]})
    r = Renderer(argo, cfg, "r1", "abc")
    out = r.render()
    assert set(out.apps) == {"addons", "web", "from-set"}
    assert out.apps["web"].parent == "entrypoint-lab"
    assert checks.duplicates(out) == ([], [])
    assert argo.apps["r1-web"]["spec"]["source"]["targetRevision"] == "abc"
    assert len(out.appsets) == 1
    assert argo.generated["spec"]["template"]["spec"]["project"] == "default"
    assert out.apps["from-set"].project == "tenants" and out.apps["web"].project == "infra"
    assert argo.apps["r1-from-set"]["spec"]["project"] == "default"
    r.cleanup()
    assert sorted(argo.deleted) == sorted(argo.apps)


def test_render_records_errors(cfg):
    tree = {"clusters/lab/addons": [app("bad", "broken"),
                                    {"apiVersion": "argoproj.io/v1alpha1", "kind": "ApplicationSet",
                                     "metadata": {"name": "empty"}, "spec": {}}]}
    out = Renderer(FakeArgo(tree, {"empty": []}), cfg, "r", "abc").render()
    assert "entrypoint-lab" in out.apps
    assert "InvalidSpecError" in out.apps["bad"].error
    assert out.warnings == ["ApplicationSet empty (from entrypoint-lab) generates no Application"]
    errors, _ = checks.render_errors(out)
    assert errors == ["bad: InvalidSpecError: Unable to generate manifests in broken"]


def rendering(**apps):
    r = Rendering("abc")
    for name, (spec, resources) in apps.items():
        r.apps[name] = App(name, "lab", None, {"spec": spec}, resources)
    return r


def secret(name, annotations=None, data=None):
    return {"apiVersion": "v1", "kind": "Secret", "metadata": {"name": name, "namespace": "n",
                                                              "annotations": annotations or {}},
            "data": data if data is not None else {"k": "++++++++"}}


def test_duplicates():
    cm = {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "c", "namespace": "n"}}
    errors, _ = checks.duplicates(rendering(a=({}, [cm]), b=({}, [cm])))
    assert errors == ["ConfigMap n/c is rendered by both a and b"]


def test_plaintext_secrets(cfg, tmp_path):
    files = {
        "a/clear.yaml": "kind: Secret\nmetadata: {name: clear}\nstringData: {k: v}\n",
        "a/sops.enc.yaml": "kind: Secret\nmetadata: {name: enc}\nstringData: {k: ENC[x]}\nsops: {mac: m}\n",
        "a/empty.yaml": "kind: Secret\nmetadata: {name: empty}\n",
        "a/repo.yaml": "kind: Secret\nmetadata: {name: repo, labels: {argocd.argoproj.io/secret-type: repository}}\n"
                       "stringData: {url: https://x, type: helm}\n",
        "a/repo-creds.yaml": "kind: Secret\nmetadata: {name: rc, labels: {argocd.argoproj.io/secret-type: repository}}\n"
                             "stringData: {url: https://x, password: p}\n",
        "a/cluster.yaml": "kind: Secret\nmetadata: {name: c, labels: {argocd.argoproj.io/secret-type: cluster}}\n"
                          "stringData: {config: '{\"bearerToken\": \"t\"}'}\n",
        "b/kustomization.yaml": "secretGenerator: [{name: s, literals: [k=v]}]\n",
        "old/clear.yaml": "kind: Secret\nmetadata: {name: ignored}\nstringData: {k: v}\n",
    }
    for f, text in files.items():
        (tmp_path / f).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / f).write_text(text)
    errors, _ = checks.plaintext_secrets(tmp_path, cfg)
    assert [e.split(":")[0] for e in errors] == ["a/clear.yaml", "a/cluster.yaml", "a/repo-creds.yaml",
                                                 "b/kustomization.yaml"]


def test_duplicates_namespace_from_destination():
    svc = {"apiVersion": "v1", "kind": "Service", "metadata": {"name": "web"}}
    ns = {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": "shared"}}
    r = rendering(a=({"destination": {"namespace": "one"}}, [svc, ns]),
                  b=({"destination": {"namespace": "two"}}, [svc, ns]))
    errors, _ = checks.duplicates(r)
    assert errors == ["Namespace shared is rendered by both a and b"]


def test_unreached(cfg, tmp_path):
    t = tmp_path / "repo"
    for f in ["clusters/lab/addons/app.yaml", "clusters/lab/web/kustomization.yaml", "base/web/deploy.yaml",
              "orphan/x.yaml", "old/y.yaml", "samples/apps/a/k.yaml", "rec/sub/z.yaml", ".tekton/ci.yaml",
              "chart/Chart.yaml", "chart/templates/t.yaml",
              "ns/kustomization.yaml", "ns/dev/kustomization.yaml"]:
        (t / f).parent.mkdir(parents=True, exist_ok=True)
        (t / f).write_text("{}")
    (t / "clusters/lab/web/kustomization.yaml").write_text("resources: [../../../base/web]\n")
    r = rendering(web=({"source": {"repoURL": REPO, "path": "clusters/lab/web"}}, []),
                  rec=({"source": {"repoURL": REPO, "path": "rec", "directory": {"recurse": True}}}, []),
                  chart=({"source": {"repoURL": REPO, "path": "chart"}}, []),
                  ns=({"source": {"repoURL": REPO, "path": "ns", "kustomize": {"components": ["dev"]}}}, []))
    errors, _ = checks.unreached(r, cfg, t, ["samples/apps/*"])
    assert [e.split("/ ")[0] for e in errors] == [".tekton", "orphan"]
    assert "directory" not in render.entry_app(cfg.entrypoints[0], cfg)["spec"]["source"]


def test_generated_edits(tmp_path):
    (tmp_path / "gen.yaml").write_text(checks.GENERATED_MARK + " from the cluster file\nkind: X\n")
    (tmp_path / "hand.yaml").write_text("kind: X\n")
    _, warnings = checks.generated_edits(tmp_path, ["gen.yaml", "hand.yaml", "gone.yaml"])
    assert len(warnings) == 1 and warnings[0].startswith("gen.yaml")


def test_projects(cfg):
    proj = {"apiVersion": "argoproj.io/v1alpha1", "kind": "AppProject", "metadata": {"name": "svc", "namespace": "argocd"},
            "spec": {"sourceRepos": [REPO], "clusterResourceWhitelist": [{"group": "", "kind": "Namespace"}],
                     "destinations": [{"server": "*", "namespace": "*"}, {"server": "*", "namespace": "!kube-*"}],
                     "namespaceResourceWhitelist": [{"group": "*", "kind": "ConfigMap"}, {"group": "", "kind": "Namespace"}]}}
    root = {"apiVersion": "argoproj.io/v1alpha1", "kind": "AppProject", "metadata": {"name": "default", "namespace": "argocd"},
            "spec": {"sourceRepos": ["*"], "destinations": [{"server": "*", "namespace": "*"}],
                     "clusterResourceWhitelist": [{"group": "*", "kind": "*"}]}}
    cm = {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "c"}}
    ns = {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": "web"}}
    cr = {"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "ClusterRole", "metadata": {"name": "x"}}
    sec = {"apiVersion": "v1", "kind": "Secret", "metadata": {"name": "s", "namespace": "kube-system"}}
    dest = {"server": "https://kubernetes.default.svc", "namespace": "web"}
    r = rendering(root=({"destination": {"server": "https://kubernetes.default.svc", "namespace": "argocd"}}, [proj, root]),
                  good=({"source": {"repoURL": REPO}, "destination": dest}, [cm, ns]),
                  bad=({"source": {"repoURL": "https://charts.example"}, "destination": dict(dest, namespace="kube-system")},
                       [cm, cr, sec]),
                  lost=({"source": {"repoURL": REPO}, "destination": dest}, []))
    for name, p in [("good", "svc"), ("bad", "svc"), ("lost", "nope")]:
        r.apps[name].project = p
        r.apps[name].parent = "root"
    errors, _ = checks.projects(r, cfg)
    assert [e.split(":")[0] for e in errors] == ["bad"] * 5 + ["lost"]
    assert any("cluster-scoped kind" in e and "ClusterRole x" in e for e in errors)
    assert any("kind not permitted" in e and "Secret kube-system/s" in e for e in errors)
    r.apps["good"].entrypoint = "samples"
    r.apps["good"].project, r.apps["good"].spec["spec"]["source"] = "nope", {"repoURL": "x"}
    cfg.entrypoints.append(config.Entrypoint("samples", "lab", "samples", deployed=False))
    assert len(checks.projects(r, cfg)[0]) == 6
    assert checks.projects(rendering(good=({"destination": dest}, [cm])), cfg) == ([], [])


def test_compare():
    dep = {"apiVersion": "apps/v1", "kind": "Deployment",
           "metadata": {"name": "d", "namespace": "n", "annotations": {"argocd.argoproj.io/tracking-id": "x"}},
           "spec": {"replicas": 1}}
    moved = dict(dep, metadata=dict(dep["metadata"], annotations={"argocd.argoproj.io/tracking-id": "y"}))
    scaled = dict(moved, spec={"replicas": 2})
    before = rendering(same=({}, [dep]), scaled=({}, [dep]), gone=({}, [dep]))
    after = rendering(same=({}, [moved]), scaled=({}, [scaled]), new=({}, [dep]))
    d = {x.app: x for x in diff.compare(before, after)}
    assert set(d) == {"scaled", "gone", "new"}
    assert d["new"].status == "added" and d["gone"].status == "removed"
    assert "-  replicas: 1\n+  replicas: 2" in d["scaled"].changed["apps/Deployment n/d"]


def test_secret_keys_compared_not_values():
    a = secret("s", {diff.FINGERPRINTS: "k: 1\n"})
    b = secret("s", {diff.FINGERPRINTS: "k: 2\n"})
    d = diff.compare(rendering(x=({}, [a])), rendering(x=({}, [b])))
    assert "k: 2" in d[0].changed["Secret n/s"] and "++++" not in d[0].changed["Secret n/s"]


def test_report_truncates():
    big = diff.AppDiff("a", "changed", changed={"r": "+x\n" * 5000})
    text = report.markdown(errors=["e"], warnings=[], diffs=[big, big], base="b" * 40, head="h" * 40,
                           counts=(1, 1), limit=12000)
    assert len(text) < 12000 and "left out" in text and "failed: 1 error" in text


def test_token_from_git_credentials(tmp_path):
    p = tmp_path / ".git-credentials"
    p.write_text("https://git:s3cr%40t@forge.example\n")
    assert token_from_git_credentials(p) == "s3cr@t"


def test_normalize_secret_without_values():
    s = {"apiVersion": "v1", "kind": "Secret", "metadata": {"name": "s"}, "data": None}
    assert diff.normalize(s)["data"] == {}


def test_helm_tests_dropped():
    from gitops_check.render import is_helm_test
    assert is_helm_test({"metadata": {"annotations": {"helm.sh/hook": "test-success"}}})
    assert is_helm_test({"metadata": {"annotations": {"helm.sh/hook": "pre-install, test"}}})
    assert not is_helm_test({"metadata": {"annotations": {"helm.sh/hook": "pre-install"}}})


def test_compare_failed_render():
    dep = {"apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"name": "d", "namespace": "n"}}
    after = rendering(x=({}, []))
    after.apps["x"].error = "InvalidSpecError"
    d = diff.compare(rendering(x=({}, [dep])), after)
    assert [(x.app, x.status, x.removed) for x in d] == [("x", "failed", [])]
    text = report.markdown(errors=["x"], warnings=[], diffs=d, base="b", head="h", counts=(1, 1))
    assert "render failed" in text and "<details>" not in text


def test_report_without_limit_keeps_every_diff():
    big = diff.AppDiff("a", "changed", changed={"r": "+x\n" * 5000})
    text = report.markdown(errors=[], warnings=[], diffs=[big, big], base="b" * 40, head="h" * 40,
                           counts=(1, 1), limit=None)
    assert "left out" not in text and text.count("<details>") == 2


def test_git_generator_globs_templated(cfg):
    appsets = [{"spec": {"generators": [{"matrix": {"generators": [
        {"git": {"repoURL": REPO, "files": [{"path": "applications/*/organization.yaml"}]}},
        {"git": {"repoURL": REPO, "files": [{"path": "applications/{{ .org.path.basename }}/*/team.yaml"}],
                 "directories": [{"path": "x/*", "exclude": True}]}},
    ]}}]}}]
    assert checks.git_generator_globs(None, cfg, appsets) == [
        "applications/*/organization.yaml", "applications/*/*/team.yaml"]


def test_render_same_name_other_namespaces_and_external(tmp_path):
    p = tmp_path / ".gitops-check.yaml"
    p.write_text(CONFIG + "external: [{prefix: 'ssh://git@forge.example:2222/tenants/', reason: tenants}]\n")
    c = config.load(p)
    assert c.external == ["ssh://git@forge.example:2222/tenants/"]
    tenant = "ssh://git@forge.example:2222/tenants/a-gitops.git"
    root_a = app("root", "apps", repo=tenant)
    root_a["metadata"]["namespace"] = "team-a"
    root_b = app("root", "b")
    root_b["metadata"]["namespace"] = "team-b"
    tree = {"clusters/lab/addons": [app("addons", "clusters/lab/addons"), app("root", "demo"), root_a, root_b],
            "demo": [], "b": []}
    argo = FakeArgo(tree)
    out = Renderer(argo, c, "r", "abc").render()
    assert {"root", "team-a/root", "team-b/root"} <= set(out.apps)
    assert out.apps["team-a/root"].external and out.apps["team-a/root"].error is None
    assert "r-team-a-root" not in argo.apps and "r-team-b-root" in argo.apps
    assert argo.apps["r-team-b-root"]["metadata"]["labels"]["gitops-check/app"] == "root"
    gen = app("from-set", "b")
    gen["metadata"]["namespace"] = FakeArgo.namespace
    assert render.app_key(gen, ("argocd", FakeArgo.namespace)) == "from-set"
