from datetime import datetime, timedelta, timezone

import pytest

from qqgarden.errors import GardenerError
from qqgarden.ledger import Entry, Ledger
from qqgarden.policy import Action, Counts, Policy, decide

NOW = datetime(2026, 10, 4, 12, 0, tzinfo=timezone.utc)
NONE = Counts(0, {}, {})


def pol(cfg, **policy):
    cfg["auto_revert"]["policy"].update(policy)
    return Policy.from_config(cfg)


def test_policy_comes_from_config(cfg):
    p = Policy.from_config(cfg)
    ar = cfg["auto_revert"]
    assert p.daily_cap == ar["policy"]["daily_cap"]
    assert p.window == timedelta(hours=ar["policy"]["window_hours"])
    assert p.types["build"].submit_daily_limit == ar["build_failure"]["submit_daily_limit"]
    assert p.types["test"].submit_daily_limit == ar["test_failure"]["submit_daily_limit"]
    assert p.auto_land_repos == frozenset()      # not in infra-config yet: propose only


def test_missing_cap_refuses_to_run(cfg):
    del cfg["auto_revert"]["policy"]["daily_cap"]
    with pytest.raises(GardenerError, match="daily_cap"):
        Policy.from_config(cfg)


def young():
    return NOW - timedelta(hours=1)


def test_build_break_lands_when_the_repo_allows_it(cfg):
    p = pol(cfg, auto_land_repos=["xo-space"])
    assert decide(p, "xo-space", "build", young(), True, False, NONE, NOW).action is Action.LAND


def test_v0_repos_are_proposed_only(cfg):
    d = decide(Policy.from_config(cfg), "xo-space", "build", young(), True, False, NONE, NOW)
    assert d.action is Action.PROPOSE and "auto_land_repos" in d.reason


def test_test_failures_are_proposed_only(cfg):
    p = pol(cfg, auto_land_repos=["xo-space"])
    d = decide(p, "xo-space", "test", young(), True, False, NONE, NOW)
    assert d.action is Action.PROPOSE and "proposed only" in d.reason


def test_old_culprit_is_proposed_not_landed(cfg):
    p = pol(cfg, auto_land_repos=["xo-space"])
    old = NOW - timedelta(hours=p.types["build"].max_culprit_age_hours + 0.1)
    d = decide(p, "xo-space", "build", old, True, False, NONE, NOW)
    assert d.action is Action.PROPOSE and "old" in d.reason


def test_submit_limit(cfg):
    p = pol(cfg, auto_land_repos=["xo-space"])
    n = p.types["build"].submit_daily_limit
    d = decide(p, "xo-space", "build", young(), True, False, Counts(n, {"build": n}, {"build": n}), NOW)
    assert d.action is Action.PROPOSE and "submit limit" in d.reason


def test_unknown_unverified_and_duplicate_are_refused(cfg):
    p = Policy.from_config(cfg)
    assert decide(p, "x", "unknown", young(), True, False, NONE, NOW).action is Action.REFUSE
    assert decide(p, "x", "build", young(), False, False, NONE, NOW).action is Action.REFUSE
    assert decide(p, "x", "build", young(), True, True, NONE, NOW).action is Action.REFUSE


def test_flood_refuses_the_eleventh_revert_in_24h(cfg, tmp_path):
    """V0-GAR-03 done-when, offline half: the cap from auto_revert.toml holds across repos and types."""
    p = Policy.from_config(cfg)
    ledger = Ledger(tmp_path)
    made = []
    for i in range(p.daily_cap + 1):
        repo, kind = ("xo-space", "build") if i % 2 else ("innernet", "test")
        d = decide(p, repo, kind, young(), True, False, ledger.counts(NOW - p.window), NOW)
        if d.action is not Action.REFUSE:
            ledger.reserve(Entry(id=f"{repo}-{i:012x}", repo=repo, culprit=f"{i:040x}", kind=kind,
                                 action=d.action.value, created_at=(NOW - timedelta(minutes=i)).strftime("%Y-%m-%dT%H:%M:%SZ")))
        made.append(d)
    assert [d.action is not Action.REFUSE for d in made] == [True] * p.daily_cap + [False]
    assert "daily cap reached" in made[-1].reason


def test_old_reverts_leave_the_window(cfg, tmp_path):
    p = Policy.from_config(cfg)
    ledger = Ledger(tmp_path)
    for i in range(p.daily_cap):
        ledger.reserve(Entry(id=f"r{i}", repo="xo-space", culprit=f"{i:040x}", kind="build", action="propose",
                             created_at=(NOW - p.window - timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%SZ")))
    d = decide(p, "xo-space", "build", young(), True, False, ledger.counts(NOW - p.window), NOW)
    assert d.action is not Action.REFUSE


def test_ledger_is_write_once(tmp_path):
    ledger = Ledger(tmp_path)
    e = Entry(id="a", repo="r", culprit="c" * 40, kind="build", action="propose", created_at="2026-10-04T11:00:00Z")
    ledger.reserve(e)
    with pytest.raises(GardenerError, match="write-once"):
        ledger.reserve(e)
    ledger.mark_landed("a", "2026-10-04T11:01:00Z")
    c = ledger.counts(NOW - timedelta(hours=24))
    assert c.created == 1 and c.landed_by_kind == {"build": 1}
    assert ledger.entry("a") == e and ledger.entry("b") is None


def test_published_ledger_pushes_each_record(tmp_path):
    import subprocess
    def g(cwd, *a):
        return subprocess.run(["git", *a], cwd=cwd, check=True, capture_output=True, text=True).stdout
    remote = tmp_path / "remote.git"
    g(tmp_path, "init", "-q", "--bare", "-b", "ledger", str(remote))
    g(tmp_path, "clone", "-q", str(remote), "work")
    work = tmp_path / "work"
    ledger = Ledger(work, publish="ledger")
    ledger.reserve(Entry(id="a", repo="r", culprit="c" * 40, kind="build", action="propose",
                         created_at="2026-10-04T11:00:00Z"))
    assert "reverts/a.json" in g(remote, "ls-tree", "-r", "--name-only", "ledger")
