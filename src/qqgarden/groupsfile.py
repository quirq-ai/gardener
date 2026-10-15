"""The failure groups the tree-status job publishes as `groups/groups.json` on the `tree-status`
branch, so the monitoring dashboard can say why each red range broke (build, test or infra).

They are computed from the status files that job has just written, never from a second look at
GitHub, so one commit of the branch cannot disagree with itself. Each red range's failure type
comes from the failed steps of its first red run's FIRST attempt, which a re-run never changes;
when that attempt shows no failed step (cancelled, a startup failure, or green and later re-run
red), from the newest completed attempt that has one. So the published type can differ from the
cycle's, which reads the newest attempt. A range already in the previous file, with the same
first red runs, is carried over without reading anything.

The file holds nothing that changes from run to run (no times, no status commit, no reason text),
so it changes only when a red range appears, moves or is first read. Writing it is all or nothing:
`write` replaces the file only once everything was read.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Protocol

from qqgarden.groups import classify
from qqgarden.model import RedSpan, TreeStatus

SCHEMA = "qq-gardener-groups/1"


class AttemptSteps(Protocol):
    def __call__(self, repo: str, run_url: str, attempt: int) -> list[str] | None: ...


def attempts_to_read(span: RedSpan) -> list[int]:
    """Attempt 1 first, then the newest completed attempt down to 2. `first_bad_attempt` is the
    newest attempt, including a re-run still in progress, which is skipped."""
    newest = span.first_bad_attempt - (1 if span.first_bad_running else 0)
    return [1] + list(range(newest, 1, -1))


def span_steps(repo: str, span: RedSpan, read: AttemptSteps) -> tuple[list[str], int]:
    """The failed steps of the first attempt that has any, and that attempt (0 when none has)."""
    for n in attempts_to_read(span):
        steps = read(repo, span.url, n) if span.url else None
        if steps:
            return steps, n
    return [], 0


def key(entry: dict[str, Any]) -> tuple:
    return (entry["repo"], entry["last_good"], entry["first_bad"], tuple(entry["runs"]))


def build(statuses: list[TreeStatus], read: AttemptSteps, previous: dict[str, Any] | None = None) -> dict[str, Any]:
    known = {}
    if previous and previous.get("schema") == SCHEMA:
        known = {key(e): e for e in previous.get("groups", []) if isinstance(e, dict) and {"repo", "last_good", "first_bad", "runs"} <= e.keys()}
    groups = []
    for status in statuses:
        by_range: dict[tuple[str, str], list[RedSpan]] = {}
        for span in status.red:
            by_range.setdefault((span.last_good, span.first_bad), []).append(span)
        for (last_good, first_bad), spans in by_range.items():
            spans = sorted(spans, key=lambda s: s.builder)
            entry_key = (status.repo, last_good, first_bad, tuple(s.url for s in spans))
            if entry_key in known:
                groups.append(known[entry_key])
                continue
            steps: list[str] = []
            kinds = []
            for s in spans:
                found, _ = span_steps(status.repo, s, read)
                steps += [f"{s.builder}: {x}" for x in found]
                kinds.append(classify(found, None))
            groups.append({
                "repo": status.repo,
                "last_good": last_good,
                "first_bad": first_bad,
                "builders": [s.builder for s in spans],
                "runs": [s.url for s in spans],
                "kind": next((k for k in ("infra", "build", "test") if k in kinds), "unknown"),
                "failed_steps": steps,
            })
    groups.sort(key=lambda e: (e["repo"], e["first_bad"], e["last_good"]))
    return {"schema": SCHEMA, "groups": groups}


def to_json(doc: dict[str, Any]) -> str:
    return json.dumps(doc, sort_keys=True, indent=2) + "\n"


def read_statuses(directory: Path) -> list[TreeStatus]:
    return [TreeStatus.from_dict(json.loads(p.read_text())) for p in sorted(directory.glob("*.json"))]


def read_previous(path: Path | None) -> dict[str, Any] | None:
    if path is None or not path.is_file():
        return None
    try:
        doc = json.loads(path.read_text())
    except ValueError:
        return None
    return doc if isinstance(doc, dict) else None


def write(path: Path, doc: dict[str, Any]) -> None:
    """Replace the file in one step, so a reader or a failed run never sees half of one."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(to_json(doc))
    os.replace(tmp, path)
