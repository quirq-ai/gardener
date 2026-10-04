"""V0-GAR-03: the revert caps, read from infra-config's auto_revert.toml and nothing else.

    [policy] daily_cap reverts created per rolling window_hours, across every repo and failure type
    [build_failure] / [test_failure]: create_daily_limit, submit_daily_limit, max_culprit_age_hours

`decide` turns one culprit into one of three actions:

- `refuse`: no revert is created (cap or type limit reached, unknown failure, unverified culprit,
  already reverted). A refusal is not counted.
- `propose`: a revert PR is created and counted, and a person or the rotation lands it.
- `land`: as propose, and the gardener also queues it to land. Only when the type's submit limit
  has room, the culprit is younger than max_culprit_age_hours, and the repo allows auto-landing.

Which repos allow auto-landing is read from `[policy] auto_land_repos`. infra-config does not have
that field yet; with none, every revert is proposed only, which is what v0 wants for xo-space and
innernet (suraj merges their reverts).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from qqgarden.errors import GardenerError

KINDS = {"build": "build_failure", "test": "test_failure"}


class Action(StrEnum):
    REFUSE = "refuse"
    PROPOSE = "propose"
    LAND = "land"


@dataclass(frozen=True)
class TypeLimits:
    create_daily_limit: int
    submit_daily_limit: int
    max_culprit_age_hours: float


@dataclass(frozen=True)
class Policy:
    daily_cap: int
    window: timedelta
    require_culprit_verification: bool
    only_clean_reverts: bool
    notify: tuple[str, ...]
    types: dict[str, TypeLimits]
    auto_land_repos: frozenset[str]

    @classmethod
    def from_config(cls, cfg: dict) -> "Policy":
        try:
            ar = cfg["auto_revert"]
            p = ar["policy"]
            return cls(
                daily_cap=int(p["daily_cap"]),
                window=timedelta(hours=p["window_hours"]),
                require_culprit_verification=bool(p["require_culprit_verification"]),
                only_clean_reverts=bool(p["only_clean_reverts"]),
                notify=tuple(p["notify"]),
                types={k: TypeLimits(int(ar[section]["create_daily_limit"]),
                                     int(ar[section]["submit_daily_limit"]),
                                     float(ar[section]["max_culprit_age_hours"]))
                       for k, section in KINDS.items()},
                auto_land_repos=frozenset(p.get("auto_land_repos", [])),
            )
        except KeyError as e:
            raise GardenerError(f"infra-config auto_revert.toml is missing {e}; the gardener reverts "
                                "nothing without its caps") from None


@dataclass(frozen=True)
class Counts:
    """What the ledger holds inside the current window."""
    created: int
    created_by_kind: dict[str, int]
    landed_by_kind: dict[str, int]


@dataclass(frozen=True)
class Decision:
    action: Action
    reason: str

    def to_dict(self) -> dict:
        return {"action": self.action.value, "reason": self.reason}


def decide(policy: Policy, repo: str, kind: str, culprit_landed_at: datetime, verified: bool,
           already_reverted: bool, counts: Counts, now: datetime) -> Decision:
    if already_reverted:
        return Decision(Action.REFUSE, "this culprit already has a revert")
    if kind not in policy.types:
        return Decision(Action.REFUSE, f"failure type {kind!r} is never reverted automatically")
    if policy.require_culprit_verification and not verified:
        return Decision(Action.REFUSE, "the culprit is not verified (require_culprit_verification)")
    hours = int(policy.window.total_seconds() // 3600)
    if counts.created >= policy.daily_cap:
        return Decision(Action.REFUSE, f"daily cap reached: {counts.created} of {policy.daily_cap} "
                                       f"reverts created in the last {hours} h")
    limits = policy.types[kind]
    made = counts.created_by_kind.get(kind, 0)
    if made >= limits.create_daily_limit:
        return Decision(Action.REFUSE, f"{kind} create limit reached: {made} of "
                                       f"{limits.create_daily_limit} in the last {hours} h")
    landed = counts.landed_by_kind.get(kind, 0)
    age_h = (now - culprit_landed_at).total_seconds() / 3600
    if landed >= limits.submit_daily_limit:
        why = (f"{kind} reverts are proposed only" if limits.submit_daily_limit == 0 else
               f"{kind} submit limit reached: {landed} of {limits.submit_daily_limit} landed")
    elif age_h >= limits.max_culprit_age_hours:
        why = f"the culprit is {age_h:.1f} h old, over {limits.max_culprit_age_hours:g} h"
    elif repo not in policy.auto_land_repos:
        why = f"{repo} does not allow auto-landed reverts (auto_land_repos)"
    else:
        return Decision(Action.LAND, f"within caps: {counts.created + 1} of {policy.daily_cap} created, "
                                     f"{landed + 1} of {limits.submit_daily_limit} {kind} landed")
    return Decision(Action.PROPOSE, why)
