"""GitHub backend. History comes from a public git clone; post-submit runs from the Actions API.

infra-config delivers each post-submit builder as `.github/workflows/qq-<builder>.yml`, triggered by
`push` to the default branch, with one job named after the builder. So the builder's runs on a
commit are that workflow's `push` runs whose head is the commit.

Reads need no token for public repos; GITHUB_TOKEN, when set, only raises the rate limit.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from qqgarden import git
from qqgarden.config import Repo
from qqgarden.errors import GardenerError
from qqgarden.model import BuilderRun, Commit

API = "https://api.github.com"


def workflow_file(builder: str) -> str:
    return f"qq-{builder}.yml"


class Backend:
    def __init__(self, cache: str | Path = ".qq/git", token: str | None = None, **_):
        self.cache = Path(cache)
        self.token = token if token is not None else os.environ.get("GITHUB_TOKEN", "")

    # --- history ------------------------------------------------------------------------------

    def commits(self, repo: Repo, limit: int) -> list[Commit]:
        if not repo.slug:
            raise GardenerError(f"{repo.name}: source {repo.source!r} is not on github.com")
        dest = git.mirror(f"https://github.com/{repo.slug}.git", self.cache / repo.name,
                          repo.default_branch)
        return git.first_parent(dest, repo.default_branch, limit)

    # --- runs ---------------------------------------------------------------------------------

    def runs(self, repo: Repo, builder: str, pages: int = 3) -> tuple[list[BuilderRun], str]:
        path = (f"/repos/{repo.slug}/actions/workflows/{workflow_file(builder)}/runs?"
                + urllib.parse.urlencode({"branch": repo.default_branch, "event": "push",
                                          "per_page": 100}))
        out: list[BuilderRun] = []
        for page in range(1, pages + 1):
            doc = self._get(f"{path}&page={page}")
            if doc is None:
                return [], (f"{workflow_file(builder)} is not in {repo.slug}: the post-submit workflow "
                            "has not been delivered (infra-config `qqcfg deliver`)")
            items = doc.get("workflow_runs", [])
            out.extend(_run(builder, r) for r in items)
            if len(items) < 100:
                break
        return out, ""

    def _get(self, path: str) -> dict | None:
        req = urllib.request.Request(API + path, headers={
            "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28",
            **({"Authorization": f"Bearer {self.token}"} if self.token else {})})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            raise GardenerError(f"GitHub API {path}: HTTP {e.code} {e.reason}") from None
        except (urllib.error.URLError, TimeoutError) as e:
            raise GardenerError(f"GitHub API {path}: {e}") from None


def _run(builder: str, r: dict) -> BuilderRun:
    return BuilderRun(
        builder=builder, commit=r["head_sha"], status=r.get("status") or "",
        conclusion=r.get("conclusion") or "", id=str(r["id"]), attempt=int(r.get("run_attempt") or 1),
        url=r.get("html_url", ""),
        finished_at=r.get("updated_at", "") if r.get("status") == "completed" else "")
