"""The report in Markdown, for the log, --report and the pull request comment."""
from __future__ import annotations

from .diff import AppDiff

# Hidden first line by which forgejo.upsert_comment finds the previous report.
MARKER = "<!-- gitops-check -->"
LIMIT = 60000   # Forgejo comments hold 65535 characters


def markdown(*, errors: list[str], warnings: list[str], diffs: list[AppDiff], base: str, head: str,
             counts: tuple[int, int], limit: int | None = LIMIT) -> str:
    """The report. With a limit (characters; the default fits a Forgejo
    comment), the <details> blocks that don't fit are left out and a note
    says so; limit=None keeps everything (the log, --report)."""
    ok = not errors
    head_line = "✅ **gitops-check passed**" if ok else f"❌ **gitops-check failed: {len(errors)} error(s)**"
    summary = (f"Rendered `{base[:10]}` (before, {counts[0]} Applications) and `{head[:10]}` "
               f"(after, {counts[1]} Applications) with the render-only Argo CD.")
    out = [MARKER, head_line, "", summary, ""]
    if errors:
        out += ["### Errors (block the merge)", ""] + [f"- {e}" for e in errors] + [""]
    if warnings:
        out += ["### Warnings", ""] + [f"- {w}" for w in warnings] + [""]
    if not diffs:
        out += ["### Changes", "", "No rendered resource changes.", ""]
    else:
        out += ["### Changes", "", "| Application | | added | removed | changed |", "|---|---|---|---|---|"]
        out += [f"| `{d.app}` | {d.status} | {len(d.added)} | {len(d.removed)} | {len(d.changed)} |"
                if d.status != "failed" else f"| `{d.app}` | **render failed** | | | |" for d in diffs]
        out.append("")
        details = []
        for d in (x for x in diffs if x.status != "failed"):
            body = [f"<details><summary><code>{d.app}</code> ({d.status})</summary>", ""]
            body += [f"- added: `{r}`" for r in d.added] + [f"- removed: `{r}`" for r in d.removed]
            for r, text in d.changed.items():
                body += ["", f"`{r}`", "", "```diff", text.rstrip("\n"), "```"]
            body += ["", "</details>", ""]
            details.append("\n".join(body))
        # Append the per-Application details while they fit, keeping 200
        # characters for the note on what was left out.
        text = "\n".join(out)
        for i, block in enumerate(details):
            if limit is not None and len(text) + len(block) > limit - 200:
                text += (f"\n_{len(details) - i} more Application diff(s) left out: see the pipeline's log "
                         f"(the check step prints the whole report)._\n")
                break
            text += "\n" + block
        return text
    return "\n".join(out)
