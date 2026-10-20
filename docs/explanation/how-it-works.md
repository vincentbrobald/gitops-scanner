# How it works

> Explanation · [Documentation](../../README.md#documentation)

What one run of `gitops-check` does, in order, and why it can't deploy
anything. The checks themselves: [Checks](../reference/checks.md).

## A run

1. **Prepare.** Resolve `--base` and `--head` to commits and check each out
   in a worktree of the clone. Read `.gitops-check.yaml` at the head (a pull
   request that adds an entry point is checked with it). Error if the head
   doesn't contain the base: "after" must be what the merge gives. Delete
   Applications that runs which died left in the preview.
2. **Render each revision** (`render.py`), starting from the entry points and
   following exactly what Argo CD follows:

   ```
   queue = [stand-in Application for the entry point's directory]
   while queue:
       app = queue.pop(0)
       skip it if its key was rendered already   # an app-of-apps lists itself; key: name, or namespace/name outside argocd
       skip it if all its sources are on external repositories (not rendered)
       rewrite → submit (POST /applications?validate) → read back (GET .../manifests)
       Application in the output    → queue it
       ApplicationSet in the output → generate it once (POST /applicationsets/generate), queue its Applications
   ```

   What is submitted is rewritten so that it renders and can never deploy:

   | Object | Rewritten |
   |---|---|
   | Application | run-specific name (`<run>-b-<key>`, `<run>-a-<key>`, the key's `/` as `-`; hashed past 63 characters) in namespace `argocd-preview`; labels `gitops-check/run` and `gitops-check/app` (the original name); no `syncPolicy`, `operation`, `status`, finalizers; project `default` (ApplicationSet templates too; the written project is kept for the projects check); **sources on this repository tracking a branch of `trackedRevisions` pinned to the commit** (other repositories, tags and charts as written) |
   | ApplicationSet | namespace `argocd-preview`; **git generators on this repository pinned to the commit**, nested `matrix`/`merge` included; its generated Applications are rewritten like any other |

   When a real Application deploys an entry point's own directory, the
   stand-in is dropped, so nothing counts twice. Argo CD's errors are kept
   per Application; the walk goes on.
3. **Check "after"** (`checks.py`): errors block the merge, warnings are
   only reported ([Checks](../reference/checks.md)).
4. **Diff** (`diff.py`), per Application and resource, after removing the
   noise: Argo CD's tracking annotation; Secret values (compared by key names
   and the fingerprint annotation of the ksops stand-in, never by value);
   Helm test hooks (Argo CD never runs them). Each Application is **added**,
   **removed**, **changed**, **failed** (doesn't render after) or **repaired**
   (didn't render before).
5. **Clean up** (always, even after an error): delete every Application of
   the run (`cascade=false`), remove the worktrees.
6. **Report** (`report.py`): print the whole report (the pipeline's log),
   write it to `--report`, and post it on the pull request: one comment,
   found by a hidden marker and updated by each run, cut to what a Forgejo
   comment holds (the log keeps everything).

## Why it is safe

- Pull requests can't change the check that gates them: the PipelineRun comes
  from the repository's default branch (`pipelinerun_provenance: default_branch`).
- Nothing is decrypted anywhere in the check; Secret values never appear in
  the report.
- Everything submitted to the preview is rewritten so that it can't deploy,
  and the preview couldn't anyway (no controller, no cluster-wide roles).
- The preview's `ci` token can only touch Applications in the preview.

## Limits

- The revisions must exist on Forgejo (the preview's repo-server fetches
  them): pull requests from forks are not supported.
- sops re-encrypts every value of a file it edits: an edited Secret shows all
  its keys as changed.
- A typo in an Application's path also marks the directories it used to reach
  as unreached: several errors for one mistake.
- kube-prometheus-stack-sized diffs exceed a comment; the log has them.
