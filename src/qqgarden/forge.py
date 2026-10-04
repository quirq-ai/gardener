"""Forges: where a revert becomes a change people see, and lands. Each backend provides one.

    identity_problem() -> str                # "" when it can write, else why not
    clone_url(repo) -> str
    auth_env() -> dict                       # git credentials, carried only in env
    existing_revert(repo, culprit) -> str    # an open or closed revert PR for the culprit, or ""
    branch_exists(repo, branch) -> bool
    push(workdir, repo, branch)              # push HEAD to that branch
    open_revert(repo, branch, base, title, body, assignees) -> str   # its URL
    queue_land(repo, url)                    # land it through the gate (merge queue)
    rerun(repo, run_url) -> bool             # re-run a failed post-submit run, to verify a culprit
    commit_url(repo, sha) -> str
    landed(repo, url) -> str                 # the revert's commit URL once it is on the branch, else ""

A forge that cannot write (no bot identity) raises NoIdentity from the write calls, and the
cycle then only reports what it would have done.
"""
from __future__ import annotations

from qqgarden.errors import GardenerError


class NoIdentity(GardenerError):
    pass
