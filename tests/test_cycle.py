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
    assert pr["title"] == f"Revert {culprit['sha'][:12]} (qq gardener)" and culprit["sha"] in pr["body"]
    assert "`commit 5`" in pr["body"]
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


def shared_ledger(tmp_path):
    """A worktree of a `ledger` branch on a local bare remote, as the workflow checks it out."""
    from forgerepo import g
    remote = tmp_path / "ledger.git"
    g(tmp_path, "init", "-q", "--bare", "-b", "ledger", str(remote))
    g(tmp_path, "clone", "-q", str(remote), "led")
    g(tmp_path / "led", "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-q",
      "--allow-empty", "-m", "ledger")
    g(tmp_path / "led", "push", "-q", "origin", "HEAD:ledger")
    return tmp_path / "led"


def refused(args, capsys, why):
    rc = cli.main(args)
    return rc == 2 and why in capsys.readouterr().err


def revert_args(config_root, snap, led, culprit, *extra):
    return ["revert", "--config", str(config_root), "--backend", "snapshot", "--snapshot", str(snap),
            "--repo", "xo-space", "--ledger", str(led), "--now", NOW.isoformat(), "--culprit", culprit[:12],
            *extra]


def test_cli_revert_needs_a_verified_bisect_of_this_culprit(config_root, tmp_path, capsys):
    xo = build(tmp_path)
    culprit = xo["commits"][1]["sha"]
    snap = snapshot(tmp_path, **{"xo-space": xo})
    led = shared_ledger(tmp_path)
    args = revert_args(config_root, snap, led, culprit, "--publish-ledger", "ledger")
    assert refused(args, capsys, "verified culprit")
    bj = tmp_path / "bisect.json"
    bj.write_text(json.dumps({"culprit": xo["commits"][2]["sha"], "verified": True}))   # another commit
    assert refused(args + ["--bisect-json", str(bj)], capsys, "verified culprit")
    bj.write_text(json.dumps({"culprit": culprit, "verified": False}))
    assert refused(args + ["--bisect-json", str(bj)], capsys, "verified culprit")
    bj.write_text(json.dumps({"culprit": culprit, "verified": True}))
    assert cli.main(args + ["--bisect-json", str(bj), "--json"]) == 0
    [o] = json.loads(capsys.readouterr().out)
    assert o["step"] == "proposed" and o["kind"] == "build"      # the kind came from the red run
    from forgerepo import g
    assert f"reverts/xo-space-{culprit[:12]}.json" in g(tmp_path / "ledger.git", "ls-tree", "-r",
                                                          "--name-only", "ledger")


def test_cli_revert_refuses_a_private_ledger(config_root, tmp_path, capsys):
    xo = build(tmp_path)
    snap = snapshot(tmp_path, **{"xo-space": xo})
    for extra in ([], ["--publish-ledger", "mine"]):
        args = revert_args(config_root, snap, tmp_path / "ledger", xo["commits"][1]["sha"], *extra)
        assert refused(args, capsys, "shared ledger")


def test_cli_revert_refuses_a_commit_outside_a_red_range(config_root, tmp_path, capsys):
    xo = build(tmp_path)
    snap = snapshot(tmp_path, **{"xo-space": xo})
    green = xo["commits"][3]["sha"]                  # commit 3, before the break
    bj = tmp_path / "bisect.json"
    bj.write_text(json.dumps({"culprit": green, "verified": True}))
    args = revert_args(config_root, snap, shared_ledger(tmp_path), green,
                       "--publish-ledger", "ledger", "--bisect-json", str(bj))
    assert refused(args, capsys, "not a suspect")
    assert not (tmp_path / "forge").exists()


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


def test_two_writers_cannot_both_take_the_last_submit_slot(cfg, tmp_path):
    """A reservation that decided to land counts as landed at once, so the second writer re-decides
    after pulling it in and backs out instead of landing past submit_daily_limit."""
    import pytest
    from forgerepo import g
    from qqgarden.errors import GardenerError
    from qqgarden.policy import Action, decide
    p = Policy.from_config(cfg)
    p = type(p)(**{**p.__dict__, "auto_land_repos": frozenset({"xo-space"})})
    limit = p.types["build"].submit_daily_limit
    assert limit > 0
    remote = tmp_path / "ledger.git"
    g(tmp_path, "init", "-q", "--bare", "-b", "ledger", str(remote))
    g(tmp_path, "clone", "-q", str(remote), "seed")
    seed = Ledger(tmp_path / "seed", publish="ledger")
    for i in range(limit - 1):
        seed.reserve(Entry(id=f"s{i}", repo="xo-space", culprit=f"{i:040x}", kind="build", action="land",
                           created_at="2026-10-04T11:00:00Z"))
    g(tmp_path, "clone", "-q", "-b", "ledger", str(remote), "a")
    g(tmp_path, "clone", "-q", "-b", "ledger", str(remote), "b")
    a, b = Ledger(tmp_path / "a", publish="ledger"), Ledger(tmp_path / "b", publish="ledger")
    since = NOW - p.window
    young = NOW - timedelta(minutes=20)

    def judge(ledger, rid):
        return decide(p, "xo-space", "build", young, True, False, ledger.counts(since, exclude=rid), NOW)
    assert judge(a, "ra").action is Action.LAND and judge(b, "rb").action is Action.LAND

    def recheck(ledger, rid):
        return lambda: "" if judge(ledger, rid).action is Action.LAND else "now propose"
    a.reserve(Entry(id="ra", repo="xo-space", culprit="a" * 40, kind="build", action="land",
                    created_at="2026-10-04T11:30:00Z"), recheck=recheck(a, "ra"))
    with pytest.raises(GardenerError, match="another writer"):
        b.reserve(Entry(id="rb", repo="xo-space", culprit="b" * 40, kind="build", action="land",
                        created_at="2026-10-04T11:30:00Z"), recheck=recheck(b, "rb"))
    assert b.counts(since).landed_by_kind == {"build": limit}


def landed_revert(tmp_path, xo, conclusion):
    """The cycle's revert is on main; its own post-submit reported `conclusion` (None: not yet)."""
    from forgerepo import g
    sha = g(tmp_path / "xo-space.git", "rev-parse", "main")
    when = (NOW - timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    xo["commits"].insert(0, {"sha": sha, "landed_at": when, "title": f"Revert {xo['commits'][1]['sha'][:12]} (qq gardener)"})
    if conclusion:
        url = "https://example.invalid/xo-space/runs/200"
        xo["runs"].append({"builder": "xo-space-postsubmit", "commit": sha, "status": "completed",
                           "conclusion": conclusion, "id": "200", "attempt": 1, "url": url})
        xo["failed_steps"][url] = ["build (demo)"]
    return sha


def test_still_red_after_the_revert_landed_goes_to_a_person(cfg, tmp_path):
    cfg["auto_revert"]["policy"]["auto_land_repos"] = ["xo-space"]
    xo = build(tmp_path)
    [o] = run_cycle(cfg, snapshot(tmp_path, **{"xo-space": xo}), tmp_path)
    assert o.step == "reverted"
    landed_revert(tmp_path, xo, None)                  # its post-submit has not reported yet
    [o] = run_cycle(cfg, snapshot(tmp_path, **{"xo-space": xo}), tmp_path)
    assert o.step == "refused" and "already has a revert" in o.reason
    xo["commits"].pop(0)
    landed_revert(tmp_path, xo, "failure")             # red on the revert too: a second break
    [o] = run_cycle(cfg, snapshot(tmp_path, **{"xo-space": xo}), tmp_path)
    assert o.step == "needs-person" and "still red after its revert" in o.reason


def test_a_gardener_revert_is_never_reverted(cfg, tmp_path):
    cfg["auto_revert"]["policy"]["auto_land_repos"] = ["xo-space"]
    xo = build(tmp_path)
    xo["commits"][1]["title"] = "Revert 0123456789ab (qq gardener)"
    [o] = run_cycle(cfg, snapshot(tmp_path, **{"xo-space": xo}), tmp_path)
    assert o.step == "needs-person" and "own revert" in o.reason
    assert not (tmp_path / "forge").exists()


def test_titles_never_reach_markdown_raw():
    assert cycle.inline("fixes #1 `x`\n@someone") == "`fixes #1 'x' @someone`"
    cut = cycle.inline("a" * 100, 20)
    assert len(cut) == 20 and cut.startswith("`") and cut.endswith("...`")
    assert cycle.revert_title("ab" * 20) == "Revert abababababab (qq gardener)"
    assert cycle.GARDENER_REVERT.match(cycle.revert_title("ab" * 20))


def test_a_parent_red_on_its_rerun_is_not_a_verified_culprit(cfg, tmp_path):
    """Audit B2: with and without the suspect. A parent that goes red when re-run means the
    environment broke, not the culprit: a person looks, nothing is reverted."""
    cfg["auto_revert"]["policy"]["auto_land_repos"] = ["xo-space"]
    xo = build(tmp_path)
    parent = xo["commits"][2]["sha"]
    [rerun] = [r for r in xo["runs"] if r["commit"] == parent and r["attempt"] == 2]
    rerun["conclusion"] = "failure"
    xo["runs"].append({**rerun, "attempt": 3, "conclusion": "success"})
    [o] = run_cycle(cfg, snapshot(tmp_path, **{"xo-space": xo}), tmp_path)
    assert o.step == "needs-person" and "was red on one attempt" in o.reason
    assert not (tmp_path / "forge" / "prs").exists()


def test_verification_reruns_the_parent_too(cfg, tmp_path):
    cfg["auto_revert"]["policy"]["auto_land_repos"] = ["xo-space"]
    xo = build(tmp_path)
    parent = xo["commits"][2]["sha"]
    xo["runs"] = [r for r in xo["runs"] if not (r["commit"] == parent and r["attempt"] == 2)]
    [o] = run_cycle(cfg, snapshot(tmp_path, **{"xo-space": xo}), tmp_path)
    assert o.step == "awaiting-verification"
    [url] = {r["url"] for r in xo["runs"] if r["commit"] == parent}
    assert o.reason == f"re-running {url}"


def test_a_fetch_failure_is_infra_and_never_reverted(cfg, tmp_path):
    cfg["auto_revert"]["policy"]["auto_land_repos"] = ["xo-space"]
    xo = build(tmp_path)
    for url in xo["failed_steps"]:
        xo["failed_steps"][url] = ["fetch (demo)"]
    [o] = run_cycle(cfg, snapshot(tmp_path, **{"xo-space": xo}), tmp_path)
    assert o.step == "refused" and o.kind == "infra"
    assert not (tmp_path / "forge" / "prs").exists()
