"""V0-GAR-03: one gardening cycle. Observe every onboarded repo, group its failures, and for each
group with a verified culprit, create a clean revert within the caps.

    observe (GAR-01) -> group (GAR-02) -> culprit -> decide (policy) -> revert -> ledger -> PR

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
from qqgarden.model import Commit, RedSpan, TreeStatus
from qqgarden.policy import Action, Decision, Policy, decide


@dataclass
class Outcome:
    repo: str
    group: str
    kind: str
    step: str                  # reverted, proposed, refused, stuck, needs-bisect, awaiting-verification, dry-run, error
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

Culprit: {culprit.sha} "{culprit.title}"
Failure group: `{g.key}` ({g.kind} failure in {", ".join(g.builders)})
Regression range: {g.last_good[:12] or "(none)"}..{g.first_bad[:12]}

Red post-submit runs:
{runs}

{land}

To reland, revert this revert with the fix and say what changed. If this revert is wrong,
close it and say why in the gardener thread; the culprit is then not reverted again.
"""


def verified(spans: list[RedSpan]) -> bool:
    """LUCI-style: the culprit's own post-submit was re-run and is red again, and its parent (the
    last good commit) is green, for every builder in the group. A later red commit is not proof:
    it may be a different break, and the first red may have been a flake."""
    return bool(spans) and all(s.last_good and s.first_bad_attempt > 1 for s in spans)


def run(cfg: dict, repos: list[Repo], backend, ledger: Ledger, policy: Policy, now: datetime,
        grace: timedelta, limit: int, evidence=None, dry_run: bool = False) -> list[Outcome]:
    if evidence is None:
        from qqgarden.evidence import Evidence
        evidence = Evidence(backend, {r.name: r for r in repos})
    out: list[Outcome] = []
    for repo in repos:
        status, commits = postsubmit.observe(backend, repo, limit, now, grace)
        out += handle_repo(cfg, repo, status, commits, backend, ledger, policy, now, evidence, dry_run)
    return out


def handle_repo(cfg, repo: Repo, status: TreeStatus, commits: list[Commit], backend, ledger: Ledger,
                policy: Policy, now: datetime, evidence, dry_run: bool) -> list[Outcome]:
    out = []
    by_sha = {c.sha: c for c in commits}
    for g in groups_mod.group(status, evidence):
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
    if policy.require_culprit_verification and not verified(spans):
        reason = "red once; waiting for a re-run of its post-submit to be red again"
        if not dry_run and backend.forge and not backend.forge.identity_problem():
            asked = [s.url for s in spans if s.first_bad_attempt == 1 and backend.forge.rerun(repo, s.url)]
            if asked:
                reason = "red once; asked for a re-run of " + ", ".join(asked)
        return Outcome(**base, culprit=culprit.sha, step="awaiting-verification", reason=reason)
    return revert_culprit(cfg, repo, g, culprit, backend, ledger, policy, now, dry_run, verified=True)


def revert_culprit(cfg, repo: Repo, g: groups_mod.Group, culprit: Commit, backend, ledger: Ledger,
                   policy: Policy, now: datetime, dry_run: bool, verified: bool) -> Outcome:
    base = dict(repo=repo.name, group=g.key, kind=g.kind, runs=g.runs, culprit=culprit.sha)
    rid = revert_id(repo.name, culprit.sha)
    branch = revert.branch_name(culprit.sha)
    forge = backend.forge
    if forge is None:
        raise GardenerError(f"backend for {repo.name} has no forge")
    linked = ledger.revert_links().get(rid, "")
    existing = linked or forge.existing_revert(repo, culprit.sha)
    if existing:
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
                                 group=g.key, runs=g.runs),
                           recheck=lambda: "" if judge().action is not Action.REFUSE else judge().reason)
        forge.push(made.workdir, repo, branch)
        url = forge.open_revert(repo, branch, made.base, f'Revert "{made.title}"',
                                pr_body(g, culprit, reason, action, g.runs), assignees(cfg))
    ledger.set_revert(rid, url)
    if action is Action.LAND:
        forge.queue_land(repo, url)
        ledger.mark_landed(rid, timestamp(now))
        return Outcome(**base, step="reverted", reason=reason, revert=url)
    return Outcome(**base, step="proposed", reason=reason, revert=url)


def timestamp(t: datetime) -> str:
    return t.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
