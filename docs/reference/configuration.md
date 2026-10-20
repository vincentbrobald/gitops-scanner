# Configuration: `.gitops-check.yaml`

> Reference · [Documentation](../../README.md#documentation)

At the root of the checked repository (read at the pull request's head):

```yaml
apiVersion: gitops-check.brobald.be/v1alpha1
kind: CheckConfig
repository:
  url: ssh://git@git.example.org:2222/platform/gitops.git   # as Applications write repoURL
  trackedRevisions: [main, HEAD]       # pinned to the commit under test
argocd:
  version: v3.5.5                      # the Argo CD the preview must run (informational)
clusters:                              # as registered in the preview (name, server, labels)
  - name: lab
    server: https://kubernetes.default.svc
    labels: {env: lab}
entrypoints:                           # where the walk starts
  - {name: lab, cluster: lab, path: clusters/lab/addons}
  - {name: samples, cluster: lab, path: samples/applicationsets, deployed: false}
ignore:                                # directories no entry point reaches, on purpose
  - {path: .tekton, reason: pipelines of this repository}
external:                              # repositories outside the check
  - {prefix: "ssh://git@forge:2222/tenants/", reason: the teams' repositories}
```

| Key | Required | Meaning |
|---|---|---|
| `repository.url` | yes | This repository's URL as Applications name it; compared ignoring case, a trailing `/` and `.git` (an `https://` URL never matches an `ssh://` one) |
| `repository.trackedRevisions` | no (`[HEAD]`) | Revisions that mean "the branch": sources and git generators on this repository with one of them are pinned to the commit under test |
| `argocd.version` | no | Documentation: the preview must run the clusters' Argo CD, or the diff shows false changes. Not compared at run time |
| `clusters[].name`, `.server` | yes, per entry point's cluster | The stand-in Applications deploy to `server`. The preview registers the same clusters (with the same labels, for cluster generators) |
| `entrypoints[].name`, `.cluster`, `.path` | yes | A directory of manifests (Applications, ApplicationSets, or a kustomization producing them); the walk starts there |
| `entrypoints[].deployed` | no (`true`) | Informational: `false` marks examples nothing syncs |
| `entrypoints[].recurse` | no (`false`) | Read subdirectories too (`directory.recurse`), as the root Application that syncs the directory does |
| `ignore[].path`, `.reason` | no | Directories exempt from the "unreached" check, and everything below them |
| `external[].prefix`, `.reason` | no | Repository URL prefixes (case ignored) outside the check: an Application whose sources are all there (e.g. a team's own app-of-apps on the team's repository) is listed but not rendered, as the preview may not read those repositories and a change here doesn't touch them |

A new cluster folder means a new entry point. A file that is not reached from
an entry point is not checked at all, beyond the "unreached" error.

Keys the scanner doesn't read are ignored: a `checks:` block is
informational (the scanner always redacts Secrets and blocks on errors), and
clusters' `labels` and `annotations` describe how the preview registers them
([The preview](preview.md)), which is configured there.

