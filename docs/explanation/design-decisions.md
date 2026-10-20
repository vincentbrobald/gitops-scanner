# Design decisions

> Explanation · [Documentation](../../README.md#documentation)

The choices gitops-check is built on, what else was considered, and when to
look at them again. Why the GitOps repository has merge checks at all: the
gitops repository's [Rendering before merging](https://github.com/vincentbrobald/gitops/blob/main/docs/explanation/merge-checks.md).

### Argo CD renders, not a reimplementation

**Decision.** Every Application and ApplicationSet is rendered by a real Argo
CD of the clusters' version; gitops-check only decides what to submit,
follows what comes back, and judges it.
**Why.** The bundled kustomize and Helm, the generators' templating, the
multi-source and plugin handling are exactly production's, so "renders here"
means "renders on the cluster".
**Alternatives.** Running `kustomize build` and `helm template` ourselves:
version skew and every Argo CD feature to reimplement (generators,
`valueFiles` from other sources, KSOPS). `argocd app diff` against the live
cluster: needs cluster access from CI and only covers Applications that
already exist.
**Revisit if** Argo CD ships a supported offline render of a whole
app-of-apps tree.

### A render-only Argo CD with no controller

**Decision.** `argocd-preview` runs only the API server and the repo-server.
**Why.** Nothing it receives can ever sync: no controller reconciles, and its
account may only manage Applications in its own namespace.
**Consequence.** The REST API is used, not the CLI: `argocd app manifests`
waits for a controller refresh that never comes.

### Rewrite everything submitted

**Decision.** Applications get run-specific names in the preview's namespace,
lose sync policies, operations and finalizers, are put in project `default`,
and sources on the repository are pinned to the commit under test.
**Why.** Two revisions render side by side without collisions, nothing can
deploy, and "before" and "after" really are two commits rather than whatever
the branch points at.

### A ksops stand-in: never decrypt

**Decision.** The preview's KSOPS renders each encrypted Secret with its key
names and a fingerprint per value.
**Why.** CI never holds a decryption key, values never reach a report, and a
diff still shows a key added, removed or changed.

### Follow what Argo CD follows, and report the rest

**Decision.** The walk starts at the configured entry points and follows
Applications and ApplicationSets; directories with manifests nothing reaches
are errors.
**Why.** A file nothing reaches isn't checked, so silence would hide typos in
paths and dead content.

### Errors block, diffs inform

**Decision.** Only errors fail the commit status; diffs are posted for the
reviewer.
**Why.** Whether a change works is the check's job; whether it is wanted is
the reviewer's.

### Projects checked from the rendering itself

**Decision.** Since the preview renders everything in `default`, each
Application is compared with the AppProject found in the same rendering.
**Why.** Allow-list gaps fail the pull request instead of the sync.

### Keys by namespace and name; external repositories listed, not rendered

**Decision.** Applications outside `argocd` are keyed `namespace/name`;
Applications whose sources are all on external repositories (the teams') are
listed but not rendered.
**Why.** Several teams' Argo CDs each have a `root`; the preview may not read
teams' repositories, and a platform change doesn't touch them.

### Run from `main`

**Decision.** The gitops repository runs the PipelineRun from its default
branch (`pipelinerun_provenance: default_branch`), never from the pull
request.
**Why.** A pull request can't change the check that gates it, nor read its
token.

### The image follows `:latest`

**Decision.** The gitops pipeline pulls `gitops-check:latest` on each run.
**Why.** A merge here applies to the next check there, without a second
change. **Cost.** A broken image breaks every check at once; this
repository's own CI (tests, smoke test of the image, Trivy) is the guard.
**Revisit if** reproducible checks matter more than immediacy: pin a digest.

### Python on Alpine

**Decision.** The image is Python on Alpine plus git and kubeconform.
**Why.** The Debian slim base carried unfixed HIGH/CRITICAL vulnerabilities
that the build's Trivy gate refuses; Alpine had none.

### One comment, updated

**Decision.** The report is one pull request comment, found by a hidden marker
and updated by each run, cut to Forgejo's size; the pipeline's log keeps the
whole report.
**Why.** The pull request stays readable across many pushes.

### Limits accepted

Pull requests from forks aren't supported (the preview fetches revisions from
Forgejo); Crossplane's kinds have no schemas, so their fields are checked only
at sync.
