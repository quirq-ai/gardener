"""V0-GAR-01: a verdict for every main commit, red detection and the tree status.

Input is the branch's first-parent history (newest first) and every post-submit run a backend
reports for it. Output is a TreeStatus:

- per builder, its newest verdict; the tree is closed while any builder's newest verdict is red,
  open when all are green, and unknown while a builder has no verdict yet (a missing signal is not
  a healthy one);
- per red builder, the red streak and its regression range (last good .. first bad), which V0-GAR-02
  bisects;
- coverage: commits since onboarding with no result, or whose run was cancelled. Each one is a hole
  where a culprit can hide, so GAR-01's done-when fails on any.
"""
from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import datetime, timedelta, timezone

from qqgarden.model import (BuilderRun, BuilderStatus, Commit, Coverage, RedSpan, RunState,
                            TreeState, TreeStatus)


def parse_time(s: str) -> datetime:
    """RFC 3339; a time without an offset is taken as UTC."""
    t = datetime.fromisoformat(s.replace("Z", "+00:00"))
    return (t if t.tzinfo else t.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)


def latest_runs(runs: Iterable[BuilderRun]) -> dict[tuple[str, str], BuilderRun]:
    """(builder, commit) -> the run that counts: the newest run, and within it the newest attempt.
    A re-run replaces its first try, as GitHub shows it; a later run of the same commit (pushed
    again) replaces an older one."""
    out: dict[tuple[str, str], BuilderRun] = {}
    for r in runs:
        key = (r.builder, r.commit)
        cur = out.get(key)
        if cur is None or (_id_key(r.id), r.attempt) > (_id_key(cur.id), cur.attempt):
            out[key] = r
    return out


def _id_key(run_id: str) -> tuple[int, str]:
    return (int(run_id), "") if run_id.isdigit() else (0, run_id)


def state_of(commit: Commit, builder: str, runs: dict[tuple[str, str], BuilderRun],
             now: datetime, grace: timedelta) -> RunState:
    run = runs.get((builder, commit.sha))
    if run is not None:
        return run.state
    return RunState.PENDING if now - parse_time(commit.landed_at) < grace else RunState.MISSING


def observe(backend, repo, limit: int, now: datetime, grace: timedelta,
            notes: Sequence[str] = ()) -> tuple[TreeStatus, list[Commit]]:
    """Read one repo through a backend and compute its tree status."""
    notes = list(notes)
    commits = backend.commits(repo, limit)
    runs: list[BuilderRun] = []
    for b in repo.postsubmit:
        r, note = backend.runs(repo, b)
        runs.extend(r)
        if note:
            notes.append(note)
    return tree_status(repo.name, repo.default_branch, repo.postsubmit, commits, runs, now, grace,
                       notes), list(commits)


def tree_status(repo: str, branch: str, builders: Sequence[str], commits: Sequence[Commit],
                runs: Iterable[BuilderRun], now: datetime, grace: timedelta,
                notes: Sequence[str] = ()) -> TreeStatus:
    """`commits` is the branch's first-parent history, newest first."""
    notes = list(notes)
    head = commits[0].sha if commits else ""
    if not builders:
        return TreeStatus(repo=repo, branch=branch, head=head, state=TreeState.UNKNOWN,
                          reason="infra-config defines no post-submit builder for this repo",
                          notes=notes)
    latest = latest_runs(runs)
    grid = {(b, c.sha): state_of(c, b, latest, now, grace) for b in builders for c in commits}

    statuses: dict[str, BuilderStatus] = {}
    red: list[RedSpan] = []
    for b in builders:
        verdicts = [(c, grid[(b, c.sha)]) for c in commits
                    if grid[(b, c.sha)] in (RunState.GREEN, RunState.RED)]
        if not verdicts:
            statuses[b] = BuilderStatus(state="none")
            continue
        newest, state = verdicts[0]
        statuses[b] = BuilderStatus(state=state.value, commit=newest.sha,
                                    url=latest[(b, newest.sha)].url)
        if state is RunState.RED:
            red.append(_red_span(b, commits, grid, latest))

    if red:
        state = TreeState.CLOSED
        reason = "red: " + ", ".join(f"{s.builder} since {s.first_bad[:12]}" for s in red)
    elif all(s.state == "green" for s in statuses.values()):
        state, reason = TreeState.OPEN, "every post-submit builder's newest verdict is green"
    else:
        waiting = sorted(b for b, s in statuses.items() if s.state == "none")
        state, reason = TreeState.UNKNOWN, "no post-submit verdict yet from " + ", ".join(waiting)

    return TreeStatus(repo=repo, branch=branch, head=head, state=state.value, reason=reason,
                      builders=statuses, red=red, coverage=coverage(builders, commits, grid, latest),
                      notes=notes)


def _red_span(builder: str, commits: Sequence[Commit], grid: dict, latest: dict) -> RedSpan:
    """Walk back from the newest verdict (red) to the last green: the regression range."""
    first_bad = latest_bad = ""
    last_good = ""
    window: list[str] = []          # commits newer than last_good, newest first
    for c in commits:
        s = grid[(builder, c.sha)]
        if s is RunState.GREEN and latest_bad:
            last_good = c.sha
            break
        if s is RunState.RED:
            latest_bad = latest_bad or c.sha
            first_bad = c.sha
        if latest_bad:
            window.append(c.sha)
    # Suspects: from first_bad back to (not including) last_good. Commits newer than first_bad in
    # the streak are red already, so they cannot be the first break.
    suspects = list(reversed(window[window.index(first_bad):]))
    return RedSpan(builder=builder, first_bad=first_bad, latest_bad=latest_bad, last_good=last_good,
                   suspects=suspects, url=latest[(builder, first_bad)].url,
                   first_bad_attempt=latest[(builder, first_bad)].attempt)


def coverage(builders: Sequence[str], commits: Sequence[Commit], grid: dict,
             latest: dict) -> Coverage:
    """Commits from the oldest one with any post-submit run (onboarding) to the head."""
    ran = [i for i, c in enumerate(commits) if any((b, c.sha) in latest for b in builders)]
    if not ran:
        return Coverage()
    oldest = max(ran)
    missing, cancelled, pending, retried = [], [], [], []
    for c in commits[:oldest + 1]:
        for b in builders:
            s = grid[(b, c.sha)]
            if s is RunState.MISSING:
                missing.append(f"{c.sha} {b}")
            elif s is RunState.CANCELLED:
                cancelled.append(f"{c.sha} {b}")
                if latest[(b, c.sha)].backfill:
                    retried.append(f"{c.sha} {b}")
            elif s is RunState.PENDING:
                pending.append(f"{c.sha} {b}")
    return Coverage(since=commits[oldest].sha, commits=oldest + 1, missing=missing,
                    cancelled=cancelled, pending=pending, retried=retried)
