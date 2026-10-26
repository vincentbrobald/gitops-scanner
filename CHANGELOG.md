# Changelog

Notable changes to gitops-check and its image, newest first. Every commit of
the main branch is published (image tags `<commit>` and `latest`), so a
section is headed by the time its changes reached the main branch (UTC), not
a version. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Changes before
2026-10-26 13:27 UTC: the git history.

## Unreleased

### Fixed

- Docs: the commands that run the image on a local clone failed with
  "detected dubious ownership" (the image runs as uid 65532); they now set
  git's `safe.directory` through `GIT_CONFIG_*`.
- Docs: the image's tools and licenses, the Docker Hub overview and the
  preview's reference describe rendering in process.

## 2026-10-26 13:27 UTC

### Added

- argocd-render: Argo CD's manifest generation and ApplicationSet
  generators (v3.5.5), run in process. It is the default renderer: no
  render-only Argo CD, no token, nothing left behind by a dead run.
- `.gitops-check.yaml`: `argocd.cm`, the clusters' `argocd-cm` settings
  that change rendering (`kustomize.buildOptions`...); cluster `labels` and
  `annotations`, for the ApplicationSet cluster generator.
- In a pod, Helm's `.Capabilities` come from the pod's cluster (version and
  API versions).

### Changed

- `--server` (or `$ARGOCD_SERVER`) is optional: with it, a render-only
  Argo CD renders, as before.
- The image builds kustomize v5.8.1 and Helm v4.2.1 (Argo CD v3.5.5's
  versions) from source with a current Go, instead of copying Argo CD's
  binaries, which carried fixed HIGH and CRITICAL vulnerabilities. Their
  licenses are under `/licenses`. The image is about 100 MB larger.

### Limits

- Only the list, clusters, git, matrix and merge ApplicationSet generators
  work.
- An Argo CD upgrade is a rebuild, sometimes a code change (argocd-render
  uses Argo CD's internal packages).
