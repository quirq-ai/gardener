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
        xo["runs"].append({"builder": "xo-space-postsubmit", "commit": sha, "status": "completed",
                           "conclusion": "success", "id": str(900 + i), "backfill": True})
    assert run_cycle(cfg, xo, tmp_path) == []
    assert len(dispatched(tmp_path)) == 3


def test_a_backfill_cancelled_again_goes_to_a_person(cfg, tmp_path):
    xo, shas = batched(tmp_path)
    xo["runs"].append({"builder": "xo-space-postsubmit", "commit": shas[4], "status": "completed",
                       "conclusion": "cancelled", "id": "950", "backfill": True})
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
                     {"ref": "main", "inputs": {"commit": "c" * 40}})]
