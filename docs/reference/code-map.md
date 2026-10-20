# Code map

> Reference · [Documentation](../../README.md#documentation)

| File | Role |
|---|---|
| `gitops_check/__main__.py` | Command line and the run's order (prepare, render, check, diff, clean up, report) |
| `gitops_check/config.py` | `.gitops-check.yaml` |
| `gitops_check/render.py` | The walk and the rewriting; the `Rendering` the rest works on |
| `gitops_check/argocd.py` | REST client of the preview |
| `gitops_check/checks.py` | The checks: each returns (errors, warnings) |
| `gitops_check/kinds.py` | Which kinds are cluster-scoped (discovery in a pod, else a built-in list) |
| `gitops_check/diff.py` | Normalisation and the per-resource diff |
| `gitops_check/report.py` | The Markdown report (whole, or cut for a comment) |
| `gitops_check/forgejo.py` | The pull request comment |
| `Containerfile` | The image: kubeconform and sops built from their modules, the package, `/licenses` |
| `image/python-licenses.py` | Copies the installed Python packages' license files into `/licenses` (image build) |
| `image/OVERVIEW.md` | The image's overview on Docker Hub |
