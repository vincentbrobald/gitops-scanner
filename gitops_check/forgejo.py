"""The report as a pull request comment: one per pull request, updated.

The comment is found again by the hidden marker report.MARKER at its top, so
each run replaces its previous report instead of adding one; comments of
people and of Pipelines-as-Code itself are never touched. The token is
pac-bot's, as Pipelines-as-Code hands it to each run (a write collaborator's
token may comment on pull requests).
"""
from __future__ import annotations

import json
import ssl
import urllib.parse
import urllib.request
from pathlib import Path

from .report import MARKER


def token_from_git_credentials(path: Path) -> str:
    """The token in a .git-credentials line (https://user:token@host), as
    Pipelines-as-Code writes it for each run."""
    for line in path.read_text().splitlines():
        u = urllib.parse.urlsplit(line.strip())
        if u.password:
            return urllib.parse.unquote(u.password)
    raise ValueError(f"no token in {path}")


class Forgejo:
    """Forgejo's API for one repository (owner/name). cafile: the site CA,
    when Forgejo's certificate isn't publicly trusted."""

    def __init__(self, url: str, repo: str, token: str, cafile: str | None = None):
        """url: Forgejo's base URL; repo: owner/name; token: an API token. No request is made yet."""
        self.api = f"{url.rstrip('/')}/api/v1/repos/{repo}"
        self.token = token
        self.ctx = ssl.create_default_context(cafile=cafile) if cafile else None

    def _call(self, method: str, path: str, body: dict | None = None):
        """Call the repository's API at path and return the decoded JSON body (None if empty).

        Errors are not caught: urllib's HTTPError/URLError (OSError) reach the caller.
        """
        req = urllib.request.Request(f"{self.api}/{path}", method=method,
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Authorization": f"token {self.token}",
                                              "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=60, context=self.ctx) as r:
            raw = r.read()
        return json.loads(raw) if raw else None

    def upsert_comment(self, pr: int, body: str) -> None:
        """Replace the latest comment carrying the marker, or add one."""
        mine = [c for c in self._call("GET", f"issues/{pr}/comments") or [] if MARKER in c.get("body", "")]
        if mine:
            self._call("PATCH", f"issues/comments/{mine[-1]['id']}", {"body": body})
        else:
            self._call("POST", f"issues/{pr}/comments", {"body": body})
