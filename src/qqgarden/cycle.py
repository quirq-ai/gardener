"""V0-GAR-03: one gardening cycle. Observe every onboarded repo, group its failures, and for each
group with a verified culprit, create a clean revert within the caps.

    observe (GAR-01) -> group (GAR-02) -> culprit -> decide (policy) -> revert -> ledger -> PR

It also backfills coverage (GAR-01): a main commit that a batched push skipped, or whose run was
cancelled, gets its post-submit dispatched, so every commit ends with a result.

Which commit is the culprit:
- a regression range of one commit (its parent has a green verdict) names it directly. It is
  verified once a re-run of that commit's post-submit is red again (a later red commit proves
  nothing: it may be another break). Until then the cycle asks the forge for the re-run, and waits.
- a longer range needs bisection (`qqgarden bisect`, GAR-02), which runs the repo's code; the cycle
  reports the group and the gardener agent bisects it, then runs `qqgarden revert` on the culprit.

Order matters for the caps: the revert commit is made and checked clean first, then the ledger
reserves it (so it counts even if a later step dies), then the branch and PR are created.
"""
from __future__ import annotations

import re
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from qqgarden import groups as groups_mod
from qqgarden import postsubmit, revert
from qqgarden.config import Repo
from qqgarden.errors import GardenerError
from qqgarden.forge import NoIdentity
from qqgarden.ledger import Entry, Ledger, revert_id
from qqgarden.model import CONCLUSIONS, Commit, RedSpan, RunState, TreeStatus
from qqgarden.policy import Action, Decision, Policy, decide


@dataclass
class Outcome:
    repo: str
    group: str
    kind: str
    step: str                  # reverted, proposed, refused, stuck, needs-bisect, awaiting-verification,
                               # backfilled, needs-person, dry-run, error
    reason: str
    culprit: str = ""
    revert: str = ""           # PR URL
    runs: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def assignees(cfg: dict) -> list[str]:
    """The gardener rotation, else the policy owners (org.toml), as GitHub logins."""
    org = cfg.get("org", {})
    rot = next((r for r in org.get("rotation", []) if r.get("name") == "gardener"), {})
    people = rot.get("members") or org.get("roles", {}).get("policy-owner", [])
    return [p.lstrip("@") for p in people if p]


def pr_body(g: groups_mod.Group, culprit: Commit, decision_reason: str, action: Action,
            span_runs: list[str]) -> str:
    runs = "\n".join(f"- {u}" for u in span_runs) or "- (none recorded)"
    land = ("The gardener queued it to land through the gate." if action is Action.LAND else
            f"Proposed only: {decision_reason}. A person or the gardener rotation lands it.")
    return f"""Automatic revert by the quirq infra gardener (V0-GAR-03).

Culprit: {culprit.sha} {inline(culprit.title, 200)}
Failure group: `{g.key}` ({g.kind} failure in {", ".join(g.builders)})
Regression range: {g.last_good[:12] or "(none)"}..{g.first_bad[:12]}

Red post-submit runs:
{runs}

{land}

To reland, revert this revert with the fix and say what changed. If this revert is wrong,
close it and say why in the gardener thread; the culprit is then not reverted again.
"""


# Attempts of one run the cycle asks for before giving up on verifying it (a person then looks).
MAX_ATTEMPTS = 3


@dataclass
class Verification:
    state: str                                  # verified, waiting or flaky
    reason: str
    reruns: list[tuple[str, bool]] = field(default_factory=list)   # (run url, failed jobs only)


def verify(spans: list[RedSpan], backend, repo: Repo) -> Verification:
    """With and without the suspect, for every builder in the group: the culprit's own post-submit
    run is red on its last two attempts and was never green, and its parent's (the last good
    commit) is green on its last two and was never red. A later red commit proves nothing (it may
    be another break), one red may be a flake, and a parent that went green once may hide an
    environment break; attempts are read from the backend, not inferred from an attempt number."""
    if not spans or any(not s.last_good or not s.last_good_url for s in spans):
        return Verification("waiting", "no green parent run to compare with")
    if any(s.first_bad_running or s.last_good_running for s in spans):
        return Verification("waiting", "a re-run is in progress")
    reruns, flaky = [], []
    for s in spans:
        culprit_runs = backend.attempts(repo, s.url, s.first_bad_attempt)
        parent_runs = backend.attempts(repo, s.last_good_url, s.last_good_attempt)
        # "Without the suspect" must be now: the parent's last green started after the culprit
        # first failed, so an environment that broke since then cannot verify an innocent commit.
        failed_at = postsubmit.parse_time(culprit_runs[0][1]) if culprit_runs[0][1] else None
        last = parent_runs[-1][1]
        fresh = failed_at is not None and bool(last) and postsubmit.parse_time(last) > failed_at
        for url, attempts, want, other, failed_only, ok in (
                (s.url, culprit_runs, RunState.RED, RunState.GREEN, True, True),
                (s.last_good_url, parent_runs, RunState.GREEN, RunState.RED, False, fresh)):
            states = [CONCLUSIONS.get(c, RunState.CANCELLED) for c, _ in attempts]
            if other in states:
                flaky.append(f"{url} was {other.value} on one attempt and {want.value} on another")
            elif states[-2:] != [want, want] or not ok:
                if len(states) >= MAX_ATTEMPTS:
                    flaky.append(f"{url} gave no two {want.value} attempts in a row in {len(states)}"
                                 + ("" if ok else ", the last after the culprit failed"))
                else:
                    reruns.append((url, failed_only))
    if flaky:
        return Verification("flaky", "; ".join(flaky))
    if reruns:
        return Verification("waiting", "re-running " + ", ".join(u for u, _ in reruns), reruns)
    return Verification("verified", "red twice on its own run, green twice on its parent's")


# TODO(expert): a config value; backfills in flight per repo, so a long batched history fills in
# over a few cycles instead of flooding the runners.
BACKFILL_PER_CYCLE = 10


def backfill(repo: Repo, status: TreeStatus, commits: list[Commit], backend, dry_run: bool) -> list[Outcome]:
    """Dispatch the post-submit for each hole in coverage, oldest first. A run cancelled again
    after a backfill is left to a person, so nothing retries forever."""
    cov = status.coverage
    base = dict(repo=repo.name, group="coverage", kind="")
    out = []
    if cov.retried:
        out.append(Outcome(**base, step="needs-person",
                           reason="cancelled again after a backfill: " + ", ".join(cov.retried)))
    holes = [h for h in cov.missing + cov.cancelled if h not in cov.retried]
    room = BACKFILL_PER_CYCLE - len(cov.backfilling)
    if not holes or room <= 0:
        return out
    age = {c.sha: i for i, c in enumerate(commits)}   # newest first, so oldest has the largest index
    holes = sorted(holes, key=lambda h: -age.get(h.split(" ", 1)[0], 0))[:room]
    forge = backend.forge
    problem = "dry run" if dry_run else ("no forge" if forge is None else forge.identity_problem())
    if problem:
        return out + [Outcome(**base, step="dry-run" if dry_run else "refused",
                              reason=f"would backfill {len(holes)}: {problem}")]
    done, failed = [], set()
    for h in holes:
        sha, builder = h.split(" ", 1)
        if builder in failed:      # one builder's broken dispatch never stops the others'
            continue
        try:
            forge.backfill(repo, builder, sha)
        except GardenerError as e:
            out.append(Outcome(**base, step="error", reason=f"backfilling {h}: {e}"))
            failed.add(builder)
            continue
        done.append(h)
    if done:
        out.append(Outcome(**base, step="backfilled", reason="dispatched " + ", ".join(done)))
    return out


def run(cfg: dict, repos: list[Repo], backend, ledger: Ledger, policy: Policy, now: datetime,
        grace: timedelta, limit: int, evidence=None, dry_run: bool = False,
        records=None) -> list[Outcome]:
    if evidence is None:
        from qqgarden.evidence import Evidence
        evidence = Evidence(backend, {r.name: r for r in repos})
    out: list[Outcome] = []
    for repo in repos:
        status, commits = postsubmit.observe(backend, repo, limit, now, grace)
        out += backfill(repo, status, commits, backend, dry_run)
        out += handle_repo(cfg, repo, status, commits, backend, ledger, policy, now, evidence, dry_run)
    if records is not None and not dry_run:
        out += follow_up(repos, backend, ledger, records, now)
    return out


# A revert not landed after this long is taken as abandoned: the cycle stops asking the forge.
LANDED_POLL = timedelta(days=14)


def inline(text: str, limit: int = 0) -> str:
    """A commit title shown in markdown, as inline code: its author cannot mention people, link or
    embed anything through the gardener's issues and PRs. With `limit`, the whole result (backticks
    included) fits in it: the title is cut inside the backticks, so the code span always closes."""
    body = " ".join(text.replace("`", "'").split())
    if limit and len(body) + 2 > limit:
        body = body[:max(limit - 5, 0)] + "..."
    return "`" + body + "`"


# Issue titles longer than this are cut by the results store's issue mirror (test-pipelines);
# a summary that fits never loses the closing backtick of its inline title.
SUMMARY_MAX = 80


def revert_title(culprit: str) -> str:
    """The revert PR's title and commit subject: never the culprit's own title, which would reach
    main's history and the PR list raw (`fixes #N` acting again, mentions, links)."""
    return f"Revert {culprit[:12]} (qq gardener)"


def follow_up(repos: list[Repo], backend, ledger: Ledger, records, now: datetime) -> list[Outcome]:
    """V0-GAR-04: every created revert has a failure record and postmortem stub; once the revert
    has landed, the record links it as the fix. Safe to repeat: records and links are idempotent.
    One entry's failure never stops the others, and finished records cost no API calls."""
    from qqgarden.postsubmit import parse_time
    by_name = {r.name: r for r in repos}
    links = ledger.revert_links()
    out = []
    for e in ledger.entries():
        repo, url = by_name.get(e.repo), links.get(e.id)
        base = dict(repo=e.repo, group=e.group, kind=e.kind, culprit=e.culprit, revert=url or "",
                    runs=list(e.runs))
        if repo is None:
            continue
        if not url:
            if not records.exists(repo, e.culprit):
                out.append(Outcome(**base, step="unlinked",
                                   reason="reserved, but no revert PR was linked; no record (for a person)"))
            continue
        try:
            forge = backend.forge
            if records.done(repo, e.culprit):
                continue
            g = groups_mod.Group(repo=e.repo, first_bad=e.culprit, last_good=e.last_good,
                                 suspects=[e.culprit], builders=[], kind=e.kind, tests=list(e.tests),
                                 runs=list(e.runs))
            head = f"{e.kind} break in {e.repo[:40]}: reverted "
            summary = head + inline(e.title or e.culprit[:12], SUMMARY_MAX - len(head))
            state = records.open(repo, e.culprit, forge.commit_url(repo, e.culprit),
                                 e.culprit_landed_at, url, g, summary)
            step = "recorded"
            if not state.links.get("fix") and now - parse_time(e.created_at) <= LANDED_POLL:
                landed = forge.landed(repo, url)
                if landed:
                    state = records.link_fix(repo, e.culprit, landed)
                    step = "fix-linked"
            ledger.sync(["failures", "mirrors"], f"failures: {e.id}")
        except Exception as err:   # noqa: BLE001 - one record never stops the others
            out.append(Outcome(**base, step="record-failed", reason=f"{type(err).__name__}: {err}"))
            continue
        out.append(Outcome(**base, step=step, reason=f"record {state.record.id}: "
                           + ("closed" if state.closed else "needs " + ", ".join(state.missing))))
    return out


def handle_repo(cfg, repo: Repo, status: TreeStatus, commits: list[Commit], backend, ledger: Ledger,
                policy: Policy, now: datetime, evidence, dry_run: bool) -> list[Outcome]:
    out = []
    by_sha = {c.sha: c for c in commits}
    # Reverts act on GitHub's own run and step data only. TODO(expert): let stored verdicts classify
    # once test-pipelines checks each record's origin; today any workflow run can write one.
    for g in groups_mod.group(status, evidence, store_classifies=False):
        base = dict(repo=repo.name, group=g.key, kind=g.kind, runs=g.runs)
        try:
            out.append(handle_group(cfg, repo, g, status, by_sha, backend, ledger, policy, now, dry_run))
        except GardenerError as e:
            # One stuck group never stops the others, or the other repos.
            out.append(Outcome(**base, step="error", reason=str(e)))
    return out


def handle_group(cfg, repo: Repo, g, status: TreeStatus, by_sha: dict, backend, ledger: Ledger,
                 policy: Policy, now: datetime, dry_run: bool) -> Outcome:
    base = dict(repo=repo.name, group=g.key, kind=g.kind, runs=g.runs)
    if len(g.suspects) != 1 or not g.last_good:
        return Outcome(**base, step="needs-bisect",
                       reason=f"{len(g.suspects)} suspects; bisect them with `qqgarden bisect`")
    spans = [s for s in status.red if (s.last_good, s.first_bad) == (g.last_good, g.first_bad)]
    culprit = by_sha[g.first_bad]
    if any(s.first_bad_backfill for s in spans):
        # A backfill runs main's newer workflow on the old commit, so its red may be the workflow's.
        return Outcome(**base, culprit=culprit.sha, step="needs-bisect",
                       reason="the first red is a backfill run (newer workflow on an older commit); "
                              "confirm with `qqgarden bisect`")
    if GARDENER_REVERT.match(culprit.title):
        return Outcome(**base, culprit=culprit.sha, step="needs-person",
                       reason="the culprit is a gardener revert; the gardener never reverts its own revert")
    if g.kind not in policy.types:
        return Outcome(**base, culprit=culprit.sha, step="refused",
                       reason=f"{g.kind} failures are never reverted automatically; for a person")
    if policy.require_culprit_verification:
        v = verify(spans, backend, repo)
        if v.state == "flaky":
            return Outcome(**base, culprit=culprit.sha, step="needs-person", reason="not verified: " + v.reason)
        if v.state == "waiting":
            reason = v.reason
            if v.reruns and not dry_run and backend.forge and not backend.forge.identity_problem():
                try:
                    asked = [u for u, failed_only in v.reruns if backend.forge.rerun(repo, u, failed_only)]
                except GardenerError as e:
                    # e.g. a run too old to re-run: asking again every cycle would never verify it
                    return Outcome(**base, culprit=culprit.sha, step="needs-person",
                                   reason=f"not verified: the forge refused a re-run ({e})")
                reason = ("asked for re-runs of " + ", ".join(asked)) if asked else reason
            return Outcome(**base, culprit=culprit.sha, step="awaiting-verification", reason=reason)
    return revert_culprit(cfg, repo, g, culprit, backend, ledger, policy, now, dry_run, verified=True,
                          status=status, order={sha: i for i, sha in enumerate(by_sha)})


# The subject of every revert the gardener makes (revert_title). A culprit titled like this is
# never reverted: a person decides. Anyone can title a commit this way, which only costs a person.
# A squash or merge queue lands it as "<PR title> (#N)".
GARDENER_REVERT = re.compile(r"^Revert [0-9a-f]{12} \(qq gardener\)( \(#\d+\))?$")


def red_after(status: TreeStatus | None, g: groups_mod.Group, order: dict[str, int], sha: str) -> bool:
    """Some builder of the group is red on `sha` or a newer commit (`order`: newest first, 0 up)."""
    # A landed revert is newer than its culprit, which is listed, so it is listed too once the
    # history is read again; until then its own post-submit cannot have reported either.
    if status is None or sha not in order:
        return False
    spans = [s for s in status.red if (s.last_good, s.first_bad) == (g.last_good, g.first_bad)]
    return any(order.get(s.latest_bad, len(order)) <= order[sha] for s in spans)


def revert_culprit(cfg, repo: Repo, g: groups_mod.Group, culprit: Commit, backend, ledger: Ledger,
                   policy: Policy, now: datetime, dry_run: bool, verified: bool,
                   status: TreeStatus | None = None, order: dict[str, int] | None = None) -> Outcome:
    """`status` and `order` (commit -> age, newest 0) let it tell a landed revert that left the
    tree red from one whose own post-submit has not reported yet."""
    order = order or {}
    base = dict(repo=repo.name, group=g.key, kind=g.kind, runs=g.runs, culprit=culprit.sha)
    rid = revert_id(repo.name, culprit.sha)
    branch = revert.branch_name(culprit.sha)
    forge = backend.forge
    if forge is None:
        raise GardenerError(f"backend for {repo.name} has no forge")
    if GARDENER_REVERT.match(culprit.title):
        return Outcome(**base, step="needs-person",
                       reason="the culprit is a gardener revert; the gardener never reverts its own revert")
    linked = ledger.revert_links().get(rid, "")
    existing = linked or forge.existing_revert(repo, culprit.sha)
    if existing:
        landed = forge.landed(repo, existing)
        if landed and red_after(status, g, order, landed.rstrip("/").rsplit("/", 1)[-1]):
            # The range stays anchored at the reverted commit, so a second break after it would
            # hide behind "already has a revert": a person looks at the still-red tree.
            return Outcome(**base, step="needs-person", revert=existing,
                           reason="still red after its revert landed: another break, or the revert "
                                  "did not fix it; bisect from the revert")
        return Outcome(**base, step="refused", reason="this culprit already has a revert", revert=existing)
    if forge.branch_exists(repo, branch):
        return Outcome(**base, step="stuck", reason=(
            f"branch {branch} exists with no revert PR; a person must delete it or open its PR"))
    reserved = ledger.entry(rid)

    def judge() -> "Decision":
        return decide(policy, repo.name, g.kind, postsubmit.parse_time(culprit.landed_at), verified,
                      already_reverted=False, counts=ledger.counts(now - policy.window, exclude=rid),
                      now=now)
    d = judge()
    if reserved is not None and d.action is Action.REFUSE:
        # Reserved and counted by an earlier cycle that died before the PR existed: finish it, but
        # never land it on a stale decision.
        d = Decision(Action.PROPOSE, f"finishing an earlier reservation ({d.reason})")
    if d.action is Action.REFUSE:
        return Outcome(**base, step="refused", reason=d.reason)
    action, reason = d.action, d.reason
    problem = forge.identity_problem()
    if dry_run or problem:
        return Outcome(**base, step="dry-run",
                       reason=f"would {action.value}: {reason}" + (f"; {problem}" if problem else ""))
    footer = f"qq-gardener: {g.kind} failure {g.key}; caps from infra-config auto_revert.toml"
    with tempfile.TemporaryDirectory(prefix="qqgarden-revert-") as tmp:
        made = revert.make(forge.clone_url(repo), repo.default_branch, culprit.sha, Path(tmp) / "w",
                           footer, env=forge.auth_env())
        if not made.clean:
            return Outcome(**base, step="refused", reason=made.reason)
        if reserved is None:
            # Re-decided after any concurrent writer's records are pulled in, so two writers
            # cannot both take the last slot under the cap.
            ledger.reserve(Entry(id=rid, repo=repo.name, culprit=culprit.sha, kind=g.kind,
                                 action=action.value, created_at=timestamp(now), reason=reason,
                                 group=g.key, runs=g.runs, last_good=g.last_good, title=culprit.title,
                                 culprit_landed_at=culprit.landed_at, tests=g.tests),
                           recheck=lambda: "" if (r := judge()).action is action else
                           f"now {r.action.value}: {r.reason}")
        forge.push(made.workdir, repo, branch)
        url = forge.open_revert(repo, branch, made.base, revert_title(culprit.sha),
                                pr_body(g, culprit, reason, action, g.runs), assignees(cfg))
    ledger.set_revert(rid, url)
    if action is Action.LAND:
        forge.queue_land(repo, url, made.commit)
        ledger.mark_landed(rid, timestamp(now))
        return Outcome(**base, step="reverted", reason=reason, revert=url)
    return Outcome(**base, step="proposed", reason=reason, revert=url)


def timestamp(t: datetime) -> str:
    return t.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
