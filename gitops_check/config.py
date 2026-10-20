"""The repository's check settings: .gitops-check.yaml (kind CheckConfig).

Read at the pull request's head, so a pull request that adds an entry point
is checked with it. Keys (see the repository's file for a commented example):

    repository.url               this repository, as Applications name it in repoURL
    repository.trackedRevisions  branches pinned to the commit under test (default [HEAD])
    argocd.version               the Argo CD the preview must match (informational)
    clusters[]                   name, server: destinations of the stand-in Applications
    entrypoints[]                name, cluster, path: where the walk starts;
                                 deployed: false marks examples (informational);
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
    """A directory of the repository the walk starts from (render.py)."""
    name: str
    cluster: str                     # whose server the stand-in Application deploys to
    path: str                        # relative to the repository root, no surrounding slashes
    deployed: bool = True            # informational: False for examples nothing syncs
    recurse: bool = False            # the stand-in reads subdirectories (directory.recurse)


@dataclasses.dataclass
class Config:
    """The parsed file; see the module docstring for the keys."""
    repo_url: str
    tracked_revisions: list[str]
    argocd_version: str              # informational: not compared with the preview's
    clusters: dict[str, Cluster]
    entrypoints: list[Entrypoint]
    ignore: list[str]                # paths, no surrounding slashes
    external: list[str] = dataclasses.field(default_factory=list)   # repoURL prefixes, lower case

    def server(self, cluster: str) -> str:
        """The API server URL of a cluster named in the file."""
        return self.clusters[cluster].server


def load(path: Path) -> Config:
    """Parse and validate the file. Raises ConfigError (the run exits 2)."""
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
        return u.strip().lower().rstrip("/").removesuffix(".git")
    return norm(a) == norm(b)
