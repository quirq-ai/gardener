import json
from datetime import timedelta

from forgerepo import build, remote_main_state, snapshot
from history import NOW

from qqgarden import cli, config, cycle
from qqgarden.backends import load
from qqgarden.ledger import Entry, Ledger
from qqgarden.policy import Policy


def run_cycle(cfg, snap, tmp_path, dry_run=False):
    backend = load("snapshot", path=snap, forge_dir=tmp_path / "forge")
    repos = [r for r in config.repos(cfg) if r.name == "xo-space"]
    return cycle.run(cfg, repos, backend, Ledger(tmp_path / "ledger"), Policy.from_config(cfg), NOW,
                     timedelta(minutes=15), 100, dry_run=dry_run)


def test_planted_build_break_is_reverted_within_30_minutes(cfg, tmp_path):
    """V0-GAR-03 done-when, offline half. The break lands 20 min before the cycle, its post-submit
    and the next commit's are red (so the culprit is verified), and the cycle lands a clean revert."""
    cfg["auto_revert"]["policy"]["auto_land_repos"] = ["xo-space"]
    xo = build(tmp_path)
    culprit = xo["commits"][1]
    assert remote_main_state(tmp_path) == "broken"
    [o] = run_cycle(cfg, snapshot(tmp_path, **{"xo-space": xo}), tmp_path)
    assert o.step == "reverted", o.reason
    assert o.culprit == culprit["sha"] and o.kind == "build"
    from qqgarden.postsubmit import parse_time
    assert NOW - parse_time(culprit["landed_at"]) <= timedelta(minutes=30)
    assert remote_main_state(tmp_path) == "ok"          # main is green again
    pr = json.loads((tmp_path / "forge" / "prs" / "1.json").read_text())
    assert pr["title"] == 'Revert "commit 5"' and culprit["sha"] in pr["body"]
    assert pr["assignees"] == ["sharmasuraj0123"]        # org.toml policy-owner, rotation empty
    led = Ledger(tmp_path / "ledger")
    assert led.counts(NOW - timedelta(hours=24)).created == 1
    # A second cycle does not revert it again.
    [o2] = run_cycle(cfg, snapshot(tmp_path, **{"xo-space": xo}), tmp_path)
    assert o2.step == "refused" and "already has a revert" in o2.reason


def test_v0_config_proposes_and_leaves_main_alone(cfg, tmp_path):
    xo = build(tmp_path)
    [o] = run_cycle(cfg, snapshot(tmp_path, **{"xo-space": xo}), tmp_path)
    assert o.step == "proposed" and "auto_land_repos" in o.reason
    assert remote_main_state(tmp_path) == "broken"
    assert o.revert == "local://xo-space/pull/1"


def test_store_records_alone_never_make_a_failure_revertable(cfg, tmp_path):
    """Any workflow run can write the results store today, so a stored unexpected test must not
    turn a failure GitHub cannot explain into a revert."""
    from qqgarden.evidence import Evidence
    xo = build(tmp_path)
    xo["failed_steps"] = {}                      # GitHub names no failed step
    backend = load("snapshot", path=snapshot(tmp_path, **{"xo-space": xo}), forge_dir=tmp_path / "forge")
    repos = [r for r in config.repos(cfg) if r.name == "xo-space"]

    class Forged(Evidence):
        def unexpected_tests(self, repo, builder, commit):
            return ["planted::test"]
    ev = Forged(backend, {r.name: r for r in repos})
    [o] = cycle.run(cfg, repos, backend, Ledger(tmp_path / "ledger"), Policy.from_config(cfg), NOW,
                    timedelta(minutes=15), 100, evidence=ev)
    assert o.kind == "unknown" and o.step != "proposed" and o.revert == ""
    assert not (tmp_path / "ledger").exists()


def test_red_once_waits_for_verification(cfg, tmp_path):
    xo = build(tmp_path, n=5, brk=5, rerun_red=False)
    [o] = run_cycle(cfg, snapshot(tmp_path, **{"xo-space": xo}), tmp_path)
    assert o.step == "awaiting-verification"
    assert not (tmp_path / "ledger").exists()


def test_conflicting_revert_is_refused_and_not_counted(cfg, tmp_path):
    xo = build(tmp_path, conflict=True)
    [o] = run_cycle(cfg, snapshot(tmp_path, **{"xo-space": xo}), tmp_path)
    assert o.step == "refused" and "not a clean revert" in o.reason
    assert Ledger(tmp_path / "ledger").counts(NOW - timedelta(hours=24)).created == 0


def test_cap_reached_refuses(cfg, tmp_path):
    led = Ledger(tmp_path / "ledger")
    for i in range(Policy.from_config(cfg).daily_cap):
        led.reserve(Entry(id=f"innernet-{i:012x}", repo="innernet", culprit=f"{i:040x}", kind="test",
                          action="propose", created_at="2026-10-04T11:00:00Z"))
    xo = build(tmp_path)
    [o] = run_cycle(cfg, snapshot(tmp_path, **{"xo-space": xo}), tmp_path)
    assert o.step == "refused" and "daily cap reached" in o.reason


def test_dry_run_creates_nothing(cfg, tmp_path):
    xo = build(tmp_path)
    [o] = run_cycle(cfg, snapshot(tmp_path, **{"xo-space": xo}), tmp_path, dry_run=True)
    assert o.step == "dry-run" and o.reason.startswith("would propose")
    assert not (tmp_path / "forge").exists()


def test_github_without_identity_reports_only(cfg, tmp_path):
    from qqgarden.backends import github
    b = github.Backend(token="", write_token="")
    assert "QQ_GARDENER_TOKEN" in b.identity_problem()
    assert b.auth_env() == {}
    env = github.Backend(token="", write_token="t0k").auth_env()
    assert "t0k" not in env["GIT_CONFIG_VALUE_0"]       # base64, and only in env


def test_cli_cycle(config_root, tmp_path, capsys):
    xo = build(tmp_path)
    snap = snapshot(tmp_path, **{"xo-space": xo})
    rc = cli.main(["cycle", "--config", str(config_root), "--backend", "snapshot", "--snapshot", str(snap),
                   "--repo", "xo-space", "--ledger", str(tmp_path / "ledger"), "--now", NOW.isoformat(),
                   "--json"])
    assert rc == 0
    [o] = json.loads(capsys.readouterr().out)
    assert o["step"] == "proposed"


def test_cli_revert_needs_verification(config_root, tmp_path, capsys):
    xo = build(tmp_path)
    snap = snapshot(tmp_path, **{"xo-space": xo})
    args = ["revert", "--config", str(config_root), "--backend", "snapshot", "--snapshot", str(snap),
            "--repo", "xo-space", "--ledger", str(tmp_path / "ledger"), "--now", NOW.isoformat(),
            "--culprit", xo["commits"][1]["sha"][:12], "--kind", "build"]
    assert cli.main(args) == 2
    assert "verified" in capsys.readouterr().err
    assert cli.main(args + ["--verified"]) == 0
    assert "proposed" in capsys.readouterr().out


def test_a_later_red_commit_does_not_verify_an_innocent_one(cfg, tmp_path):
    """A flaky red on commit 5 and a real break on commit 6: 5 is not reverted on 6's evidence."""
    cfg["auto_revert"]["policy"]["auto_land_repos"] = ["xo-space"]
    xo = build(tmp_path, rerun_red=False)
    [o] = run_cycle(cfg, snapshot(tmp_path, **{"xo-space": xo}), tmp_path)
    assert o.step == "awaiting-verification"
    assert not (tmp_path / "forge").exists()


def test_a_revert_branch_without_a_pr_is_stuck_not_a_crash(cfg, tmp_path):
    from forgerepo import g
    xo = build(tmp_path)
    culprit = xo["commits"][1]["sha"]
    g(tmp_path / "xo-space.git", "branch", f"qq-gardener/revert-{culprit[:12]}", "main")
    [o] = run_cycle(cfg, snapshot(tmp_path, **{"xo-space": xo}), tmp_path)
    assert o.step == "stuck" and "a person must" in o.reason
    assert Ledger(tmp_path / "ledger").counts(NOW - timedelta(hours=24)).created == 0


def test_one_failing_group_does_not_stop_the_cycle(cfg, tmp_path, monkeypatch):
    from qqgarden.errors import GardenerError
    xo = build(tmp_path)

    def boom(*a, **k):
        raise GardenerError("forge said no")
    monkeypatch.setattr(cycle, "revert_culprit", boom)
    [o] = run_cycle(cfg, snapshot(tmp_path, **{"xo-space": xo}), tmp_path)
    assert o.step == "error" and "forge said no" in o.reason


def test_a_concurrent_writer_taking_the_last_slot_wins(cfg, tmp_path):
    """Two writers each see 9 of 10; the second to push re-decides after pulling and backs out."""
    import subprocess
    from forgerepo import g
    from qqgarden.errors import GardenerError
    import pytest
    cap = Policy.from_config(cfg).daily_cap
    remote = tmp_path / "ledger.git"
    g(tmp_path, "init", "-q", "--bare", "-b", "ledger", str(remote))
    g(tmp_path, "clone", "-q", str(remote), "seed")
    seed = Ledger(tmp_path / "seed", publish="ledger")
    for i in range(cap - 1):
        seed.reserve(Entry(id=f"s{i}", repo="innernet", culprit=f"{i:040x}", kind="test", action="propose",
                           created_at="2026-10-04T11:00:00Z"))
    g(tmp_path, "clone", "-q", "-b", "ledger", str(remote), "a")
    g(tmp_path, "clone", "-q", "-b", "ledger", str(remote), "b")
    a, b = Ledger(tmp_path / "a", publish="ledger"), Ledger(tmp_path / "b", publish="ledger")
    since = NOW - timedelta(hours=24)

    def recheck(ledger, rid):
        return lambda: "" if ledger.counts(since, exclude=rid).created < cap else "daily cap reached"
    a.reserve(Entry(id="ra", repo="xo-space", culprit="a" * 40, kind="build", action="propose",
                    created_at="2026-10-04T11:30:00Z"), recheck=recheck(a, "ra"))
    with pytest.raises(GardenerError, match="another writer"):
        b.reserve(Entry(id="rb", repo="xo-space", culprit="b" * 40, kind="build", action="propose",
                        created_at="2026-10-04T11:30:00Z"), recheck=recheck(b, "rb"))
    names = g(remote, "ls-tree", "-r", "--name-only", "ledger")
    assert "reverts/ra.json" in names and "reverts/rb.json" not in names


def test_two_writers_saving_the_same_record_leave_a_clean_worktree(tmp_path):
    from forgerepo import g
    from qqgarden.errors import GardenerError
    import pytest
    remote = tmp_path / "ledger.git"
    g(tmp_path, "init", "-q", "--bare", "-b", "ledger", str(remote))
    g(tmp_path, "clone", "-q", str(remote), "seed")
    Ledger(tmp_path / "seed", publish="ledger").reserve(
        Entry(id="s", repo="innernet", culprit="0" * 40, kind="test", action="propose",
              created_at="2026-10-04T11:00:00Z"))
    g(tmp_path, "clone", "-q", "-b", "ledger", str(remote), "a")
    g(tmp_path, "clone", "-q", "-b", "ledger", str(remote), "b")
    a, b = Ledger(tmp_path / "a", publish="ledger"), Ledger(tmp_path / "b", publish="ledger")
    a.reserve(Entry(id="r", repo="xo-space", culprit="a" * 40, kind="build", action="propose",
                    created_at="2026-10-04T11:30:00Z"))
    with pytest.raises(GardenerError, match="could not rebase"):
        b.reserve(Entry(id="r", repo="xo-space", culprit="a" * 40, kind="build", action="land",
                        created_at="2026-10-04T11:31:00Z"))
    assert not (tmp_path / "b" / ".git" / "rebase-merge").exists()
    assert g(tmp_path / "b", "status", "--porcelain") == ""
