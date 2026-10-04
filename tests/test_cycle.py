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


def test_red_once_waits_for_verification(cfg, tmp_path):
    xo = build(tmp_path, n=5, brk=5)
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
