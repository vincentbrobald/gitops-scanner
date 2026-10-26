# Develop and release

> How-to · [Documentation](../../README.md#documentation)

Test a change, add a check, and get it to the gitops repository's pipeline.

## Test

```sh
pip install -r requirements-test.txt && ruff check . && python -m pytest --cov
```

- `tests/test_gitops_check.py`: the units (configuration, rewriting, the walk
  against a fake Argo CD, the checks, the diff, the report).
- `tests/test_integration.py`: whole runs of `gitops-check` against real git
  repositories (passing, failing and outdated pull requests, a report larger
  than a comment), served by a fake Argo CD HTTP server that renders
  directory sources from git at the pinned revision; the Argo CD and Forgejo
  clients over HTTP; kubeconform's results mapped back to Applications. Needs
  `git`; no cluster.
- `test_main_renders_in_process` runs only with `argocd-render` on `PATH`
  (and Argo CD's `kustomize`, `helm`, `git-verify-wrapper.sh`); it is skipped otherwise.
- `argocd-render/main_test.go`: the Go helper, against a temporary git
  repository. It builds inside Argo CD's source, as the image does:

  ```sh
  git clone -q --depth 1 --branch v3.5.5 https://github.com/argoproj/argo-cd.git /tmp/argo-cd
  mkdir -p /tmp/argo-cd/cmd/argocd-render && cp argocd-render/*.go /tmp/argo-cd/cmd/argocd-render/
  (cd /tmp/argo-cd && go vet ./cmd/argocd-render && go test ./cmd/argocd-render)
  ```

  The image build runs the same, so a failing Go test fails the build.
- Coverage must stay at or above `fail_under` in `pyproject.toml` (85%).
- **A notable change** (behaviour, options, configuration, the image's
  contents) adds a line under `Unreleased` in `CHANGELOG.md`; the section
  is headed by its merge's time, `YYYY-MM-DD HH:MM UTC`.
- **Adding a check**: a function in `checks.py` returning `(errors, warnings)`,
  called in `__main__.main`, with a test; document it in
  [Checks](../reference/checks.md) and in `checks.py`'s docstring.

`.tekton/build.yaml` runs the same on every pull request and push (tools pinned
in `requirements-test.txt`), builds the image with kaniko (its last step runs
the image's entry point and tools: a smoke test), scans it with Trivy (fails on
fixable HIGH/CRITICAL; the image is Python on Alpine plus git, argocd-render, kustomize, helm, kubeconform, sops and ssh-keygen (the last two for the gitops repository's generators),
where Debian's slim base carried unfixed ones),
and on `main` pushes `gitops-scanner/gitops-check:<commit>`
and `:latest` to the site's registry. The gitops repository's pipeline runs
`:latest`, pulled each time: a merge here applies to the next check there.
The same image is published as `docker.io/vincentbrobald/gitops-scanner`,
tags `<commit>` (7 characters) and `latest`, with `image/OVERVIEW.md` as its
overview there. To build and host it yourself instead: the gitops
repository's how-to [Build and host the images
yourself](https://github.com/vincentbrobald/gitops/blob/main/docs/how-to/build-and-host-the-images.md).

The image carries the licenses of what it contains under `/licenses`: this
repository's, argocd-render's, kustomize's, helm's, kubeconform's and sops' with their dependencies' (go-licenses,
in their build stages; the source of MPL-2.0 modules too), the Python
packages' license files (`image/python-licenses.py`) and the list of the
Alpine packages with their licenses (`apk list --installed`). Its labels
`org.opencontainers.image.source` and `.licenses` name the public repository
and MIT.
