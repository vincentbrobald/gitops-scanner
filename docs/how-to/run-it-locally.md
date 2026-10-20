# Run it locally

> How-to · [Documentation](../../README.md#documentation)

Run a check by hand, outside the pipeline.

Against the preview through a port-forward, with any clone holding both
revisions (the preview's repo-server fetches them from Forgejo: push the
branch first):

```sh
kubectl -n argocd-preview port-forward svc/argocd-preview-server 18080:80 &
export ARGOCD_TOKEN=...      # argocd account generate-token --account ci (on the preview)
python -m gitops_check --repo ../gitops --base origin/main --head origin/my-branch \
    --server http://localhost:18080 --report report.md
```

Or with the image: `podman run --rm --network host -e ARGOCD_TOKEN -v ../gitops:/repo:Z
registry.example.org/gitops-scanner/gitops-check:latest --repo /repo --base ... --head ...`.
