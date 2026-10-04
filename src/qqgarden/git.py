"""The little git the gardener needs: a branch's first-parent history from a public clone.

First parent, because that is the sequence of commits that landed on the branch: commits a merge
brought in from a PR branch never ran post-submit on their own and are not suspects.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from qqgarden.errors import GardenerError
from qqgarden.model import Commit


def run(args: list[str], cwd: Path | None = None, check: bool = True) -> subprocess.CompletedProcess:
    p = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    if check and p.returncode != 0:
        raise GardenerError(f"git {' '.join(args)} failed in {cwd or '.'}: {p.stderr.strip()}")
    return p


def mirror(url: str, dest: Path, branch: str) -> Path:
    """A blobless bare clone of one branch at dest, created or refreshed. Blobless keeps it cheap:
    listing history needs commits only, and bisection fetches blobs on checkout."""
    dest = Path(dest)
    if not (dest / "HEAD").is_file():
        dest.parent.mkdir(parents=True, exist_ok=True)
        run(["clone", "--quiet", "--bare", "--filter=blob:none", "--single-branch", "--branch", branch,
             url, str(dest)])
    else:
        run(["fetch", "--quiet", "--filter=blob:none", "origin", f"+refs/heads/{branch}:refs/heads/{branch}"],
            cwd=dest)
    return dest


def first_parent(repo_dir: Path, ref: str, limit: int) -> list[Commit]:
    """Newest first."""
    out = run(["log", "--first-parent", f"--max-count={limit}", "--format=%H%x09%cI%x09%s", ref],
              cwd=repo_dir).stdout
    commits = []
    for line in out.splitlines():
        sha, when, title = (line.split("\t", 2) + [""])[:3]
        commits.append(Commit(sha=sha, landed_at=when, title=title))
    return commits
