"""V0-GAR-01 backfill: a batched push runs only its newest commit; the cycle dispatches the rest."""
import json
from datetime import timedelta

from forgerepo import build, snapshot
from history import NOW

from qqgarden import config, cycle
from qqgarden.backends import github, load
from qqgarden.config import Repo
from qqgarden.ledger import Ledger
from qqgarden.policy import Policy


def run_cycle(cfg, xo, tmp_path, dry_run=False):
    backend = load("snapshot", path=snapshot(tmp_path, **{"xo-space": xo}), forge_dir=tmp_path / "forge")
    repos = [r for r in config.repos(cfg) if r.name == "xo-space"]
    return cycle.run(cfg, repos, backend, Ledger(tmp_path / "ledger"), Policy.from_config(cfg), NOW,
                     timedelta(minutes=15), 100, dry_run=dry_run)


def dispatched(tmp_path):
    p = tmp_path / "forge" / "dispatches" / "xo-space.jsonl"
    return [json.loads(line) for line in p.read_text().splitlines()] if p.exists() else []


def backfilled(xo, sha, conclusion, run_id, head_sha=None, status="completed"):
    """A dispatched run for `sha`, run from main's tip unless `head_sha` says otherwise."""
    return {"builder": "xo-space-postsubmit", "commit": sha, "status": status, "conclusion": conclusion,
            "id": str(run_id), "backfill": True, "head_sha": head_sha or xo["commits"][0]["sha"],
            "url": f"https://example.invalid/runs/{run_id}"}


def batched(tmp_path):
    """Six green commits; 2 and 3 came in one push with 4 (no runs), 5's run was cancelled."""
    xo = build(tmp_path, brk=99)
    shas = [c["sha"] for c in reversed(xo["commits"])]          # oldest first
    xo["runs"] = [r for r in xo["runs"] if r["commit"] not in (shas[1], shas[2])]
    for r in xo["runs"]:
        if r["commit"] == shas[4]:
            r["conclusion"] = "cancelled"
    return xo, shas


def test_holes_are_backfilled_oldest_first_and_then_covered(cfg, tmp_path):
    xo, shas = batched(tmp_path)
    [o] = run_cycle(cfg, xo, tmp_path)
    assert o.step == "backfilled" and o.group == "coverage"
    assert [d["commit"] for d in dispatched(tmp_path)] == [shas[1], shas[2], shas[4]]
    assert {d["builder"] for d in dispatched(tmp_path)} == {"xo-space-postsubmit"}
    # The dispatched runs report back; nothing is left to backfill.
    for i, sha in enumerate((shas[1], shas[2], shas[4])):
        xo["runs"].append(backfilled(xo, sha, "success", 900 + i))
    assert run_cycle(cfg, xo, tmp_path) == []
    assert len(dispatched(tmp_path)) == 3


def test_a_backfill_cancelled_again_goes_to_a_person(cfg, tmp_path):
    xo, shas = batched(tmp_path)
    xo["runs"].append(backfilled(xo, shas[4], "cancelled", 950))
    out = run_cycle(cfg, xo, tmp_path)
    assert [o.step for o in out] == ["needs-person", "backfilled"]
    assert shas[4] in out[0].reason
    assert [d["commit"] for d in dispatched(tmp_path)] == [shas[1], shas[2]]


def test_dry_run_dispatches_nothing(cfg, tmp_path):
    xo, _ = batched(tmp_path)
    [o] = run_cycle(cfg, xo, tmp_path, dry_run=True)
    assert o.step == "dry-run" and "would backfill 3" in o.reason
    assert dispatched(tmp_path) == []


def test_backfill_is_capped_per_cycle(cfg, tmp_path):
    xo = build(tmp_path, n=14, brk=99)
    ends = (xo["commits"][0]["sha"], xo["commits"][-1]["sha"])
    xo["runs"] = [r for r in xo["runs"] if r["commit"] in ends]     # one push of 13 commits
    [o] = run_cycle(cfg, xo, tmp_path)
    assert o.step == "backfilled" and len(dispatched(tmp_path)) == cycle.BACKFILL_PER_CYCLE
    assert dispatched(tmp_path)[0]["commit"] == xo["commits"][-2]["sha"]    # oldest hole first


REPO = Repo(name="demo", source="github.com/quirq-ai/demo", default_branch="main",
            postsubmit=("demo-postsubmit",), timeout_minutes=30)


def test_github_reads_dispatched_runs_by_run_name(monkeypatch):
    b = github.Backend(token="")
    c, tip = "c" * 40, "f" * 40
    own = {"path": ".github/workflows/qq-demo-postsubmit.yml", "head_repository": {"full_name": "quirq-ai/demo"},
           "head_branch": "main", "head_sha": tip, "status": "completed", "conclusion": "success"}

    def fake_get(path):
        if "event=workflow_dispatch" not in path:
            return {"workflow_runs": []}
        return {"workflow_runs": [
            {**own, "event": "workflow_dispatch", "id": 1, "display_title": f"demo-postsubmit {c}"},
            # not the builder's: another branch, another builder's name, a short sha, no commit
            {**own, "event": "workflow_dispatch", "id": 2, "display_title": f"demo-postsubmit {c}",
             "head_branch": "evil"},
            {**own, "event": "workflow_dispatch", "id": 3, "display_title": f"other-postsubmit {c}"},
            {**own, "event": "workflow_dispatch", "id": 4, "display_title": "demo-postsubmit cccc"},
            {**own, "event": "workflow_dispatch", "id": 5, "display_title": "demo-postsubmit"},
        ]}
    monkeypatch.setattr(b, "_get", fake_get)
    runs, note = b.runs(REPO, "demo-postsubmit")
    assert note == ""
    assert [(r.id, r.commit, r.backfill) for r in runs] == [("1", c, True)]


def test_github_backfill_dispatches_on_the_default_branch(monkeypatch):
    b = github.Backend(token="", write_token="t")
    sent = []
    monkeypatch.setattr(b, "_send", lambda method, path, token, body=None: sent.append((method, path, body)))
    b.backfill(REPO, "demo-postsubmit", "c" * 40)
    assert sent == [("POST", "/repos/quirq-ai/demo/actions/workflows/qq-demo-postsubmit.yml/dispatches",
                     {"ref": "refs/heads/main", "inputs": {"commit": "c" * 40}})]


def test_a_dispatch_not_run_from_main_is_ignored(cfg, tmp_path):
    """A writer's tag named `main` with an edited workflow cannot forge a verdict on a main commit:
    a backfill counts only when it ran from main at or after its commit."""
    xo, shas = batched(tmp_path)
    xo["runs"].append(backfilled(xo, shas[1], "failure", 960, head_sha="e" * 40))      # not on main
    xo["runs"].append(backfilled(xo, shas[2], "failure", 961, head_sha=shas[0]))       # older than it
    snap = snapshot(tmp_path, **{"xo-space": xo})
    backend = load("snapshot", path=snap)
    [repo] = [r for r in config.repos(cfg) if r.name == "xo-space"]
    from qqgarden import postsubmit
    status, _ = postsubmit.observe(backend, repo, 100, NOW, timedelta(minutes=15))
    assert status.state == "open" and not status.red
    assert any("did not run from main" in n for n in status.notes)
    assert f"{shas[1]} xo-space-postsubmit" in status.coverage.missing


def test_rerunning_the_push_run_clears_a_cancelled_backfill(cfg, tmp_path):
    xo, shas = batched(tmp_path)
    xo["runs"].append(backfilled(xo, shas[4], "cancelled", 950))
    push = next(r for r in xo["runs"] if r["commit"] == shas[4] and not r.get("backfill"))
    xo["runs"].append({**push, "attempt": 2, "conclusion": "success"})
    out = run_cycle(cfg, xo, tmp_path)
    assert "needs-person" not in [o.step for o in out]
    assert shas[4] not in [d["commit"] for d in dispatched(tmp_path)]


def test_a_manual_dispatch_of_old_history_does_not_move_onboarding(cfg, tmp_path):
    xo = build(tmp_path, n=8, brk=99)
    shas = [c["sha"] for c in reversed(xo["commits"])]
    xo["runs"] = [r for r in xo["runs"] if r["commit"] in shas[4:]]      # onboarded at commit 5
    xo["runs"].append(backfilled(xo, shas[0], "success", 970))          # someone dispatched commit 1
    assert run_cycle(cfg, xo, tmp_path) == []
    assert dispatched(tmp_path) == []


def test_backfills_in_flight_count_against_the_cap(cfg, tmp_path):
    xo = build(tmp_path, n=14, brk=99)
    shas = [c["sha"] for c in reversed(xo["commits"])]
    xo["runs"] = [r for r in xo["runs"] if r["commit"] in (shas[0], shas[-1])]
    for i, sha in enumerate(shas[1:1 + cycle.BACKFILL_PER_CYCLE]):
        xo["runs"].append(backfilled(xo, sha, "", 980 + i, status="in_progress"))
    assert run_cycle(cfg, xo, tmp_path) == []
    assert dispatched(tmp_path) == []


def test_a_red_backfill_is_not_reverted_on_its_own(cfg, tmp_path):
    """A backfill runs main's newer workflow on an older commit; its red alone names no culprit."""
    xo = build(tmp_path)                        # break at commit 5, red re-run on it
    culprit = xo["commits"][1]["sha"]
    for r in xo["runs"]:
        if r["commit"] == culprit:
            r.update(backfill=True, head_sha=xo["commits"][0]["sha"])
    [o] = run_cycle(cfg, xo, tmp_path)
    assert o.step == "needs-bisect" and "backfill" in o.reason
    assert not (tmp_path / "forge" / "prs").exists()


def test_a_failing_dispatch_never_stops_reverts(cfg, tmp_path):
    from qqgarden.errors import GardenerError
    xo = build(tmp_path, n=7, brk=6)
    shas = [c["sha"] for c in reversed(xo["commits"])]
    xo["runs"] = [r for r in xo["runs"] if r["commit"] != shas[2]]       # a hole at commit 3
    backend = load("snapshot", path=snapshot(tmp_path, **{"xo-space": xo}), forge_dir=tmp_path / "forge")

    def broken(*a):
        raise GardenerError("HTTP 422: workflow has no workflow_dispatch trigger")
    backend.forge.backfill = broken
    repos = [r for r in config.repos(cfg) if r.name == "xo-space"]
    out = cycle.run(cfg, repos, backend, Ledger(tmp_path / "ledger"), Policy.from_config(cfg), NOW,
                    timedelta(minutes=15), 100)
    assert [o.step for o in out] == ["error", "proposed"]
