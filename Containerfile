# Image of gitops-check, the merge-request checks of a gitops repository,
# built by .tekton/build.yaml (kaniko) and pushed to the site's registry as
# gitops-scanner/gitops-check:<commit> and :latest; published as
# docker.io/vincentbrobald/gitops-scanner. It carries the licenses of what it
# contains under /licenses.
#   podman build -t gitops-check . && podman run --rm gitops-check --help
# BASE, GO_BASE: the CI passes the same images through the registry's
# pull-through mirror.
# BASE is Alpine: Debian's slim image carried ~50 HIGH findings in system
# packages without fixes (util-linux, expat, curl...); Alpine has far fewer
# packages and fixes them quickly.
ARG BASE=docker.io/library/python:3.14.7-alpine3.24
ARG GO_BASE=docker.io/library/golang:1.26.10-trixie
ARG GO_LICENSES=github.com/google/go-licenses/v2@v2.0.1
# The clusters' Argo CD (the gitops repository's .gitops-check.yaml,
# argocd.version): argocd-render is built from its source, the
# repo-server's git wrappers come from its image, and kustomize and helm are
# its versions (hack/tool-versions.sh), built from their modules.
ARG ARGOCD_VERSION=v3.5.5
ARG ARGOCD_IMAGE=quay.io/argoproj/argocd:${ARGOCD_VERSION}

# kubeconform validates the rendered resources. Built from its release's
# module (checksum-verified by Go's sum database) with a current Go: the
# release binaries embed the Go of their day, whose fixed vulnerabilities the
# scan rightly refuses. Dependencies raised for the same reason are listed in
# KUBECONFORM_DEPS.
FROM ${GO_BASE} AS kubeconform
ARG KUBECONFORM_VERSION=v0.8.0
ARG KUBECONFORM_DEPS="golang.org/x/text@v0.41.0"
ENV GOTOOLCHAIN=local CGO_ENABLED=0
WORKDIR /src
RUN go mod download github.com/yannh/kubeconform@${KUBECONFORM_VERSION} \
    && cp -r "$(go env GOMODCACHE)/github.com/yannh/kubeconform@${KUBECONFORM_VERSION}/." . \
    && chmod -R u+w . \
    && go get ${KUBECONFORM_DEPS} && go mod tidy \
    && go build -trimpath -ldflags "-s -w -X main.version=${KUBECONFORM_VERSION}" \
         -o /kubeconform ./cmd/kubeconform \
    && /kubeconform -v
# Its license and its dependencies' (go-licenses: the texts, and the source of
# MPL-2.0 modules).
ARG GO_LICENSES
RUN go run ${GO_LICENSES} save ./cmd/kubeconform --save_path=/licenses/kubeconform

# sops (v3.13.3, 2026-07-23): the gitops repository's merge check encrypts
# the Secrets its generators make (tools/teams.py, tools/ci.py), with the
# recipients' public age keys only. Built from its module like kubeconform.
# Dependencies raised for fixed vulnerabilities: SOPS_DEPS.
FROM ${GO_BASE} AS sops
ARG SOPS_VERSION=v3.13.3
ARG SOPS_DEPS="golang.org/x/crypto@v0.57.0 golang.org/x/net@v0.60.0 google.golang.org/grpc@v1.83.2"
ENV GOTOOLCHAIN=local CGO_ENABLED=0
WORKDIR /src
RUN go mod download github.com/getsops/sops/v3@${SOPS_VERSION} \
    && cp -r "$(go env GOMODCACHE)/github.com/getsops/sops/v3@${SOPS_VERSION}/." . \
    && chmod -R u+w . \
    && go get ${SOPS_DEPS} && go mod tidy \
    && go build -trimpath -ldflags "-s -w" -o /sops ./cmd/sops \
    && /sops --version --disable-version-check
ARG GO_LICENSES
RUN go run ${GO_LICENSES} save ./cmd/sops --save_path=/licenses/sops

# helm and kustomize: the versions Argo CD runs, built from their modules
# like kubeconform (its image's binaries embed an older Go). Dependencies
# raised for fixed vulnerabilities: HELM_DEPS, KUSTOMIZE_DEPS.
FROM ${GO_BASE} AS helm
ARG HELM_VERSION=v4.2.1
ARG HELM_DEPS="golang.org/x/crypto@v0.57.0 golang.org/x/net@v0.60.0 oras.land/oras-go/v2@v2.6.2"
ENV GOTOOLCHAIN=local CGO_ENABLED=0
WORKDIR /src
RUN go mod download helm.sh/helm/v4@${HELM_VERSION} \
    && cp -r "$(go env GOMODCACHE)/helm.sh/helm/v4@${HELM_VERSION}/." . \
    && chmod -R u+w . \
    && go get ${HELM_DEPS} && go mod tidy \
    && go build -trimpath -ldflags "-s -w -X helm.sh/helm/v4/internal/version.version=${HELM_VERSION}" \
         -o /helm ./cmd/helm \
    && /helm version --short
ARG GO_LICENSES
RUN go run ${GO_LICENSES} save ./cmd/helm --save_path=/licenses/helm

FROM ${GO_BASE} AS kustomize
ARG KUSTOMIZE_VERSION=v5.8.1
ARG KUSTOMIZE_DEPS="golang.org/x/text@v0.41.0"
ENV GOTOOLCHAIN=local CGO_ENABLED=0
WORKDIR /src
RUN go mod download sigs.k8s.io/kustomize/kustomize/v5@${KUSTOMIZE_VERSION} \
    && cp -r "$(go env GOMODCACHE)/sigs.k8s.io/kustomize/kustomize/v5@${KUSTOMIZE_VERSION}/." . \
    && chmod -R u+w . \
    && go get ${KUSTOMIZE_DEPS} && go mod tidy \
    && go build -trimpath -ldflags "-s -w -X sigs.k8s.io/kustomize/api/provenance.version=${KUSTOMIZE_VERSION}" \
         -o /kustomize . \
    && /kustomize version
ARG GO_LICENSES
RUN go run ${GO_LICENSES} save . --save_path=/licenses/kustomize

# argocd-render renders with Argo CD's own code (argocd-render/main.go). It is
# built inside Argo CD's source, with that version's go.mod: its replace
# directives and the gitops-engine module it holds, which a Go module
# download leaves out. ARGOCD_COMMIT is the tag's commit, checked.
# Dependencies raised for fixed vulnerabilities: ARGOCD_RENDER_DEPS.
FROM ${GO_BASE} AS argocd-render
ARG ARGOCD_VERSION
ARG ARGOCD_COMMIT=a5038b0e2bbac1715754380ae0084df8e3d5b48c
ARG ARGOCD_RENDER_DEPS="github.com/klauspost/compress@v1.18.7 golang.org/x/net@v0.60.0"
ENV GOTOOLCHAIN=local CGO_ENABLED=0
RUN git clone -q --depth 1 --branch ${ARGOCD_VERSION} https://github.com/argoproj/argo-cd.git /src \
    && test "$(git -C /src rev-parse HEAD)" = "${ARGOCD_COMMIT}"
WORKDIR /src
COPY argocd-render/ cmd/argocd-render/
RUN go get ${ARGOCD_RENDER_DEPS} && go vet ./cmd/argocd-render && go test ./cmd/argocd-render \
    && go build -trimpath -ldflags "-s -w" -o /argocd-render ./cmd/argocd-render
ARG GO_LICENSES
RUN go run ${GO_LICENSES} save ./cmd/argocd-render --save_path=/licenses/argocd-render

FROM ${ARGOCD_IMAGE} AS argocd

FROM ${BASE}
LABEL org.opencontainers.image.source=https://github.com/vincentbrobald/gitops-scanner \
      org.opencontainers.image.licenses=MIT
COPY --from=kubeconform /kubeconform /usr/local/bin/kubeconform
COPY --from=sops /sops /usr/local/bin/sops
COPY --from=argocd-render /argocd-render /usr/local/bin/argocd-render
COPY --from=helm /helm /usr/local/bin/helm
COPY --from=kustomize /kustomize /usr/local/bin/kustomize
COPY --from=argocd /usr/local/bin/git-verify-wrapper.sh /usr/local/bin/gpg-wrapper.sh /usr/local/bin/
# The ksops stand-in: renders encrypted Secrets with fingerprints, never decrypts.
COPY image/ksops /usr/local/bin/ksops
COPY --from=kubeconform /licenses/kubeconform /licenses/kubeconform
COPY --from=sops /licenses/sops /licenses/sops
COPY --from=argocd-render /licenses/argocd-render /licenses/argocd-render
COPY --from=helm /licenses/helm /licenses/helm
COPY --from=kustomize /licenses/kustomize /licenses/kustomize
COPY LICENSE /licenses/gitops-scanner/LICENSE

# git: the checks render both revisions from worktrees of one clone, and
# argocd-render fetches them from it.
# openssh-keygen: the generators make the teams' deploy keys.
# bash, yq-go: the ksops stand-in.
RUN apk add --no-cache git openssh-keygen bash yq-go
ENV YQ=/usr/bin/yq

WORKDIR /app
COPY pyproject.toml ./
COPY gitops_check ./gitops_check
COPY image/python-licenses.py /tmp/python-licenses.py
# Install the package and list the licenses of what it brought (the Python
# packages' license files; the Alpine packages with their licenses, whose
# sources are Alpine's aports). Then drop what only installing needs: /app
# (sources, build files), pip itself and the wheels ensurepip bundles. The
# image then carries no packaging tools (nor their vendored libraries'
# vulnerabilities).
RUN pip install --no-cache-dir --disable-pip-version-check --root-user-action=ignore . \
    && python /tmp/python-licenses.py /licenses/python && rm /tmp/python-licenses.py \
    && apk list --installed > /licenses/alpine-packages.txt \
    && cd / && rm -rf /app \
    && python -m pip uninstall -y --root-user-action=ignore pip \
    && rm -rf /usr/local/lib/python3*/ensurepip/_bundled
# Smoke test of the image itself: the entry point and the tools it calls work.
RUN gitops-check --help > /dev/null && kubeconform -v && git --version && helm version --short \
    && kustomize version && echo '{}' | argocd-render \
    && sops --version --disable-version-check && ssh-keygen -t ed25519 -N '' -q -f /tmp/k && rm -f /tmp/k /tmp/k.pub

# An unprivileged user, with a passwd entry (ssh-keygen refuses to run
# without one); git and kubeconform write under HOME only.
RUN echo 'gitops-check:x:65532:65532:gitops-check:/tmp:/sbin/nologin' >> /etc/passwd
ENV HOME=/tmp
USER 65532:65532
RUN ssh-keygen -q -t ed25519 -N '' -C test -f /tmp/k && rm -f /tmp/k /tmp/k.pub
ENTRYPOINT ["gitops-check"]
