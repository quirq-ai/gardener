"""A forge on local bare git repos, for tests and CI's done-when demos.

Each repo's remote is a bare repo (the snapshot's `remote` field). A "PR" is a JSON file under
`<forge dir>/prs/`, and landing pushes the revert onto the branch, as a merge queue would.
"""
from __future__ import annotations

import json
from pathlib import Path

from qqgarden import git
from qqgarden.errors import GardenerError
from qqgarden.revert import branch_name


class LocalForge:
    def __init__(self, remotes: dict[str, str], forge_dir: str | Path):
        self.remotes = remotes
        self.dir = Path(forge_dir)

    def identity_problem(self) -> str:
        return ""

    def clone_url(self, repo) -> str:
        return self.remotes[repo.name]

    def auth_env(self) -> dict:
        return {}

    def existing_revert(self, repo, culprit: str) -> str:
        for p in sorted((self.dir / "prs").glob("*.json")) if (self.dir / "prs").is_dir() else []:
            pr = json.loads(p.read_text())
            if pr["repo"] == repo.name and pr["branch"] == branch_name(culprit):
                return pr["url"]
        return ""

    def branch_exists(self, repo, branch: str) -> bool:
        return git.run(["rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"],
                       cwd=Path(self.remotes[repo.name]), check=False).returncode == 0

    def push(self, workdir: Path, repo, branch: str) -> None:
        git.run(["push", "--quiet", "origin", f"HEAD:refs/heads/{branch}"], cwd=workdir)

    def open_revert(self, repo, branch: str, base: str, title: str, body: str,
                    assignees: list[str]) -> str:
        prs = self.dir / "prs"
        prs.mkdir(parents=True, exist_ok=True)
        n = len(list(prs.glob("*.json"))) + 1
        url = f"local://{repo.name}/pull/{n}"
        (prs / f"{n}.json").write_text(json.dumps({"url": url, "repo": repo.name, "branch": branch,
                                                   "base": base, "title": title, "body": body,
                                                   "assignees": assignees}, indent=2))
        return url

    def queue_land(self, repo, url: str, head: str = "") -> None:
        pr = json.loads((self.dir / "prs" / f"{url.rsplit('/', 1)[1]}.json").read_text())
        remote = Path(self.remotes[repo.name])
        tip = git.run(["rev-parse", f"refs/heads/{pr['branch']}"], cwd=remote).stdout.strip()
        if head and tip != head:
            raise GardenerError(f"{url}: its head is no longer the revert commit {head[:12]}; not landing it")
        git.run(["push", "--quiet", ".", f"refs/heads/{pr['branch']}:refs/heads/{repo.default_branch}"],
                cwd=remote)

    def rerun(self, repo, run_url: str, failed_only: bool = True) -> bool:
        return False

    def commit_url(self, repo, sha: str) -> str:
        return f"local://{repo.name}/commit/{sha}"

    def landed(self, repo, url: str) -> str:
        """The revert's commit once it is on the default branch, else ""."""
        pr = json.loads((self.dir / "prs" / f"{url.rsplit('/', 1)[1]}.json").read_text())
        remote = Path(self.remotes[repo.name])
        tip = git.run(["rev-parse", f"refs/heads/{pr['branch']}"], cwd=remote).stdout.strip()
        on_main = git.run(["merge-base", "--is-ancestor", tip, f"refs/heads/{repo.default_branch}"],
                          cwd=remote, check=False).returncode == 0
        return self.commit_url(repo, tip) if on_main else ""

    def backfill(self, repo, builder: str, commit: str) -> None:
        d = self.dir / "dispatches"
        d.mkdir(parents=True, exist_ok=True)
        with open(d / f"{repo.name}.jsonl", "a") as f:
            f.write(json.dumps({"builder": builder, "commit": commit}) + "\n")
