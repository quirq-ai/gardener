"""Trackers: where failure-record mirrors and postmortem stubs become issues people see.

    record_url(state) -> str                       # where the record itself can be read
    mirror(state) -> str                           # the record's issue URL ("" when withheld)
    open_postmortem(fid, title, body, label) -> str

v0 files both in the gardener's own repo (TODO(suraj): or in the affected repo, which needs the bot
identity's issues permission there). The issues are public, so security-looking records are never
mirrored and get no stub.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from qqgarden.errors import GardenerError


class GitHubTracker:
    def __init__(self, repo: str, token: str, ledger_branch: str = "ledger"):
        self.repo = repo
        self.token = token
        self.ledger_branch = ledger_branch

    @classmethod
    def from_env(cls, ledger_branch: str = "ledger") -> "GitHubTracker | None":
        repo, token = os.environ.get("GITHUB_REPOSITORY", ""), os.environ.get("GITHUB_TOKEN", "")
        return cls(repo, token, ledger_branch or "ledger") if repo and token else None

    def record_url(self, state) -> str:
        return (f"https://github.com/{self.repo}/tree/refs/heads/{self.ledger_branch}/failures/"
                f"{state.path.name}")

    def mirror(self, state) -> str:
        from qqresults.backends.github import mirror_issue
        url, _ = mirror_issue(state, self.repo, self.token)
        return url

    def open_postmortem(self, fid: str, title: str, body: str, label: str) -> str:
        from qqresults.backends.github import API, api
        marker = f"<!-- qq-postmortem: {fid} -->"
        # Find an earlier stub on every page; if the search fails, open nothing (a duplicate stub
        # is worse than one cycle without one).
        for page in range(1, 51):
            status, issues = api("GET", f"{API}/repos/{self.repo}/issues?labels={label}&state=all"
                                        f"&per_page=100&page={page}", self.token)
            if status != 200:
                raise GardenerError(f"{self.repo}: looking for an existing postmortem stub: HTTP {status}")
            for i in issues:
                if (i.get("body") or "").startswith(marker):
                    return i["html_url"]
            if len(issues) < 100:
                break
        else:
            raise GardenerError(f"{self.repo}: more than 5000 {label} issues; not opening another stub")
        api("POST", f"{API}/repos/{self.repo}/labels", self.token,
            {"name": label, "color": "5319e7", "description": "quirq infra postmortem (blameless)"})
        status, issue = api("POST", f"{API}/repos/{self.repo}/issues", self.token,
                            {"title": title, "body": body, "labels": [label]})
        if status != 201:
            raise GardenerError(f"{self.repo}: opening the postmortem stub: HTTP {status} "
                                "(the token needs issues: write)")
        return issue["html_url"]


class LocalTracker:
    """Issues as JSON files, for tests and the offline demos."""

    def __init__(self, root: Path):
        self.root = Path(root)

    def record_url(self, state) -> str:
        return f"file://{state.path}"

    def _issue(self, kind: str, key: str, data: dict) -> str:
        d = self.root / kind
        d.mkdir(parents=True, exist_ok=True)
        p = d / f"{key}.json"
        p.write_text(json.dumps(data, indent=2, sort_keys=True))
        return f"local://{kind}/{key}"

    def mirror(self, state) -> str:
        if state.security:
            return ""
        f = state.current
        return self._issue("failures", f.id, {"record": f.to_dict(), "closed": state.closed})

    def open_postmortem(self, fid: str, title: str, body: str, label: str) -> str:
        p = self.root / "postmortems" / f"{fid}.json"
        if p.is_file():
            return f"local://postmortems/{fid}"
        return self._issue("postmortems", fid, {"title": title, "body": body, "labels": [label]})
