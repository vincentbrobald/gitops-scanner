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
- Coverage must stay at or above `fail_under` in `pyproject.toml` (85%).
- **Adding a check**: a function in `checks.py` returning `(errors, warnings)`,
  called in `__main__.main`, with a test; document it in
  [Checks](../reference/checks.md) and in `checks.py`'s docstring.

`.tekton/build.yaml` runs the same on every pull request and push (tools pinned
in `requirements-test.txt`), builds the image with kaniko (its last step runs
the image's entry point and tools: a smoke test), scans it with Trivy (fails on
fixable HIGH/CRITICAL; the image is Python on Alpine plus git, kubeconform, sops and ssh-keygen (the last two for the gitops repository's generators),
where Debian's slim base carried unfixed ones),
and on `main` pushes `gitops-scanner/gitops-check:<commit>`
and `:latest` to the site's registry. The gitops repository's pipeline runs
`:latest`, pulled each time: a merge here applies to the next check there.
The same image is published as `docker.io/vincentbrobald/gitops-scanner`,
tags `<commit>` (7 characters) and `latest`, with `image/OVERVIEW.md` as its
overview there.

The image carries the licenses of what it contains under `/licenses`: this
repository's, kubeconform's and sops' with their dependencies' (go-licenses,
in their build stages; the source of MPL-2.0 modules too), the Python
packages' license files (`image/python-licenses.py`) and the list of the
Alpine packages with their licenses (`apk list --installed`). Its labels
`org.opencontainers.image.source` and `.licenses` name the public repository
and MIT.
