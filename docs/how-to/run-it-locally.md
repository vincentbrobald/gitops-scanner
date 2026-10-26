# Run it locally

> How-to · [Documentation](../../README.md#documentation)

Run a check by hand, outside the pipeline.

With the image, which renders in process (argocd-render): only the clone,
holding both revisions, is needed; nothing is pushed or fetched from Forgejo
(charts still come from their repositories):

```sh
podman run --rm -v ../gitops:/repo:Z -e GIT_CONFIG_COUNT=1 -e GIT_CONFIG_KEY_0=safe.directory -e GIT_CONFIG_VALUE_0='*' \
    docker.io/vincentbrobald/gitops-scanner:latest \
    --repo /repo --base origin/main --head my-branch --no-schemas
```

The image runs as uid 65532: the `GIT_CONFIG_*` variables tell its git to
accept a clone another user owns (otherwise "detected dubious ownership").

Outside a pod, Helm sees its default Kubernetes version and no cluster API
versions, and the cluster-scoped kinds are the built-in list's: a chart
that tests `.Capabilities`, or a CRD's cluster-scoped kind, can differ from
the pipeline's run.

Against the preview through a port-forward, with any clone holding both
revisions (the preview's repo-server fetches them from Forgejo: push the
branch first):

```sh
kubectl -n argocd-preview port-forward svc/argocd-preview-server 18080:80 &
export ARGOCD_TOKEN=...      # argocd account generate-token --account ci (on the preview)
python -m gitops_check --repo ../gitops --base origin/main --head origin/my-branch \
    --server http://localhost:18080 --report report.md
```

Or the preview with the image: `podman run --rm --network host -e ARGOCD_TOKEN -v ../gitops:/repo:Z
-e GIT_CONFIG_COUNT=1 -e GIT_CONFIG_KEY_0=safe.directory -e GIT_CONFIG_VALUE_0='*' docker.io/vincentbrobald/gitops-scanner:latest --repo /repo --base ... --head ...`.
