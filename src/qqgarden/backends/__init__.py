"""Backends: where commits and post-submit runs come from. Picked by the `backend` field in
infra-config (`pipelines.toml [defaults] backend`); `github` in v0, `launchpad` later.

A backend module defines `Backend` with:

    commits(repo, limit) -> list[Commit]           # first-parent history, newest first
    runs(repo, builder) -> (list[BuilderRun], note) # note is "" or why there are none
"""
from __future__ import annotations

import importlib

from qqgarden.errors import GardenerError


def load(name: str, **kwargs):
    try:
        module = importlib.import_module(f"qqgarden.backends.{name}")
    except ModuleNotFoundError as e:
        if e.name == f"qqgarden.backends.{name}":
            raise GardenerError(f"no gardener backend {name!r}; add qqgarden/backends/{name}.py") from None
        raise
    return module.Backend(**kwargs)
