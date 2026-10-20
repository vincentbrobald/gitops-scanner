"""Per-resource diff between two renderings, after removing the noise.

Applications are matched by their original name, resources within an
Application by their identity (checks.rid: group/kind, namespace, name).
Noise removed before comparing:
- the tracking annotation Argo CD adds, which names the preview Application
  (so it differs between before and after for every resource);
- Secret values, which Argo CD masks anyway: a Secret is compared by its key
  names and the fingerprint annotation of the ksops stand-in, so an edited
  encrypted value shows as a changed fingerprint, never as a value.
Helm test hooks were already left out when rendering (render.is_helm_test).
"""
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
    """A copy of a resource without the noise (see the module docstring)."""
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
    """Stable YAML for diffing: sorted keys, block style."""
    return yaml.safe_dump(r, sort_keys=True, default_flow_style=False)


@dataclasses.dataclass
class AppDiff:
    """What changed in one Application between before and after."""
    app: str
    status: str                      # added | removed | changed | failed (after) | repaired (before failed)
    added: list[str] = dataclasses.field(default_factory=list)
    removed: list[str] = dataclasses.field(default_factory=list)
    changed: dict[str, str] = dataclasses.field(default_factory=dict)   # rid -> unified diff

    @property
    def size(self) -> int:
        """How many resources changed in any way; 0: not worth reporting."""
        return len(self.added) + len(self.removed) + len(self.changed)


def compare(before: Rendering, after: Rendering) -> list[AppDiff]:
    """The Applications that differ, sorted by name. Status, in this order of
    precedence: failed (it doesn't render after; the error is reported by the
    checks), added, repaired (it didn't render before), removed, changed
    (resources added, removed, or with a unified diff each)."""
    out = []
    for name in sorted(set(before.apps) | set(after.apps)):
        b = {rid(r, dest_ns(before.apps[name])): normalize(r) for r in before.apps[name].resources} \
            if name in before.apps else None
        a = {rid(r, dest_ns(after.apps[name])): normalize(r) for r in after.apps[name].resources} \
            if name in after.apps else None
        if name in after.apps and after.apps[name].error:
            out.append(AppDiff(name, "failed"))       # the error says why; no resource to compare
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
