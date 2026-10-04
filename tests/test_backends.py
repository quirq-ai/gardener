import subprocess

import pytest

from qqgarden import backends, git
from qqgarden.backends import github
from qqgarden.config import Repo
from qqgarden.errors import GardenerError

REPO = Repo(name="demo", source="github.com/quirq-ai/demo", default_branch="main",
            postsubmit=("demo-postsubmit",), timeout_minutes=30)


def test_unknown_backend():
    with pytest.raises(GardenerError, match="no gardener backend 'nope'"):
        backends.load("nope")


def test_github_runs_parse(monkeypatch):
    b = github.Backend(token="")
    seen = []

    def fake_get(path):
        seen.append(path)
        own = {"event": "push", "path": ".github/workflows/qq-demo-postsubmit.yml",
               "head_repository": {"full_name": "quirq-ai/demo"}}
        return {"workflow_runs": [
            {**own, "id": 7, "head_sha": "a" * 40, "status": "completed", "conclusion": "failure",
             "run_attempt": 2, "html_url": "https://github.com/x/runs/7", "updated_at": "2026-10-04T10:00:00Z"},
            {**own, "id": 8, "head_sha": "b" * 40, "status": "in_progress", "conclusion": None,
             "html_url": "https://github.com/x/runs/8", "updated_at": "2026-10-04T10:01:00Z"},
            # none of these is the builder: a fork, another event, another workflow file
            {**own, "id": 9, "head_sha": "c" * 40, "status": "completed", "conclusion": "failure",
             "head_repository": {"full_name": "someone/demo"}},
            {**own, "id": 10, "head_sha": "c" * 40, "status": "completed", "conclusion": "failure",
             "event": "pull_request"},
            {**own, "id": 11, "head_sha": "c" * 40, "status": "completed", "conclusion": "failure",
             "path": ".github/workflows/other.yml"},
        ]}
    monkeypatch.setattr(b, "_get", fake_get)
    runs, note = b.runs(REPO, "demo-postsubmit")
    assert note == ""
    assert "/repos/quirq-ai/demo/actions/workflows/qq-demo-postsubmit.yml/runs?" in seen[0]
    assert "event=push" in seen[0] and "branch=main" in seen[0]
    assert [(r.commit[0], r.state.value, r.attempt) for r in runs] == [("a", "red", 2), ("b", "pending", 1)]
    assert runs[1].finished_at == ""


def test_github_undelivered_workflow_is_a_note(monkeypatch):
    b = github.Backend(token="")
    monkeypatch.setattr(b, "_get", lambda path: None)
    runs, note = b.runs(REPO, "demo-postsubmit")
    assert runs == [] and "has not been delivered" in note


def test_first_parent_skips_merged_branch_commits(tmp_path):
    def g(*a):
        subprocess.run(["git", *a], cwd=tmp_path, check=True, capture_output=True)
    g("init", "-q", "-b", "main")
    g("config", "user.email", "t@example.invalid")
    g("config", "user.name", "t")
    g("commit", "-q", "--allow-empty", "-m", "one")
    g("checkout", "-q", "-b", "side")
    g("commit", "-q", "--allow-empty", "-m", "side work")
    g("checkout", "-q", "main")
    g("commit", "-q", "--allow-empty", "-m", "two")
    g("merge", "-q", "--no-ff", "side", "-m", "merge side")
    titles = [c.title for c in git.first_parent(tmp_path, "main", 10)]
    assert titles == ["merge side", "two", "one"]
