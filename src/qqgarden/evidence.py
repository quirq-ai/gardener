"""Evidence for grouping: failed step names from the backend, unexpected tests from the results
store (test-pipelines' `results` branch, checked out as a directory)."""
from __future__ import annotations

from pathlib import Path

from qqgarden.config import Repo


class Evidence:
    def __init__(self, backend, repos: dict[str, Repo], store: Path | None = None):
        self.backend = backend
        self.repos = repos
        self.store = None
        if store is not None:
            from qqresults.store import open_store
            self.store = open_store(store)

    def failed_steps(self, repo: str, run_url: str) -> list[str]:
        return self.backend.failed_steps(self.repos[repo], run_url) if run_url else []

    def unexpected_tests(self, repo: str, builder: str, commit: str) -> list[str] | None:
        """None when the store has no post-submit run of this builder on this commit."""
        if self.store is None or not self.repos[repo].slug:
            return None
        from qqresults.store import RunFilter
        runs = [(r, v) for r, v in self.store.runs(RunFilter(repo=self.repos[repo].slug,
                                                              kind="postsubmit", commit=commit))
                if r.job == builder]
        if not runs:
            return None
        # One job can store several bundles (one per sink); keep each sink's newest attempt.
        newest: dict[str, tuple] = {}
        for r, v in runs:
            sink = r.id.rsplit(f"/{r.job}", 1)[-1]      # "" or "/<name>"
            if sink not in newest or r.attempt >= newest[sink][0].attempt:
                newest[sink] = (r, v)
        return sorted({t.test_id for _, v in newest.values() for t in v.tests if t.status == "UNEXPECTED"})
