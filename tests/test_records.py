import json
from datetime import timedelta
from pathlib import Path

from forgerepo import build, remote_main_state, snapshot
from history import NOW
from qqresults import failures

from qqgarden import config, cycle
from qqgarden.backends import load
from qqgarden.ledger import Ledger
from qqgarden.policy import Policy
from qqgarden.records import Records, trigger_mode
from qqgarden.tracker import LocalTracker


def setup(cfg, config_root, tmp_path, **build_kw):
    xo = build(tmp_path, **build_kw)
    backend = load("snapshot", path=snapshot(tmp_path, **{"xo-space": xo}), forge_dir=tmp_path / "forge")
    ledger = Ledger(tmp_path / "ledger")
    records = Records(ledger.root / "failures", LocalTracker(tmp_path / "issues"), cfg, config_root)
    repos = [r for r in config.repos(cfg) if r.name == "xo-space"]

    def go(now=NOW):
        return cycle.run(cfg, repos, backend, ledger, Policy.from_config(cfg), now,
                         timedelta(minutes=15), 100, records=records)
    return xo, go, ledger


def record_dirs(ledger):
    return sorted((ledger.root / "failures").iterdir())


def test_every_auto_revert_links_culprit_revert_and_fix(cfg, config_root, tmp_path):
    """V0-GAR-04 done-when."""
    cfg["auto_revert"]["policy"]["auto_land_repos"] = ["xo-space"]
    xo, go, ledger = setup(cfg, config_root, tmp_path)
    culprit = xo["commits"][1]["sha"]
    outcomes = go()
    assert [o.step for o in outcomes] == ["reverted", "fix-linked"]
    assert remote_main_state(tmp_path) == "ok"
    [d] = record_dirs(ledger)
    state = failures.read(d)
    f = state.current
    assert f.kind == "auto-revert" and f.repo == "quirq-ai/xo-space" and f.subject == culprit
    assert f.culprit == f"local://xo-space/commit/{culprit}"
    assert f.operation == "local://xo-space/pull/1"          # the revert
    assert f.fix.startswith("local://xo-space/commit/")       # the revert's commit, now on main
    assert f.postmortem == f"local://postmortems/{f.id}"
    assert state.missing == ["covering_test"]                 # the owner adds it; the stub asks
    stub = json.loads((tmp_path / "issues" / "postmortems" / f"{f.id}.json").read_text())
    assert stub["labels"] == [cfg["postmortem"]["policy"]["label"]]
    assert "**Trigger:** auto-revert" in stub["body"] and culprit in stub["body"]
    assert "local://xo-space/pull/1" in stub["body"] and "<!--" not in stub["body"].split("\n", 1)[1]
    mirror = json.loads((tmp_path / "issues" / "failures" / f"{f.id}.json").read_text())
    assert mirror["record"]["operation"] == "local://xo-space/pull/1"


def test_proposed_revert_gets_its_fix_when_someone_lands_it(cfg, config_root, tmp_path):
    xo, go, ledger = setup(cfg, config_root, tmp_path)
    assert [o.step for o in go()] == ["proposed", "recorded"]
    [d] = record_dirs(ledger)
    assert not failures.read(d).current.fix
    # A person merges the proposed revert; the next cycle links it as the fix, once.
    backend = load("snapshot", path=tmp_path / "snap.json", forge_dir=tmp_path / "forge")
    [repo] = [r for r in config.repos(cfg) if r.name == "xo-space"]
    backend.forge.queue_land(repo, "local://xo-space/pull/1")
    steps = [o.step for o in go(NOW + timedelta(minutes=5))]
    assert "fix-linked" in steps
    assert failures.read(d).current.fix
    links = list((d / "links").glob("*-fix-*.json"))
    go(NOW + timedelta(minutes=10))
    assert list((d / "links").glob("*-fix-*.json")) == links


def test_one_record_and_one_stub_per_culprit(cfg, config_root, tmp_path):
    xo, go, ledger = setup(cfg, config_root, tmp_path)
    go()
    go(NOW + timedelta(minutes=5))
    assert len(record_dirs(ledger)) == 1
    assert len(list((tmp_path / "issues" / "postmortems").iterdir())) == 1


def test_trigger_mode_comes_from_config(cfg):
    assert trigger_mode(cfg) == "stub"
    for t in cfg["postmortem"]["trigger"]:
        if t["event"] == "auto-revert":
            t["postmortem"] = "track"
    assert trigger_mode(cfg) == "track"


def test_track_only_opens_no_stub(cfg, config_root, tmp_path):
    for t in cfg["postmortem"]["trigger"]:
        if t["event"] == "auto-revert":
            t["postmortem"] = "track"
    xo, go, ledger = setup(cfg, config_root, tmp_path)
    go()
    assert not (tmp_path / "issues" / "postmortems").exists()
    assert failures.read(record_dirs(ledger)[0]).current.culprit


class CountingTracker(LocalTracker):
    def __init__(self, root, fail=False):
        super().__init__(root)
        self.calls, self.fail = 0, fail

    def mirror(self, state):
        self.calls += 1
        if self.fail:
            raise RuntimeError("HTTP 403 rate limited")
        return super().mirror(state)


def test_a_finished_record_costs_no_more_calls(cfg, config_root, tmp_path):
    cfg["auto_revert"]["policy"]["auto_land_repos"] = ["xo-space"]
    xo, go, ledger = setup(cfg, config_root, tmp_path)
    t = CountingTracker(tmp_path / "issues")
    records = Records(ledger.root / "failures", t, cfg, config_root)
    repos = [r for r in config.repos(cfg) if r.name == "xo-space"]
    backend = load("snapshot", path=tmp_path / "snap.json", forge_dir=tmp_path / "forge")

    def run(now):
        return cycle.run(cfg, repos, backend, ledger, Policy.from_config(cfg), now,
                         timedelta(minutes=15), 100, records=records)
    assert [o.step for o in run(NOW)] == ["reverted", "fix-linked"]
    calls = t.calls
    for i in range(1, 4):
        run(NOW + timedelta(minutes=5 * i))
    assert t.calls == calls


def test_a_failing_record_never_stops_the_cycle(cfg, config_root, tmp_path):
    xo, go, ledger = setup(cfg, config_root, tmp_path)
    records = Records(ledger.root / "failures", CountingTracker(tmp_path / "issues", fail=True), cfg,
                      config_root)
    repos = [r for r in config.repos(cfg) if r.name == "xo-space"]
    backend = load("snapshot", path=tmp_path / "snap.json", forge_dir=tmp_path / "forge")
    out = cycle.run(cfg, repos, backend, ledger, Policy.from_config(cfg), NOW,
                    timedelta(minutes=15), 100, records=records)
    assert [o.step for o in out] == ["proposed", "record-failed"]
    assert "rate limited" in out[1].reason


def test_commit_titles_cannot_ping_or_link_from_the_stub(cfg, config_root, tmp_path):
    xo, go, ledger = setup(cfg, config_root, tmp_path)
    go()
    evil = "fix @sharmasuraj0123 [click](https://evil.invalid) ![x](https://evil.invalid/p.png) `x`"
    assert cycle.inline(evil) == "`" + evil.replace("`", "'") + "`"
    stub = json.loads(next((tmp_path / "issues" / "postmortems").iterdir()).read_text())
    heading = next(line for line in stub["body"].splitlines() if line.startswith("# Postmortem:"))
    assert heading.endswith("reverted `commit 5`")


def test_a_reservation_without_a_revert_is_reported(cfg, config_root, tmp_path):
    from qqgarden.ledger import Entry
    xo, go, ledger = setup(cfg, config_root, tmp_path, brk=99)
    ledger.reserve(Entry(id="xo-space-" + "a" * 12, repo="xo-space", culprit="a" * 40, kind="build",
                         action="propose", created_at="2026-10-04T11:00:00Z"))
    [o] = go()
    assert o.step == "unlinked" and o.culprit == "a" * 40


def test_github_stub_search_pages_and_fails_closed(monkeypatch):
    import pytest
    from qqresults.backends import github as gh
    from qqgarden.errors import GardenerError
    from qqgarden.tracker import GitHubTracker
    t = GitHubTracker("quirq-ai/gardener", "tok")
    marker = "<!-- qq-postmortem: F1 -->"
    calls = []

    def api(method, url, token, body=None):
        calls.append((method, url))
        if method == "GET" and url.endswith("&page=1"):
            return 200, [{"body": "other", "html_url": "u"}] * 100
        if method == "GET":
            return 200, [{"body": marker + "\nstub", "html_url": "https://github.com/x/issues/7"}]
        raise AssertionError("must not create a second stub")
    monkeypatch.setattr(gh, "api", api)
    assert t.open_postmortem("F1", "t", "b", "postmortem") == "https://github.com/x/issues/7"
    monkeypatch.setattr(gh, "api", lambda *a, **k: (502, None))
    with pytest.raises(GardenerError, match="HTTP 502"):
        t.open_postmortem("F1", "t", "b", "postmortem")
