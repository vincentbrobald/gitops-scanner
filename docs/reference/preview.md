# The preview

> Reference · [Documentation](../../README.md#documentation)

What the render-only Argo CD must provide.

`argocd-preview` (gitops: [`clusters/lab/addons/argocd-preview.yaml`](https://github.com/vincentbrobald/gitops/blob/main/clusters/lab/addons/argocd-preview.yaml), [`base/argocd-preview/`](https://github.com/vincentbrobald/gitops/blob/main/base/argocd-preview)):

- **The clusters' Argo CD version** (same chart): the bundled kustomize and
  Helm decide the output. Upgrade both together.
- **No application or ApplicationSet controller**: nothing reconciles, nothing
  syncs. That's why the CLI's `app manifests` can't be used (it waits for a
  controller refresh) and the REST API is (`argocd.py` lists the endpoints).
- **A ksops stand-in** instead of ksops: it renders each SOPS-encrypted Secret
  with its key names and a fingerprint per value (also in the annotation
  `preview.argocd/fingerprints`, as the API masks values); nothing is decrypted.
- **Repository credentials** for this repository (a read-only deploy key, as
  a credential template for the Forgejo URL prefix), and the clusters of
  `.gitops-check.yaml` with their labels.
- **An account `ci`** whose token may create, read, update and delete
  Applications and generate ApplicationSets (gitops-ci Secret `argocd-preview`).
