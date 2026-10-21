"""The repository's check settings: .gitops-check.yaml (kind CheckConfig).

Read at the pull request's head, so a pull request that adds an entry point
is checked with it. Keys (see the repository's file for a commented example):

    repository.url               this repository, as Applications name it in repoURL
    repository.trackedRevisions  branches pinned to the commit under test (default [HEAD])
    argocd.version               the Argo CD the preview must match (informational)
    clusters[]                   name, server: destinations of the stand-in Applications
    entrypoints[]                name, cluster, path: where the walk starts;
                                 deployed: false marks examples;
                                 recurse: true reads subdirectories too (directory.recurse,
                                 as the root Application that syncs the directory does)
    ignore[]                     path, reason: directories no entry point reaches, on purpose
    external[]                   prefix, reason: repositories outside the check (e.g. tenants'):
                                 an Application whose sources are all there is not rendered
"""
from __future__ import annotations

import dataclasses
from pathlib import Path

import yaml


class ConfigError(Exception):
    """The file is missing, not YAML, not a CheckConfig, or lacks a key."""


@dataclasses.dataclass
class Cluster:
    """A cluster the preview knows (same name and server as its cluster Secret)."""
    name: str
    server: str


@dataclasses.dataclass
class Entrypoint:
    """Where the walk starts (entrypoints[]): a directory of Applications on a cluster.

    deployed: False for examples, which the project check skips; recurse: the
    directory's subdirectories are read too (and count as reached).
    """
    name: str
    cluster: str
    path: str                        # relative to the repository root, no surrounding slashes
    deployed: bool = True
    recurse: bool = False


@dataclasses.dataclass
class Config:
    """The parsed .gitops-check.yaml (see the module docstring); clusters by name."""
    repo_url: str
    tracked_revisions: list[str]
    argocd_version: str
    clusters: dict[str, Cluster]
    entrypoints: list[Entrypoint]
    ignore: list[str]                # paths, no surrounding slashes
    external: list[str] = dataclasses.field(default_factory=list)   # repoURL prefixes, lower case

    def server(self, cluster: str) -> str:
        """The API server URL of the named cluster; KeyError if it is unknown."""
        return self.clusters[cluster].server


def load(path: Path) -> Config:
    """Read and check the configuration file path.

    Paths lose their surrounding slashes and external prefixes are lower-cased.
    Raises ConfigError (the run exits 2) when the file can't be read or parsed,
    isn't a CheckConfig, lacks a key or names an unknown cluster.
    """
    try:
        d = yaml.safe_load(path.read_text())
    except (OSError, yaml.YAMLError) as e:
        raise ConfigError(f"{path}: {e}") from e
    if not isinstance(d, dict) or d.get("kind") != "CheckConfig":
        raise ConfigError(f"{path}: not a CheckConfig")
    try:
        clusters = {c["name"]: Cluster(c["name"], c["server"]) for c in d.get("clusters", [])}
        entries = [Entrypoint(e["name"], e["cluster"], e["path"].strip("/"), e.get("deployed", True),
                              bool(e.get("recurse", False)))
                   for e in d["entrypoints"]]
        cfg = Config(
            repo_url=d["repository"]["url"],
            tracked_revisions=list(d["repository"].get("trackedRevisions", ["HEAD"])),
            argocd_version=str(d.get("argocd", {}).get("version", "")),
            clusters=clusters,
            entrypoints=entries,
            ignore=[i["path"].strip("/") for i in d.get("ignore", [])],
            external=[e["prefix"].strip().lower() for e in d.get("external", [])],
        )
    except (KeyError, TypeError) as e:
        raise ConfigError(f"{path}: missing or invalid key {e}") from e
    for e in cfg.entrypoints:
        if e.cluster not in cfg.clusters:
            raise ConfigError(f"{path}: entry point {e.name}: unknown cluster {e.cluster}")
    return cfg


def same_repo(a: str, b: str) -> bool:
    """Whether two repository URLs name the same repository: case, a trailing
    slash and ".git" don't matter (ssh:// and https:// URLs never match)."""
    def norm(u: str) -> str:
        """The URL stripped, lower-cased, without a trailing slash or ".git"."""
        return u.strip().lower().rstrip("/").removesuffix(".git")
    return norm(a) == norm(b)
