from __future__ import annotations

import json
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

import pytest
import yaml

from gitops_check import __main__ as cli
from gitops_check import checks, kinds
from gitops_check.argocd import ArgoCD, ArgoError
from gitops_check.forgejo import Forgejo
from gitops_check.render import App, Rendering

REPO_URL = "ssh://git@forge.example:2222/org/gitops.git"
CONFIG = f"""\
apiVersion: gitops-check.example/v1alpha1
kind: CheckConfig
repository: {{url: {REPO_URL}, trackedRevisions: [main, HEAD]}}
clusters: [{{name: lab, server: https://kubernetes.default.svc}}]
entrypoints: [{{name: lab, cluster: lab, path: apps}}]
"""


def app_yaml(name: str, path: str) -> str:
    """An Application (YAML) of the test repository at main, rendering path into namespace name."""
    return yaml.safe_dump({
        "apiVersion": "argoproj.io/v1alpha1", "kind": "Application",
        "metadata": {"name": name, "namespace": "argocd"},
        "spec": {"project": "default", "source": {"repoURL": REPO_URL, "targetRevision": "main", "path": path},
                 "destination": {"server": "https://kubernetes.default.svc", "namespace": name},
                 "syncPolicy": {"automated": {}}}})


CONFIGMAP = "apiVersion: v1\nkind: ConfigMap\nmetadata: {name: settings}\ndata: {mode: one}\n"


def git(repo: Path, *args: str) -> str:
    """Run git in repo; its stripped stdout. Raises CalledProcessError on failure."""
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    """An empty git repository on branch main, with a committer identity."""
    r = tmp_path / "gitops"
    r.mkdir()
    git(r, "init", "-q", "-b", "main")
    git(r, "config", "user.email", "t@example.org")
    git(r, "config", "user.name", "test")
    return r


def commit(repo: Path, files: dict[str, str | None], msg: str = "change") -> str:
    """Write files (None deletes one), commit everything and return the commit's id."""
    for name, text in files.items():
        p = repo / name
        if text is None:
            p.unlink()
            continue
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", msg)
    return git(repo, "rev-parse", "HEAD")


class FakeArgo(BaseHTTPRequestHandler):
    """Argo CD's API as gitops-check calls it, rendering an Application by reading the
    YAML files of its path in repo. The argo fixture subclasses it per test, with
    fresh class attributes: apps (name -> Application) and calls (method, path)."""
    repo: Path
    apps: dict
    calls: list

    def log_message(self, *a):
        # Silence the request log.
        pass

    def _send(self, code: int, body: dict | None = None):
        """Answer with status code and body as JSON ({} when None)."""
        data = json.dumps(body or {}).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(data)

    def _body(self) -> dict:
        """The request's JSON body, {} when it has none."""
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n)) if n else {}

    def _render(self, app: dict) -> list[dict]:
        """The YAML documents under the source path at its targetRevision (git); ValueError if none."""
        src = app["spec"]["source"]
        rev, path = src["targetRevision"], src["path"].strip("/")
        names = git(self.repo, "ls-tree", "-r", "--name-only", rev, "--", path).splitlines()
        if not names:
            raise ValueError(f"app path {path} does not exist")
        docs = []
        for n in names:
            if n.endswith((".yaml", ".yml")):
                docs += [d for d in yaml.safe_load_all(git(self.repo, "show", f"{rev}:{n}")) if d]
        return docs

    def do_POST(self):
        """Create an Application (validated by rendering it), or generate from a list
        generator; 401 unless the token is "tok"."""
        u = urlsplit(self.path)
        self.calls.append(("POST", u.path))
        if self.headers.get("Authorization") != "Bearer tok":
            return self._send(401, {"message": "invalid session"})
        body = self._body()
        if u.path == "/api/v1/applications":
            try:
                self._render(body)       # validate=true renders the sources
            except (ValueError, subprocess.CalledProcessError) as e:
                return self._send(400, {"message": f"InvalidSpecError: {e}"})
            self.apps[body["metadata"]["name"]] = body
            return self._send(200, body)
        if u.path == "/api/v1/applicationsets/generate":
            elements = body["applicationSet"]["spec"]["generators"][0]["list"]["elements"]
            return self._send(200, {"applications": [
                yaml.safe_load(app_yaml(f"gen-{e['name']}", e["path"])) for e in elements]})
        self._send(404, {"message": "not found"})

    def do_GET(self):
        """An Application's manifests, or the Applications having the selector as a label key."""
        u = urlsplit(self.path)
        self.calls.append(("GET", u.path))
        if u.path.endswith("/manifests"):
            name = unquote(u.path.split("/")[-2])
            return self._send(200, {"manifests": [json.dumps(d) for d in self._render(self.apps[name])]})
        if u.path == "/api/v1/applications":
            sel = parse_qs(u.query).get("selector", [""])[0]
            return self._send(200, {"items": [a for a in self.apps.values() if sel in a["metadata"].get("labels", {})]})
        self._send(404, {"message": "not found"})

    def do_DELETE(self):
        """Delete an Application; the client must ask for cascade=false."""
        u = urlsplit(self.path)
        self.calls.append(("DELETE", u.path))
        assert parse_qs(u.query).get("cascade") == ["false"]
        self.apps.pop(unquote(u.path.split("/")[-1]), None)
        self._send(200)


@pytest.fixture
def argo(repo):
    """A FakeArgo server on a free local port, with its own state: (handler class, URL)."""
    handler = type("H", (FakeArgo,), {"repo": repo, "apps": {}, "calls": []})
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield handler, f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def run_main(repo: Path, url: str, base: str, head: str, tmp_path: Path, monkeypatch) -> tuple[int, str]:
    """Run the CLI with token "tok", run id "t", no schemas, and return (exit status, report file text)."""
    monkeypatch.setenv("ARGOCD_TOKEN", "tok")
    report = tmp_path / "report.md"
    code = cli.main(["--repo", str(repo), "--base", base, "--head", head, "--server", url,
                     "--run-id", "t", "--no-schemas", "--report", str(report)])
    return code, report.read_text() if report.exists() else ""


def test_main_passes_and_cleans_up(repo, argo, tmp_path, monkeypatch):
    """A clean change passes, the added Application is reported, worktrees and Applications are removed."""
    handler, url = argo
    base = commit(repo, {".gitops-check.yaml": CONFIG, "apps/web.yaml": app_yaml("web", "web"), "web/cm.yaml": CONFIGMAP})
    head = commit(repo, {"apps/api.yaml": app_yaml("api", "api"),
                         "api/cm.yaml": CONFIGMAP.replace("settings", "api-settings")})
    code, report = run_main(repo, url, base, head, tmp_path, monkeypatch)
    assert code == 0, report
    assert "gitops-check passed" in report
    assert "| `api` | added | 1 |" in report
    assert "`web`" not in report.split("### Changes")[1]
    assert handler.apps == {}
    assert git(repo, "worktree", "list").count("\n") == 0
    assert {a for a in (p for m, p in handler.calls if m == "DELETE")} >= {
        "/api/v1/applications/t-b-web", "/api/v1/applications/t-a-api"}


def test_main_logs_the_whole_report(repo, argo, tmp_path, monkeypatch, capsys):
    """stdout gets every diff, even beyond the comment limit, and matches --report."""
    _, url = argo
    big = "apiVersion: v1\nkind: ConfigMap\nmetadata: {name: big}\ndata: {blob: '%s'}\n"
    base = commit(repo, {".gitops-check.yaml": CONFIG, "apps/web.yaml": app_yaml("web", "web"),
                         "web/cm.yaml": big % ("a" * 40000), "apps/api.yaml": app_yaml("api", "api"),
                         "api/cm.yaml": big % ("c" * 40000)})
    head = commit(repo, {"web/cm.yaml": big % ("b" * 40000), "api/cm.yaml": big % ("d" * 40000)})
    code, report = run_main(repo, url, base, head, tmp_path, monkeypatch)
    out = capsys.readouterr().out
    assert code == 0
    assert "left out" not in out and out.count("<details>") == 2
    assert report == out.rstrip("\n") or report in out


def test_main_reports_changes_and_errors(repo, argo, tmp_path, monkeypatch):
    """A render error and a Secret in clear fail the run; the diff is in the report."""
    _, url = argo
    base = commit(repo, {".gitops-check.yaml": CONFIG, "apps/web.yaml": app_yaml("web", "web"), "web/cm.yaml": CONFIGMAP})
    head = commit(repo, {"web/cm.yaml": CONFIGMAP.replace("one", "two"),
                         "web/secret.yaml": "apiVersion: v1\nkind: Secret\nmetadata: {name: s}\nstringData: {k: v}\n",
                         "apps/broken.yaml": app_yaml("broken", "nowhere")})
    code, report = run_main(repo, url, base, head, tmp_path, monkeypatch)
    assert code == 1
    assert "broken: InvalidSpecError" in report
    assert "web/secret.yaml: Secret s has values in clear" in report
    assert "-  mode: one\n+  mode: two" in report


def test_main_refuses_an_outdated_pull_request(repo, argo, tmp_path, monkeypatch):
    """A head that doesn't contain the base commit is an error."""
    _, url = argo
    root = commit(repo, {".gitops-check.yaml": CONFIG, "apps/web.yaml": app_yaml("web", "web"), "web/cm.yaml": CONFIGMAP})
    git(repo, "branch", "feature")
    main_tip = commit(repo, {"web/cm.yaml": CONFIGMAP.replace("one", "main")})
    git(repo, "checkout", "-q", "feature")
    head = commit(repo, {"web/other.yaml": CONFIGMAP.replace("settings", "other")})
    assert root
    code, report = run_main(repo, url, main_tip, head, tmp_path, monkeypatch)
    assert code == 1 and "not up to date with its target" in report


def test_main_without_config_or_token(repo, argo, tmp_path, monkeypatch, capsys):
    """No configuration or an unknown revision exits 2; no token is a usage error."""
    _, url = argo
    rev = commit(repo, {"README.md": "no config\n"})
    monkeypatch.setenv("ARGOCD_TOKEN", "tok")
    assert cli.main(["--repo", str(repo), "--base", rev, "--head", rev, "--server", url]) == 2
    assert ".gitops-check.yaml" in capsys.readouterr().err
    assert cli.main(["--repo", str(repo), "--base", "nope", "--head", rev, "--server", url]) == 2
    monkeypatch.delenv("ARGOCD_TOKEN")
    with pytest.raises(SystemExit):
        cli.main(["--repo", str(repo), "--base", rev, "--head", rev, "--server", url])


def test_argocd_client(repo, argo):
    """The client's calls against the fake, and its errors: refused spec, bad token, no server."""
    handler, url = argo
    commit(repo, {"web/cm.yaml": CONFIGMAP})
    a = ArgoCD(url, "tok")
    app = yaml.safe_load(app_yaml("x-web", "web"))
    app["metadata"]["labels"] = {"gitops-check/run": "x"}
    a.create_app(app)
    assert [m["kind"] for m in a.manifests("x-web")] == ["ConfigMap"]
    assert [i["metadata"]["name"] for i in a.list_apps("gitops-check/run")] == ["x-web"]
    gen = a.generate({"spec": {"generators": [{"list": {"elements": [{"name": "one", "path": "web"}]}}]}})
    assert [g["metadata"]["name"] for g in gen] == ["gen-one"]
    a.delete_app("x-web")
    assert handler.apps == {}
    with pytest.raises(ArgoError, match="InvalidSpecError: app path nowhere does not exist"):
        a.create_app(yaml.safe_load(app_yaml("bad", "nowhere")))
    with pytest.raises(ArgoError, match="invalid session"):
        ArgoCD(url, "wrong").create_app(app)
    with pytest.raises(ArgoError, match="Connection refused|refused"):
        ArgoCD("http://127.0.0.1:9", "tok", timeout=2).list_apps("x")


def test_forgejo_upsert_comment():
    """The report comment is added once, then replaced; other comments are kept."""
    comments: list[dict] = []

    class H(BaseHTTPRequestHandler):
        """Forgejo's comment API on the list comments."""

        def log_message(self, *a):
            # Silence the request log.
            pass

        def _send(self, body):
            """Answer 200 with body as JSON."""
            data = json.dumps(body).encode()
            self.send_response(200)
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            """List the comments; the token must be sent as "token t0k"."""
            assert self.headers["Authorization"] == "token t0k"
            self._send(comments)

        def do_POST(self):
            """Add a comment with the next id."""
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            comments.append({"id": len(comments) + 1, "body": body["body"]})
            self._send(comments[-1])

        def do_PATCH(self):
            """Replace the body of the comment whose id ends the path."""
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            cid = int(self.path.rsplit("/", 1)[1])
            next(c for c in comments if c["id"] == cid)["body"] = body["body"]
            self._send({})

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        f = Forgejo(f"http://127.0.0.1:{srv.server_address[1]}", "org/gitops", "t0k")
        comments.append({"id": 1, "body": "someone else's comment"})
        f.upsert_comment(4, "<!-- gitops-check -->\nfirst")
        f.upsert_comment(4, "<!-- gitops-check -->\nsecond")
        assert [c["body"].split("\n")[-1] for c in comments] == ["someone else's comment", "second"]
    finally:
        srv.shutdown()


def test_schemas_maps_results_to_applications(tmp_path, monkeypatch):
    """kubeconform results map back to Applications; skipped kinds are counted; no binary is a warning."""
    out = tmp_path / "kubeconform-output.json"
    fake = tmp_path / "kubeconform"
    fake.write_text(f"#!/bin/sh\ncat > /dev/null\ncat {out}\n")
    out.write_text(json.dumps({"resources": [
        {"kind": "ConfigMap", "name": "ok", "status": "statusValid"},
        {"kind": "Deployment", "name": "d", "status": "statusInvalid",
         "msg": "problem validating schema. Check JSON formatting: jsonschema validation failed with 'x#' - at "
                "'/spec/replicas': got string, want integer"},
        {"kind": "Widget", "name": "w", "status": "statusSkipped"}]}))
    fake.chmod(0o755)
    monkeypatch.setenv("KUBECONFORM_CACHE", str(tmp_path / "cache"))
    r = Rendering("abc")
    r.apps["a"] = App("a", "lab", None, {"spec": {}}, [{"kind": "ConfigMap", "metadata": {"name": "ok"}}])
    r.apps["b"] = App("b", "lab", None, {"spec": {}}, [
        {"kind": "Deployment", "metadata": {"name": "d"}},
        {"kind": "Secret", "metadata": {"name": "s"}, "data": {"k": "++++++++"}}])
    errors, warnings = checks.schemas(r, kubeconform=str(fake))
    assert errors == ["b: Deployment d: at '/spec/replicas': got string, want integer"]
    assert warnings == ["1 resource(s) without a known schema: not validated"]
    missing = tmp_path / "missing"
    assert checks.schemas(r, kubeconform=str(missing)) == ([], [f"{missing} not found: schemas not checked"])


def test_cluster_scoped_kinds(monkeypatch):
    """Outside a pod, discovery gives None and the built-in list answers."""
    monkeypatch.setattr(kinds, "SA", Path("/nonexistent"))
    monkeypatch.setattr(kinds, "_cache", None)
    assert kinds.discover() is None
    assert kinds.is_cluster_scoped({"apiVersion": "v1", "kind": "Namespace"})
    assert kinds.is_cluster_scoped({"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "ClusterRole"})
    assert not kinds.is_cluster_scoped({"apiVersion": "apps/v1", "kind": "Deployment"})
