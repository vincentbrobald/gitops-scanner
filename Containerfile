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

FROM ${BASE}
LABEL org.opencontainers.image.source=https://github.com/vincentbrobald/gitops-scanner \
      org.opencontainers.image.licenses=MIT
COPY --from=kubeconform /kubeconform /usr/local/bin/kubeconform
COPY --from=sops /sops /usr/local/bin/sops
COPY --from=kubeconform /licenses/kubeconform /licenses/kubeconform
COPY --from=sops /licenses/sops /licenses/sops
COPY LICENSE /licenses/gitops-scanner/LICENSE

# git: the checks render both revisions from worktrees of one clone.
# openssh-keygen: the generators make the teams' deploy keys.
RUN apk add --no-cache git openssh-keygen

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
RUN gitops-check --help > /dev/null && kubeconform -v && git --version \
    && sops --version --disable-version-check && ssh-keygen -t ed25519 -N '' -q -f /tmp/k && rm -f /tmp/k /tmp/k.pub

# An unprivileged user, with a passwd entry (ssh-keygen refuses to run
# without one); git and kubeconform write under HOME only.
RUN echo 'gitops-check:x:65532:65532:gitops-check:/tmp:/sbin/nologin' >> /etc/passwd
ENV HOME=/tmp
USER 65532:65532
RUN ssh-keygen -q -t ed25519 -N '' -C test -f /tmp/k && rm -f /tmp/k /tmp/k.pub
ENTRYPOINT ["gitops-check"]
