"""The ArgoCD client's interface, answered in process by argocd-render (Argo
CD's own rendering code, argocd-render/main.go) instead of a running Argo CD.

One argocd-render runs per LocalArgo, fed JSON lines: the configuration, then
a request per call. This repository is read from the local clone, so pinned
commits need no fetch. create_app renders at once (as Argo CD's validation
would) and manifests returns that rendering; nothing is stored anywhere, so
there is nothing to list and deleting only forgets.
"""
from __future__ import annotations

import dataclasses
import json
import subprocess
from pathlib import Path

from .argocd import ArgoError
from .config import Config


class LocalArgo:
    """argocd-render behind ArgoCD's methods; close() ends it."""

    namespace = "argocd"

    def __init__(self, cfg: Config, repo: Path, binary: str = "argocd-render"):
        """Start binary with cfg's clusters and argocd-cm settings, and repo as the clone of cfg.repo_url.
        Raises ArgoError when it can't be started."""
        try:
            self.proc = subprocess.Popen([binary], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        except OSError as e:
            raise ArgoError(f"argocd-render: {e}") from None
        self.rendered: dict[str, list[dict]] = {}
        self._write({"namespace": self.namespace, "repos": {cfg.repo_url: str(repo)},
                     "clusters": [dataclasses.asdict(c) for c in cfg.clusters.values()], "argocdCM": cfg.argocd_cm})

    def _write(self, obj: dict) -> None:
        """Send one line; a dead process is an ArgoError."""
        try:
            self.proc.stdin.write(json.dumps(obj) + "\n")
            self.proc.stdin.flush()
        except OSError as e:
            raise ArgoError(f"argocd-render: {e}") from None

    def _call(self, req: dict) -> dict:
        """Send req and return the answer; its "error", or the process ending, raises ArgoError."""
        self._write(req)
        line = self.proc.stdout.readline()
        if not line:
            raise ArgoError(f"argocd-render exited with status {self.proc.wait()} (its log is above)")
        answer = json.loads(line)
        if "error" in answer:
            raise ArgoError(answer["error"])
        return answer

    def create_app(self, app: dict) -> None:
        """Render app now and keep the result for manifests(); a failure raises ArgoError."""
        self.rendered[app["metadata"]["name"]] = self._call({"render": app})["manifests"]

    def manifests(self, name: str) -> list[dict]:
        """What create_app rendered for name."""
        return self.rendered[name]

    def delete_app(self, name: str) -> None:
        """Forget name's rendering."""
        self.rendered.pop(name, None)

    def list_apps(self, selector: str) -> list[dict]:
        """None: nothing outlives a run, so no run leaves stale Applications."""
        return []

    def generate(self, appset: dict) -> list[dict]:
        """The Applications appset generates."""
        return self._call({"generate": appset})["applications"] or []

    def close(self) -> None:
        """End argocd-render (it exits at the end of its input)."""
        if self.proc.stdin:
            self.proc.stdin.close()
        self.proc.wait()
