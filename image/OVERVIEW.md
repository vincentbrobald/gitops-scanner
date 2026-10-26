# gitops-scanner

`gitops-check`: the merge checks of a GitOps repository deployed by
[Argo CD](https://argo-cd.readthedocs.io/). Each pull request is rendered
**before** (its target branch) and **after** (its head) with Argo CD's own
rendering code, run in process (argocd-render), the output is checked, and the per-resource differences are posted
on the pull request. Any error fails the commit status, which the
repository can require to merge.

The rendering is Argo CD's own (the same version, kustomize and Helm as the
clusters'): gitops-check decides what to submit, follows what comes back
(Applications, ApplicationSets, app-of-apps) and judges the result.

## What this image is

The `gitops-check` command (Python) with the tools it calls:
argocd-render (Argo CD's manifest generation and ApplicationSet generators,
built from Argo CD's source), Argo CD's versions of
[kustomize](https://kustomize.io/) and [Helm](https://helm.sh/),
[kubeconform](https://github.com/yannh/kubeconform) (schema validation),
[sops](https://github.com/getsops/sops) (encrypting generated Secrets with
public age keys only), git and ssh-keygen. Alpine based, runs as a non-root
user; the entry point is `gitops-check`. The licenses of everything in it
are under `/licenses`.

## Use

In a CI pipeline (e.g. Pipelines-as-Code on Tekton), on each pull request,
from a clone holding both revisions:

```sh
docker run --rm -v "$PWD":/repo -e GIT_CONFIG_COUNT=1 -e GIT_CONFIG_KEY_0=safe.directory -e GIT_CONFIG_VALUE_0='*' \
  docker.io/vincentbrobald/gitops-scanner:latest \
  --repo /repo --base origin/main --head HEAD --report report.md
```

Run in a pod, it gives Helm the cluster's version and API versions. With
`--server` (token: `$ARGOCD_TOKEN`), a render-only Argo CD renders instead.
What that Argo CD must provide, the checks, the configuration
(`.gitops-check.yaml`) and every option:
[documentation](https://github.com/vincentbrobald/gitops-scanner#documentation).

## Tags

- `<commit>`: one per commit of the main branch (7 characters); pin one of
  these.
- `latest`: the newest of them.

## Source and licenses

Source: [github.com/vincentbrobald/gitops-scanner](https://github.com/vincentbrobald/gitops-scanner),
under the MIT License. The tools and packages in the image keep their own
licenses (kubeconform Apache-2.0, sops MPL-2.0, git GPL-2.0, ...): their
texts, the source of the MPL-2.0 Go modules and the list of the Alpine
packages with their licenses are under `/licenses`; the Alpine packages'
sources are Alpine's [aports](https://gitlab.alpinelinux.org/alpine/aports).
