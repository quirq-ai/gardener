"""V0-GAR-04: a failure record and a postmortem stub for every revert the gardener creates.

The record is test-pipelines' `Failure` (kind `auto-revert`, plan §5.10), opened through qqresults
by pinned commit, so there is one record format across qq. Its id comes from repo and culprit, so
one culprit has one record however often the cycle sees it. It links:

- culprit: the culprit commit;
- operation: the revert PR;
- postmortem: the stub issue, opened from infra-config's template when postmortem.toml's
  `auto-revert` trigger asks for one (`stub` or `required`; `track` records only);
- fix: the commit that made main green again, linked by a later cycle once the revert lands
  (`follow_up`); a reland links its own fix by hand with `qqresults failure link`;
- issue: the record's labelled issue mirror.

The record closes only when culprit, fix and covering test are linked (postmortem.toml
`record_needs`); the covering test is the owner's to add, and the stub asks for it.
Records live in the ledger (`<ledger>/failures/`, the qqresults layout). A security-looking record
is never mirrored to a public issue and gets no public postmortem stub (test-pipelines' rule).
TODO(expert): a record that only looks security-related after a later link keeps its stub; the
mirror refuses and the cycle reports `record-failed` until a person deals with the issue.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from qqresults import failures

from qqgarden.config import Repo
from qqgarden.errors import GardenerError


def trigger_mode(cfg: dict, event: str = "auto-revert") -> str:
    for t in cfg.get("postmortem", {}).get("trigger", []):
        if t.get("event") == event:
            return t.get("postmortem", "")
    raise GardenerError(f"infra-config postmortem.toml has no {event!r} trigger; the gardener does "
                        "not guess whether a revert needs a postmortem")


def template(config_root: Path, cfg: dict) -> str:
    rel = cfg["postmortem"]["policy"]["template"]
    text = (Path(config_root) / rel).read_text()
    return re.sub(r"\A\s*<!--.*?-->\s*", "", text, flags=re.S)   # drop the authoring comment


def stub_body(tmpl: str, state: failures.State, revert_url: str, culprit_url: str, run_url: str,
              landed_at: str, record_url: str) -> str:
    f = state.current
    fill = {
        "# Postmortem: <one line naming what failed>": f"# Postmortem: {f.summary.splitlines()[0]}",
        "**Trigger:** <postmortem.toml event, e.g. canary-deploy-failed>": "**Trigger:** auto-revert",
        "**Repo or area:** <repo or config area>": f"**Repo or area:** {f.repo}",
        "**Status:** draft | in review | final": "**Status:** draft (stub opened by the gardener)",
        "**Failure record:** <link to the tracked record>": f"**Failure record:** `{f.id}` {record_url}",
        "| | first bad commit landed | <commit link> |": f"| {landed_at} | first bad commit landed | {culprit_url} |",
        "| | first red run | <run link> |": f"| | first red run | {run_url} |",
        "| | mitigated (revert, rollback or hold) | <PR or operation key> |":
            f"| | mitigated (revert) | {revert_url} |",
        "- [ ] Culprit: <commit>": f"- [x] Culprit: {culprit_url}",
        "- [ ] Fix: <PR>": "- [ ] Fix: linked by the gardener when the revert lands, or by the owner on a reland",
    }
    out = tmpl
    for old, new in fill.items():
        out = out.replace(old, new)
    return f"<!-- qq-postmortem: {f.id} -->\n{out}"


class Records:
    """Opens and follows records under `root` (the ledger's failures/), with a tracker for issues."""

    def __init__(self, root: Path, tracker, cfg: dict, config_root: Path, mirrors: Path | None = None):
        self.root = Path(root)
        self.tracker = tracker
        self.cfg = cfg
        self.config_root = Path(config_root)
        # What each record looked like when last mirrored, so an unchanged record costs no API call.
        self.mirrors = Path(mirrors) if mirrors else self.root.parent / "mirrors"

    def path(self, repo: Repo, culprit: str) -> Path:
        return self.root / failures.dirname(failures.failure_id("auto-revert", repo.slug or repo.name, culprit))

    def exists(self, repo: Repo, culprit: str) -> bool:
        return (self.path(repo, culprit) / failures.RECORD).is_file()

    def done(self, repo: Repo, culprit: str) -> bool:
        """The fix is linked and the mirror is current: nothing left for the cycle to do."""
        if not self.exists(repo, culprit):
            return False
        state = failures.read(self.path(repo, culprit))
        return bool(state.links.get("fix")) and self._mirrored(state) == _fingerprint(state)

    def _marker(self, state: failures.State) -> Path:
        return self.mirrors / f"{state.path.name}.json"

    def _mirrored(self, state: failures.State) -> str:
        p = self._marker(state)
        return json.loads(p.read_text()).get("fingerprint", "") if p.is_file() else ""

    def open(self, repo: Repo, culprit: str, culprit_url: str, landed_at: str, revert_url: str,
             group, summary: str) -> failures.State:
        f = failures.new("auto-revert", repo.slug or repo.name, culprit, first_bad=culprit,
                         last_good=group.last_good, run_id=(group.runs or [""])[0],
                         signal=", ".join(group.tests or group.builders), stage=group.kind,
                         summary=summary)
        state, _ = failures.open_record(f, self.root)
        links = state.links
        if "culprit" not in links:
            failures.add_link(state.path, "culprit", culprit_url)
        if "operation" not in links:
            failures.add_link(state.path, "operation", revert_url)
        state = failures.read(state.path)
        mode = trigger_mode(self.cfg)
        if mode in ("stub", "required") and "postmortem" not in state.links and not state.security:
            body = stub_body(template(self.config_root, self.cfg), state, revert_url, culprit_url,
                             (group.runs or [""])[0], landed_at, self.tracker.record_url(state))
            url = self.tracker.open_postmortem(state.current.id, f"Postmortem: {summary.splitlines()[0]}",
                                               body, self.cfg["postmortem"]["policy"]["label"])
            failures.add_link(state.path, "postmortem", url)
        return self.mirror(failures.read(state.path))

    def mirror(self, state: failures.State) -> failures.State:
        if self._mirrored(state) == _fingerprint(state):
            return state
        url = self.tracker.mirror(state)
        if url and state.links.get("issue") != url:
            failures.add_link(state.path, "issue", url)
        state = failures.read(state.path)
        self.mirrors.mkdir(parents=True, exist_ok=True)
        self._marker(state).write_text(json.dumps({"id": state.current.id,
                                                   "fingerprint": _fingerprint(state)}) + "\n")
        return state

    def link_fix(self, repo: Repo, culprit: str, fix_url: str) -> failures.State | None:
        p = self.path(repo, culprit)
        if not (p / failures.RECORD).is_file():
            return None
        state = failures.read(p)
        if state.links.get("fix"):
            return state
        failures.add_link(p, "fix", fix_url)
        return self.mirror(failures.read(p))


def _fingerprint(state: failures.State) -> str:
    """Everything the issue mirror shows: the record, its links, open or closed."""
    doc = {"record": state.current.to_dict(), "links": state.links, "closed": state.closed,
           "security": state.security}
    return hashlib.sha256(json.dumps(doc, sort_keys=True).encode()).hexdigest()
