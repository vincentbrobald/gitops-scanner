# gitops-scanner

`gitops-check`: the merge-request checks of a GitOps repository deployed
by Argo CD. A pull request is rendered **before** (its target
branch) and **after** (its head) by a render-only Argo CD, the output is
checked, and the per-resource differences are reported on the pull request.
Any error fails the commit status, which the repository requires to merge.

```
pull request ──webhook──> Pipelines-as-Code ──> PipelineRun (gitops-ci namespace, this image)
                                                  │ Applications, ApplicationSets (rewritten)
                                                  v
                              argocd-preview: Argo CD API server + repo-server only
                              (no controller: renders, never syncs; ksops stand-in: never decrypts)
```

The rendering is Argo CD's own (same version as the clusters', same bundled
kustomize and Helm): nothing here templates a chart or builds a kustomization.
gitops-check decides what to submit, in which form, follows what comes back,
and judges the result.

## Documentation

The documentation follows [Diátaxis](https://diataxis.fr/): how-to guides for
tasks, reference to look things up, explanation to understand. To see the
check at work on a pull request: the gitops repository's tutorial
[Your first platform change](https://github.com/vincentbrobald/gitops/blob/main/docs/tutorials/your-first-platform-change.md).

**How-to guides**
- [Run it locally](docs/how-to/run-it-locally.md): a check by hand, against the preview
- [Troubleshoot](docs/how-to/troubleshoot.md): a failed or missing check
- [Develop and release](docs/how-to/develop.md): tests, a new check, the image

**Reference**
- [Checks](docs/reference/checks.md): what blocks, what is only reported, the diff
- [Configuration](docs/reference/configuration.md): `.gitops-check.yaml`
- [Command line](docs/reference/command-line.md): options, environment, exit status
- [The preview](docs/reference/preview.md): what the render-only Argo CD must provide
- [Code map](docs/reference/code-map.md): what each module does

**Explanation**
- [Design decisions](docs/explanation/design-decisions.md): the choices, the alternatives considered, when to revisit them
- [How it works](docs/explanation/how-it-works.md): one run step by step, why it can't deploy, its limits

## License

MIT (LICENSE), © 2026 Vincent Brobald.
