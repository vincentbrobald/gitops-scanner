"""The same API the argocd CLI calls, without the CLI: on an Argo CD without an
application controller, `argocd app manifests` asks for a refresh and waits
for a reconciliation that never comes. GET .../manifests asks the repo-server
directly.

Endpoints used, and the permission each needs (the preview's account 'ci'):
    POST   /api/v1/applications?upsert&validate   applications, create/update
    GET    /api/v1/applications/<n>/manifests     applications, get
    GET    /api/v1/applications?selector=...      applications, get
    DELETE /api/v1/applications/<n>?cascade=false applications, delete
    POST   /api/v1/applicationsets/generate       applicationsets, create (nothing is stored)
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request


class ArgoError(Exception):
    """A refusal or an error of Argo CD, with its own message when it gave one
    (e.g. "application spec for x is invalid: InvalidSpecError: ..."), or
    the transport error (unreachable, timeout)."""


class ArgoCD:
    """The preview's API. server: its URL (plain HTTP inside the cluster);
    token: a bearer token of its account; namespace: where Applications live
    (the preview's own namespace: it accepts no other). The timeout is long:
    the first render of a large chart includes fetching it."""

    def __init__(self, server: str, token: str, namespace: str = "argocd-preview", timeout: float = 300):
        self.base = server.rstrip("/")
        self.token = token
        self.namespace = namespace
        self.timeout = timeout

    def _call(self, method: str, path: str, body: dict | None = None, **params) -> dict:
        url = f"{self.base}/api/v1/{path}"
        if params:
            url += "?" + urllib.parse.urlencode({k: str(v).lower() if isinstance(v, bool) else v
                                                 for k, v in params.items()})
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method, headers={
            "Authorization": f"Bearer {self.token}", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                raw = r.read()
        except urllib.error.HTTPError as e:
            try:
                msg = json.loads(e.read()).get("message", "")
            except ValueError:
                msg = ""
            raise ArgoError(msg or f"HTTP {e.code} on {method} {path}") from None
        except (urllib.error.URLError, TimeoutError) as e:
            raise ArgoError(f"{method} {path}: {e}") from None
        return json.loads(raw) if raw else {}

    def create_app(self, app: dict) -> None:
        """Create (or replace) an Application; Argo CD validates it, which
        renders its sources: a broken source fails here (InvalidSpecError)."""
        self._call("POST", "applications", app, upsert=True, validate=True)

    def manifests(self, name: str) -> list[dict]:
        """What an Application renders to, from the repo-server (no controller
        involved): one dict per resource, Helm hooks included."""
        r = self._call("GET", f"applications/{urllib.parse.quote(name)}/manifests", appNamespace=self.namespace)
        return [json.loads(m) for m in r.get("manifests") or []]

    def delete_app(self, name: str) -> None:
        """Delete an Application at once. Without cascade: a cascade adds a
        finalizer that only a controller removes, and the preview has none."""
        self._call("DELETE", f"applications/{urllib.parse.quote(name)}", cascade=False, appNamespace=self.namespace)

    def list_apps(self, selector: str) -> list[dict]:
        return self._call("GET", "applications", selector=selector, appNamespace=self.namespace).get("items") or []

    def generate(self, appset: dict) -> list[dict]:
        """The Applications an ApplicationSet generates (nothing is stored)."""
        return self._call("POST", "applicationsets/generate", {"applicationSet": appset}).get("applications") or []
