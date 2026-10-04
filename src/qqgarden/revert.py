"""Make a clean revert of one culprit on top of the branch tip, in a scratch clone.

Clean means `git revert` applied with no conflict and changed something (auto_revert.toml
`only_clean_reverts`): a revert that needs a person to resolve conflicts is a normal change, not
the gardener's to make. Nothing is pushed here; the forge pushes once the ledger has counted it.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from qqgarden import git


@dataclass(frozen=True)
class Made:
    clean: bool
    workdir: Path
    base: str = ""          # the branch tip the revert sits on
    commit: str = ""        # the revert commit
    title: str = ""         # the culprit's title
    reason: str = ""


def branch_name(culprit: str) -> str:
    return f"qq-gardener/revert-{culprit[:12]}"


def make(clone_url: str, branch: str, culprit: str, workdir: Path, message_footer: str,
         env: dict | None = None) -> Made:
    work = Path(workdir)
    git.run(["clone", "--quiet", "--filter=blob:none", "--single-branch", "--branch", branch,
             clone_url, str(work)], env=env)
    git.run(["config", "user.name", "qq gardener"], cwd=work)
    git.run(["config", "user.email", "gardener@quirq-ai.invalid"], cwd=work)
    base = git.run(["rev-parse", "HEAD"], cwd=work).stdout.strip()
    if git.run(["merge-base", "--is-ancestor", culprit, base], cwd=work, check=False).returncode != 0:
        return Made(False, work, base, reason=f"{culprit[:12]} is not on {branch}")
    title = git.run(["log", "-1", "--format=%s", culprit], cwd=work).stdout.strip()
    parents = git.run(["rev-list", "--parents", "-n", "1", culprit], cwd=work).stdout.split()[1:]
    args = ["revert", "--no-commit"] + (["-m", "1"] if len(parents) > 1 else []) + [culprit]
    p = git.run(args, cwd=work, check=False)
    if p.returncode != 0:
        git.run(["revert", "--abort"], cwd=work, check=False)
        return Made(False, work, base, title=title,
                    reason=f"reverting {culprit[:12]} conflicts with later commits; not a clean revert")
    if git.run(["diff", "--cached", "--quiet"], cwd=work, check=False).returncode == 0:
        return Made(False, work, base, title=title, reason=f"reverting {culprit[:12]} changes nothing")
    msg = f'Revert "{title}"\n\nThis reverts commit {culprit}.\n\n{message_footer.strip()}\n'
    git.run(["commit", "--quiet", "-m", msg], cwd=work)
    commit = git.run(["rev-parse", "HEAD"], cwd=work).stdout.strip()
    return Made(True, work, base, commit, title)
