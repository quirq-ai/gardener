"""V0-GAR-02, first half: group failures by regression range (Sheriff-o-Matic's grouping).

Builders that went red over the same range (last good .. first bad) share one culprit search, so
they form one group. A group also says what failed, because the revert caps differ by failure
type (auto_revert.toml [build_failure] and [test_failure]):

- `build`: a step before the tests failed (a generated builder names each step after the
  capability it runs: `fetch (...)`, `build (...)`, `test (...)`);
- `test`: a `test (...)` step failed, or the stored verdict has unexpected tests;
- `unknown`: neither could be told; the gardener never reverts an unknown failure.
"""
from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from typing import Any, Protocol

from qqgarden.model import RedSpan, TreeStatus

SCHEMA = "qq-failure-group/1"
BUILD_CAPABILITIES = ("fetch", "build")
TEST_CAPABILITIES = ("test",)


@dataclass(frozen=True)
class Group:
    repo: str
    first_bad: str
    last_good: str
    suspects: list[str]
    builders: list[str]
    kind: str                          # build, test or unknown
    failed_steps: list[str] = field(default_factory=list)
    tests: list[str] = field(default_factory=list)      # unexpected tests, when the store has them
    runs: list[str] = field(default_factory=list)       # first red run of each builder
    schema: str = SCHEMA

    @property
    def key(self) -> str:
        return f"{self.repo}@{self.last_good[:12] or 'unknown'}..{self.first_bad[:12]}"

    def to_dict(self) -> dict[str, Any]:
        return {**dataclasses.asdict(self), "key": self.key}


class Evidence(Protocol):
    """What a group needs beyond the tree status: why the first red run failed."""
    def failed_steps(self, repo: str, run_url: str) -> list[str]: ...
    def unexpected_tests(self, repo: str, builder: str, commit: str) -> list[str] | None: ...


def classify(failed_steps: list[str], tests: list[str] | None) -> str:
    caps = {s.split(" (", 1)[0].strip().lower() for s in failed_steps}
    if caps & set(BUILD_CAPABILITIES):
        return "build"           # nothing after a broken build ran, so tests cannot be blamed
    if tests:
        return "test"
    if caps & set(TEST_CAPABILITIES):
        return "test"
    return "unknown"


def group(status: TreeStatus, evidence: Evidence | None = None) -> list[Group]:
    by_range: dict[tuple[str, str], list[RedSpan]] = {}
    for span in status.red:
        by_range.setdefault((span.last_good, span.first_bad), []).append(span)
    out = []
    for (last_good, first_bad), spans in by_range.items():
        steps: list[str] = []
        tests: list[str] = []
        kinds = []
        for s in spans:
            st = evidence.failed_steps(status.repo, s.url) if evidence else []
            ts = evidence.unexpected_tests(status.repo, s.builder, s.first_bad) if evidence else None
            steps += [f"{s.builder}: {x}" for x in st]
            tests += ts or []
            kinds.append(classify(st, ts))
        kind = "build" if "build" in kinds else "test" if "test" in kinds else "unknown"
        out.append(Group(
            repo=status.repo, first_bad=first_bad, last_good=last_good,
            suspects=max((s.suspects for s in spans), key=len),
            builders=sorted(s.builder for s in spans), kind=kind, failed_steps=steps,
            tests=sorted(set(tests)), runs=[s.url for s in spans]))
    return sorted(out, key=lambda g: g.key)
