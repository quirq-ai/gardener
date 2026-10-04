"""The revert ledger: every revert the gardener created, write-once, so caps hold across runs.

    <root>/reverts/<id>.json   written before the revert PR is opened (a reservation)
    <root>/landed/<id>.json    written when the gardener queues it to land

A reservation with action "land" counts against the submit limit as soon as it exists.

Reserving first means a run that dies after opening a PR has still counted it: the ledger can
over-count, never under-count. On GitHub the ledger lives on this repo's `ledger` branch.
TODO(expert): a real database once Launchpad exists.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

from qqgarden import git
from qqgarden.errors import GardenerError
from qqgarden.policy import Counts

SCHEMA = "qq-revert/1"
# Runners have no git identity; a rebase rewrites our commit, so it needs one as much as commit does.
IDENTITY = ["-c", "user.name=github-actions[bot]",
            "-c", "user.email=41898282+github-actions[bot]@users.noreply.github.com"]


def revert_id(repo: str, culprit: str) -> str:
    return f"{repo}-{culprit[:12]}"


@dataclass(frozen=True)
class Entry:
    id: str
    repo: str
    culprit: str
    kind: str                 # build or test
    action: str               # propose or land
    created_at: str           # RFC 3339 UTC
    reason: str = ""
    group: str = ""           # the failure group key
    revert: str = ""          # the revert PR or commit, once known
    runs: list[str] = field(default_factory=list)
    last_good: str = ""
    title: str = ""           # the culprit's title
    culprit_landed_at: str = ""
    tests: list[str] = field(default_factory=list)
    schema: str = SCHEMA


class Ledger:
    """With `publish`, root is a git worktree and every record is committed and pushed to `publish`
    (a branch on origin) before the call returns, so a reservation is durable before its PR exists.
    A record that cannot be pushed raises, and the cycle stops before creating anything."""

    def __init__(self, root: Path, publish: str = ""):
        self.root = Path(root)
        self.publish = publish

    def _write_once(self, path: Path, data: dict, recheck=None) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        text = json.dumps(data, sort_keys=True, indent=2) + "\n"
        try:
            with open(path, "x") as f:
                f.write(text)
        except FileExistsError:
            raise GardenerError(f"{path} already exists; ledger records are write-once") from None
        if self.publish:
            self._push(path, recheck)

    def _push(self, path: Path, recheck=None) -> None:
        """`recheck` runs after pulling in another writer's records; a non-empty answer drops this
        commit and raises, so the cap is judged on every record, not just ours."""
        rel = str(path.relative_to(self.root))
        git.run(["add", rel], cwd=self.root)
        self._commit_push(f"ledger: {rel}", recheck)

    def _commit_push(self, message: str, recheck=None) -> None:
        git.run([*IDENTITY, "commit", "--quiet", "-m", message], cwd=self.root)
        for _ in range(3):   # another writer may have pushed; records never collide, so rebase is safe
            if git.run(["push", "--quiet", "origin", f"HEAD:{self.publish}"], cwd=self.root,
                       check=False).returncode == 0:
                return
            pulled = git.run([*IDENTITY, "pull", "--quiet", "--rebase", "origin", self.publish],
                             cwd=self.root, check=False)
            if pulled.returncode != 0:   # e.g. the same record written twice: leave the worktree clean
                git.run(["rebase", "--abort"], cwd=self.root, check=False)
                git.run(["reset", "--quiet", "--hard", "HEAD~1"], cwd=self.root)
                raise GardenerError(f"could not rebase \"{message}\" onto the {self.publish} branch: "
                                    f"{pulled.stderr.strip()}; nothing was created")
            why = recheck() if recheck else ""
            if why:
                git.run(["reset", "--quiet", "--hard", "HEAD~1"], cwd=self.root)
                raise GardenerError(f"another writer changed the ledger first; now refused: {why}")
        raise GardenerError(f"could not push \"{message}\" to the {self.publish} branch; nothing was created")

    def refresh(self) -> None:
        """Pull in every other writer's records before deciding anything."""
        if self.publish:
            git.run([*IDENTITY, "pull", "--quiet", "--rebase", "origin", self.publish], cwd=self.root)

    def check_published(self) -> None:
        """The worktree is exactly the published branch: no local commits or edits (a deleted
        reservation, say) survive a pull to be counted, or pushed with the next record."""
        if git.run(["status", "--porcelain"], cwd=self.root).stdout.strip():
            raise GardenerError(f"{self.root} has uncommitted changes; the caps are counted on the "
                                f"published {self.publish} branch only")
        git.run(["fetch", "--quiet", "origin", self.publish], cwd=self.root)
        head = git.run(["rev-parse", "HEAD"], cwd=self.root).stdout.strip()
        published = git.run(["rev-parse", "FETCH_HEAD"], cwd=self.root).stdout.strip()
        if head != published:
            raise GardenerError(f"{self.root} is not the published {self.publish} branch (HEAD "
                                f"{head[:12]}, {self.publish} {published[:12]}); reset it to the branch")

    def reserve(self, e: Entry, recheck=None) -> None:
        self._write_once(self.root / "reverts" / f"{e.id}.json", asdict(e), recheck)

    def mark_landed(self, rid: str, at: str) -> None:
        self._write_once(self.root / "landed" / f"{rid}.json", {"id": rid, "landed_at": at})

    def set_revert(self, rid: str, revert: str) -> None:
        """Link the PR once it exists. A separate record, so `reverts/` stays write-once."""
        self._write_once(self.root / "links" / f"{rid}.json", {"id": rid, "revert": revert})

    def sync(self, rels: list[str], message: str) -> None:
        """Commit and push whatever changed under `rels` (records written by other code)."""
        if not self.publish:
            return
        rels = [r for r in rels if (self.root / r).exists()]
        if not rels:
            return
        git.run(["add", "-A", "--", *rels], cwd=self.root)
        if git.run(["diff", "--cached", "--quiet"], cwd=self.root, check=False).returncode == 0:
            return
        self._commit_push(message)

    def revert_links(self) -> dict[str, str]:
        d = self.root / "links"
        out = {}
        for p in sorted(d.glob("*.json")) if d.is_dir() else []:
            data = json.loads(p.read_text())
            out[data["id"]] = data["revert"]
        return out

    def has(self, rid: str) -> bool:
        return (self.root / "reverts" / f"{rid}.json").is_file()

    def entry(self, rid: str) -> Entry | None:
        p = self.root / "reverts" / f"{rid}.json"
        if not p.is_file():
            return None
        data = json.loads(p.read_text())
        return Entry(**{k: v for k, v in data.items() if k in Entry.__dataclass_fields__})

    def entries(self) -> list[Entry]:
        d = self.root / "reverts"
        out = []
        for p in sorted(d.glob("*.json")) if d.is_dir() else []:
            data = json.loads(p.read_text())
            out.append(Entry(**{k: v for k, v in data.items() if k in Entry.__dataclass_fields__}))
        return out

    def landed(self) -> dict[str, str]:
        d = self.root / "landed"
        return {json.loads(p.read_text())["id"]: json.loads(p.read_text())["landed_at"]
                for p in (sorted(d.glob("*.json")) if d.is_dir() else [])}

    def counts(self, since: datetime, exclude: str = "") -> Counts:
        """A reservation that decided to land counts as landed from the moment it is written, so
        two writers racing for the last submit slot see each other, and a run that dies between
        queueing the land and recording it never under-counts."""
        from qqgarden.postsubmit import parse_time
        every = self.entries()
        entries = [e for e in every if parse_time(e.created_at) >= since and e.id != exclude]
        kinds: dict[str, int] = {}
        for e in entries:
            kinds[e.kind] = kinds.get(e.kind, 0) + 1
        by_id = {e.id: e for e in every}
        landing = {e.id for e in entries if e.action == "land"}
        landing |= {rid for rid, at in self.landed().items()
                    if rid in by_id and rid != exclude and parse_time(at) >= since}
        landed: dict[str, int] = {}
        for rid in landing:
            k = by_id[rid].kind
            landed[k] = landed.get(k, 0) + 1
        return Counts(created=len(entries), created_by_kind=kinds, landed_by_kind=landed)
