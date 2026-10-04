"""V0-GAR-02, second half: bisect a regression range to its culprit (LUCI Bisection's job).

The suspects are the commits after the last good one, up to and including the first bad one,
oldest first. A probe answers for one commit: pass, fail, or unknown (it could not tell, such as
an infra error). Bisection is a binary search over the suspects that skips unknown answers, like
`git bisect skip`. With `require_culprit_verification` (auto_revert.toml), the culprit is probed
again and its parent once more, so a flaky probe cannot name a culprit: LUCI reruns with and
without the suspect for the same reason.

Probes:
- `CommandProbe` checks the commit out into a scratch worktree and runs a command there (for a qq
  repo, `qq build` / `qq test` through depot). It is what CI's done-when demo and the gardener agent
  use. It runs the repo's code, so run it only where no write token is in reach.
- TODO(expert): a `dispatch` probe that re-runs the repo's own post-submit builder on the commit, once
  infra-config's generated post-submit accepts a commit input (asked of infra-config 2026-10-04).
"""
from __future__ import annotations

import shutil
import subprocess
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from qqgarden import git
from qqgarden.errors import GardenerError


class Probe(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    UNKNOWN = "unknown"


@dataclass
class Result:
    culprit: str = ""                  # "" when bisection could not name one commit
    verified: bool = False             # the culprit failed again and its parent passed again
    remaining: list[str] = field(default_factory=list)   # suspects still in play when no culprit
    probes: list[tuple[str, str]] = field(default_factory=list)   # (commit, answer) in order
    reason: str = ""

    def to_dict(self) -> dict:
        return {"culprit": self.culprit, "verified": self.verified, "remaining": self.remaining,
                "probes": [list(p) for p in self.probes], "reason": self.reason}


def bisect(suspects: Sequence[str], last_good: str, probe: Callable[[str], Probe],
           verify: bool) -> Result:
    """`suspects` oldest first; the last one is known bad. `last_good` is known good ("" if none)."""
    if not suspects:
        raise GardenerError("nothing to bisect: the regression range has no suspects")
    res = Result()

    def ask(commit: str) -> Probe:
        answer = probe(commit)
        res.probes.append((commit, answer.value))
        return answer

    # Invariant: everything before lo passes (or is last_good), suspects[hi] fails.
    lo, hi = 0, len(suspects) - 1
    skipped: set[int] = set()
    while lo < hi:
        candidates = [i for i in range(lo, hi) if i not in skipped]
        if not candidates:
            res.remaining = list(suspects[lo:hi + 1])
            res.reason = (f"{len(res.remaining)} suspects left; the probe could not tell for "
                          + ", ".join(c[:12] for c in suspects[lo:hi]))
            return res
        mid = min(candidates, key=lambda i: abs(i - (lo + hi) // 2))
        answer = ask(suspects[mid])
        if answer is Probe.FAIL:
            hi = mid
        elif answer is Probe.PASS:
            lo = mid + 1
        else:
            skipped.add(mid)
    culprit = suspects[hi]
    if verify:
        parent = suspects[hi - 1] if hi > 0 else last_good
        again = ask(culprit)
        before = ask(parent) if parent else Probe.UNKNOWN
        if again is not Probe.FAIL or before is not Probe.PASS:
            res.remaining = [culprit]
            res.reason = (f"{culprit[:12]} did not verify: it gave {again.value} on a re-probe and "
                          f"its parent {parent[:12] or '(none)'} gave {before.value}; it may be flaky")
            return res
        res.verified = True
    res.culprit = culprit
    res.reason = f"{culprit[:12]} is the first commit that fails" + (" (verified)" if verify else "")
    return res


class CommandProbe:
    """Runs `command` (through the shell) in a checkout of each commit. Exit 0 is pass; exit 125
    is unknown, as with `git bisect run`; any other exit is fail; a timeout is unknown."""

    def __init__(self, repo_dir: Path, command: str, timeout_s: int = 1800):
        self.repo_dir = Path(repo_dir)
        self.command = command
        self.timeout_s = timeout_s

    def __call__(self, commit: str) -> Probe:
        tmp = Path(tempfile.mkdtemp(prefix="qqgarden-probe-"))
        work = tmp / "w"
        try:
            git.run(["worktree", "add", "--quiet", "--detach", str(work), commit], cwd=self.repo_dir)
            try:
                p = subprocess.run(self.command, shell=True, cwd=work, capture_output=True,
                                   timeout=self.timeout_s)
            except subprocess.TimeoutExpired:
                return Probe.UNKNOWN
            return Probe.PASS if p.returncode == 0 else Probe.UNKNOWN if p.returncode == 125 else Probe.FAIL
        finally:
            git.run(["worktree", "remove", "--force", str(work)], cwd=self.repo_dir, check=False)
            shutil.rmtree(tmp, ignore_errors=True)
