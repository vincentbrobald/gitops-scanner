from __future__ import annotations

import copy
import dataclasses
import difflib

import yaml

from .checks import dest_ns, rid
from .render import Rendering

NOISE_ANNOTATIONS = ("argocd.argoproj.io/tracking-id",)   # names the preview Application
FINGERPRINTS = "preview.argocd/fingerprints"   # "key: fingerprint" lines, set by the ksops stand-in


def normalize(r: dict) -> dict:
    r = copy.deepcopy(r)
    md = r.get("metadata", {})
    ann = md.get("annotations") or {}
    for a in NOISE_ANNOTATIONS:
        ann.pop(a, None)
    if ann:
        md["annotations"] = ann
    else:
        md.pop("annotations", None)
    if r.get("kind") == "Secret":
        # Values are masked; which keys exist and their fingerprints say what changed.
        for f in ("data", "stringData"):
            if f in r:
                r[f] = {k: "<redacted>" for k in r[f] or {}}
    return r


def dump(r: dict) -> str:
    return yaml.safe_dump(r, sort_keys=True, default_flow_style=False)


@dataclasses.dataclass
class AppDiff:
    app: str
    status: str                      # added | removed | changed | failed (after) | repaired (before failed)
    added: list[str] = dataclasses.field(default_factory=list)
    removed: list[str] = dataclasses.field(default_factory=list)
    changed: dict[str, str] = dataclasses.field(default_factory=dict)   # rid -> unified diff

    @property
    def size(self) -> int:
        return len(self.added) + len(self.removed) + len(self.changed)


def compare(before: Rendering, after: Rendering) -> list[AppDiff]:
    out = []
    for name in sorted(set(before.apps) | set(after.apps)):
        b = {rid(r, dest_ns(before.apps[name])): normalize(r) for r in before.apps[name].resources} \
            if name in before.apps else None
        a = {rid(r, dest_ns(after.apps[name])): normalize(r) for r in after.apps[name].resources} \
            if name in after.apps else None
        if name in after.apps and after.apps[name].error:
            out.append(AppDiff(name, "failed"))
            continue
        if b is None:
            out.append(AppDiff(name, "added", added=sorted(a)))
            continue
        if before.apps[name].error:
            out.append(AppDiff(name, "repaired", added=sorted(a)))
            continue
        if a is None:
            out.append(AppDiff(name, "removed", removed=sorted(b)))
            continue
        d = AppDiff(name, "changed", added=sorted(set(a) - set(b)), removed=sorted(set(b) - set(a)))
        for k in sorted(set(a) & set(b)):
            if a[k] != b[k]:
                d.changed[k] = "".join(difflib.unified_diff(
                    dump(b[k]).splitlines(keepends=True), dump(a[k]).splitlines(keepends=True),
                    fromfile=f"before/{k}", tofile=f"after/{k}", n=3))
        if d.size:
            out.append(d)
    return out
