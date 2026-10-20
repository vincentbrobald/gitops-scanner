"""Which kinds are cluster-scoped: a resource without a namespace lands in its
Application's destination namespace, unless its kind has none.

From the cluster's discovery API when running in a pod (any service account
may read it, and it knows every CRD installed), else a built-in list of the
common ones (local runs, tests). Either way the built-in list is added, and
the answer is computed once per process.

Getting this wrong only blurs the duplicate check and the diff: a
cluster-scoped kind taken for namespaced would get a namespace in its identity.
"""
from __future__ import annotations

import json
import ssl
import urllib.request
from pathlib import Path

SA = Path("/var/run/secrets/kubernetes.io/serviceaccount")
API = "https://kubernetes.default.svc"

BUILTIN = {
    ("", "Namespace"), ("", "Node"), ("", "PersistentVolume"), ("", "ComponentStatus"),
    ("rbac.authorization.k8s.io", "ClusterRole"), ("rbac.authorization.k8s.io", "ClusterRoleBinding"),
    ("apiextensions.k8s.io", "CustomResourceDefinition"), ("apiregistration.k8s.io", "APIService"),
    ("storage.k8s.io", "StorageClass"), ("storage.k8s.io", "CSIDriver"), ("storage.k8s.io", "VolumeAttachment"),
    ("scheduling.k8s.io", "PriorityClass"), ("node.k8s.io", "RuntimeClass"),
    ("networking.k8s.io", "IngressClass"), ("gateway.networking.k8s.io", "GatewayClass"),
    ("admissionregistration.k8s.io", "ValidatingWebhookConfiguration"),
    ("admissionregistration.k8s.io", "MutatingWebhookConfiguration"),
    ("admissionregistration.k8s.io", "ValidatingAdmissionPolicy"),
    ("admissionregistration.k8s.io", "ValidatingAdmissionPolicyBinding"),
    ("cert-manager.io", "ClusterIssuer"), ("cilium.io", "CiliumClusterwideNetworkPolicy"),
    ("cilium.io", "CiliumLoadBalancerIPPool"), ("cilium.io", "CiliumL2AnnouncementPolicy"),
    ("tuppr.home-operations.com", "TalosUpgrade"), ("tuppr.home-operations.com", "KubernetesUpgrade"),
}

_cache: set[tuple[str, str]] | None = None   # (group, kind); "" is the core group


def _get(path: str, token: str, ctx: ssl.SSLContext) -> dict:
    """GET a discovery path of the cluster's API with the pod's token."""
    req = urllib.request.Request(API + path, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=10, context=ctx) as r:
        return json.loads(r.read())


def discover() -> set[tuple[str, str]] | None:
    """The cluster-scoped kinds from discovery (preferred version of each
    group); None outside a pod or on any failure."""
    if not (SA / "token").is_file():
        return None
    try:
        token = (SA / "token").read_text().strip()
        ctx = ssl.create_default_context(cafile=str(SA / "ca.crt"))
        versions = ["/api/v1"] + [f"/apis/{g['preferredVersion']['groupVersion']}"
                                  for g in _get("/apis", token, ctx)["groups"]]
        scoped = set()
        for v in versions:
            group = v.split("/")[2] if v.startswith("/apis/") else ""
            for res in _get(v, token, ctx).get("resources", []):
                if not res.get("namespaced") and "/" not in res["name"]:
                    scoped.add((group, res["kind"]))
        return scoped
    except (OSError, ValueError, KeyError):
        return None


def cluster_scoped() -> set[tuple[str, str]]:
    """The known cluster-scoped (group, kind) pairs, computed once."""
    global _cache
    if _cache is None:
        _cache = (discover() or set()) | BUILTIN
    return _cache


def is_cluster_scoped(r: dict) -> bool:
    """Whether a resource's kind has no namespace."""
    return (r.get("apiVersion", "").rpartition("/")[0], r.get("kind", "")) in cluster_scoped()
