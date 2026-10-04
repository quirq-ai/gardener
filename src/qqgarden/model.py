"""What the gardener reads (commits and post-submit runs) and what it publishes (tree status).

Every record serializes to canonical JSON (sorted keys), so an unchanged tree status has the same
bytes and the publisher commits only real changes.
"""
from __future__ import annotations

import dataclasses
import json
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

SCHEMA = "qq-tree-status/1"


class RunState(StrEnum):
    """One builder on one commit."""
    GREEN = "green"          # the post-submit run passed
    RED = "red"              # it failed: a verdict that closes the tree
    PENDING = "pending"      # queued, running, or the commit landed too recently to expect a run
    MISSING = "missing"      # no run, and there should be one by now
    CANCELLED = "cancelled"  # the run ended without a verdict (cancelled, skipped, stale)


class TreeState(StrEnum):
    OPEN = "open"            # every post-submit builder's newest verdict is green
    CLOSED = "closed"        # some builder's newest verdict is red
    UNKNOWN = "unknown"      # some builder has no verdict yet, and none is red


# How a backend's run conclusion maps to a state. Only a real failure is RED: the gardener reverts
# on RED, so an infra hiccup that produced no verdict must never look like a culprit.
CONCLUSIONS = {
    "success": RunState.GREEN,
    "neutral": RunState.GREEN,
    "failure": RunState.RED,
    "timed_out": RunState.RED,
    "startup_failure": RunState.RED,   # the workflow itself is broken on this commit
    "cancelled": RunState.CANCELLED,
    "skipped": RunState.CANCELLED,
    "stale": RunState.CANCELLED,
    "action_required": RunState.CANCELLED,
}


@dataclass(frozen=True)
class Commit:
    sha: str
    # RFC 3339 UTC: the committer date, which is when it reached the branch for a squash or merge
    # commit made by the forge. TODO(expert): a fast-forward push of old commits starts their grace
    # period early; the push time would need the backend's event log.
    landed_at: str
    title: str = ""


@dataclass(frozen=True)
class BuilderRun:
    builder: str
    commit: str
    status: str             # "queued", "in_progress" or "completed"
    conclusion: str = ""    # set once completed; see CONCLUSIONS
    id: str = ""
    attempt: int = 1
    url: str = ""
    finished_at: str = ""

    @property
    def state(self) -> RunState:
        if self.status != "completed":
            return RunState.PENDING
        return CONCLUSIONS.get(self.conclusion, RunState.CANCELLED)


@dataclass(frozen=True)
class BuilderStatus:
    """A builder's newest verdict on the branch."""
    state: str               # green, red, or "none" when it has no verdict on any listed commit
    commit: str = ""
    url: str = ""


@dataclass(frozen=True)
class RedSpan:
    """One builder's red streak and its regression range: the commits that can hold the culprit.

    `suspects` are the commits after `last_good` up to and including `first_bad`, oldest first.
    Commits between them without a verdict of their own are suspects too. GAR-02 bisects these.
    """
    builder: str
    first_bad: str
    latest_bad: str
    last_good: str = ""      # empty when no green verdict is in the listed window
    suspects: list[str] = field(default_factory=list)
    url: str = ""            # the first red run


@dataclass(frozen=True)
class Coverage:
    """GAR-01's done-when: every main commit since onboarding has a post-submit result."""
    since: str = ""          # the oldest listed commit with a post-submit run; "" before onboarding
    commits: int = 0         # commits checked
    missing: list[str] = field(default_factory=list)     # "<sha> <builder>"
    cancelled: list[str] = field(default_factory=list)
    pending: list[str] = field(default_factory=list)

    @property
    def complete(self) -> bool:
        return not self.missing and not self.cancelled


@dataclass(frozen=True)
class TreeStatus:
    repo: str
    state: str
    reason: str
    branch: str = ""
    head: str = ""
    builders: dict[str, BuilderStatus] = field(default_factory=dict)
    red: list[RedSpan] = field(default_factory=list)
    coverage: Coverage = field(default_factory=Coverage)
    notes: list[str] = field(default_factory=list)
    schema: str = SCHEMA

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, indent=2) + "\n"

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "TreeStatus":
        return cls(
            repo=d["repo"], state=d["state"], reason=d.get("reason", ""), branch=d.get("branch", ""),
            head=d.get("head", ""),
            builders={k: BuilderStatus(**v) for k, v in d.get("builders", {}).items()},
            red=[RedSpan(**r) for r in d.get("red", [])],
            coverage=Coverage(**d.get("coverage", {})),
            notes=list(d.get("notes", [])),
        )
