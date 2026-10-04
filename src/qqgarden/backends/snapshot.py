"""An offline backend: commits and runs from a JSON snapshot. Used by tests and by CI's done-when
demos, and handy to replay what a live backend saw.

    {"repos": {"<repo>": {"commits": [Commit...], "runs": [BuilderRun...],
                          "failed_steps": {"<run url>": ["build (...)"]},
                          "remote": "<bare repo, relative to the snapshot>"}}}

With a `remote`, the backend's forge is a LocalForge on that bare repo.
"""
from __future__ import annotations

import json
from pathlib import Path

from qqgarden.backends.localforge import LocalForge
from qqgarden.config import Repo
from qqgarden.errors import GardenerError
from qqgarden.model import BuilderRun, Commit


class Backend:
    def __init__(self, path: str | Path, forge_dir: str | Path | None = None, **_):
        try:
            self.data = json.loads(Path(path).read_text())["repos"]
        except (OSError, ValueError, KeyError) as e:
            raise GardenerError(f"{path}: not a gardener snapshot: {e}") from None
        remotes = {name: str((Path(path).parent / r["remote"]).resolve())
                   for name, r in self.data.items() if r.get("remote")}
        self.forge = LocalForge(remotes, forge_dir or Path(path).parent / "forge") if remotes else None

    def _repo(self, repo: Repo) -> dict:
        return self.data.get(repo.name, {})

    def commits(self, repo: Repo, limit: int) -> list[Commit]:
        return [Commit(**c) for c in self._repo(repo).get("commits", [])][:limit]

    def runs(self, repo: Repo, builder: str) -> tuple[list[BuilderRun], str]:
        runs = [BuilderRun(**r) for r in self._repo(repo).get("runs", []) if r["builder"] == builder]
        return runs, "" if runs else f"the snapshot has no {builder} runs"

    def failed_steps(self, repo: Repo, run_url: str) -> list[str]:
        return list(self._repo(repo).get("failed_steps", {}).get(run_url, []))
