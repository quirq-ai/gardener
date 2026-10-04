"""GitHub backend. History comes from a public git clone; post-submit runs from the Actions API.

infra-config delivers each post-submit builder as `.github/workflows/qq-<builder>.yml`, triggered by
`push` to the default branch, with one job named after the builder. So the builder's runs on a
commit are that workflow's `push` runs whose head is the commit. A batched push runs only its
newest commit; the gardener backfills the others by dispatching the workflow with a `commit`
input. A dispatched run's head is the branch tip, so it is matched by its run-name,
"<builder> <commit>", and only when it ran on the default branch.

Reads need no token for public repos; GITHUB_TOKEN, when set, only raises the rate limit.

Writes (revert branches and PRs, landing, re-runs, backfills) use QQ_GARDENER_TOKEN: in the
workflow, a short-lived installation token of the quirq infra bot (the GitHub App rollers and
test-pipelines share) with contents, pull-requests and actions write on the onboarded repos. It
cannot be the workflow's own GITHUB_TOKEN: a PR opened with that token starts no workflows, so the
revert would never be gated. Without it the forge raises NoIdentity and the gardener only reports
what it would do. TODO(suraj): create the App and its secrets QQ_BOT_CLIENT_ID, QQ_BOT_PRIVATE_KEY.
"""
from __future__ import annotations

import base64
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from qqgarden import git
from qqgarden.config import Repo
from qqgarden.errors import GardenerError
from qqgarden.forge import NoIdentity
from qqgarden.model import BuilderRun, Commit

API = "https://api.github.com"


def workflow_file(builder: str) -> str:
    return f"qq-{builder}.yml"


class Backend:
    def __init__(self, cache: str | Path = ".qq/git", token: str | None = None,
                 write_token: str | None = None, **_):
        self.cache = Path(cache)
        self.token = token if token is not None else os.environ.get("GITHUB_TOKEN", "")
        self.write_token = (write_token if write_token is not None
                            else os.environ.get("QQ_GARDENER_TOKEN", ""))

    @property
    def forge(self) -> "Backend":
        return self

    # --- history ------------------------------------------------------------------------------

    def commits(self, repo: Repo, limit: int) -> list[Commit]:
        if not repo.slug:
            raise GardenerError(f"{repo.name}: source {repo.source!r} is not on github.com")
        dest = git.mirror(f"https://github.com/{repo.slug}.git", self.cache / repo.name,
                          repo.default_branch)
        return git.first_parent(dest, repo.default_branch, limit)

    # --- runs ---------------------------------------------------------------------------------

    def runs(self, repo: Repo, builder: str, pages: int = 3) -> tuple[list[BuilderRun], str]:
        out: list[BuilderRun] = []
        for event in ("push", "workflow_dispatch"):
            path = (f"/repos/{repo.slug}/actions/workflows/{workflow_file(builder)}/runs?"
                    + urllib.parse.urlencode({"branch": repo.default_branch, "event": event,
                                              "per_page": 100}))
            for page in range(1, pages + 1):
                doc = self._get(f"{path}&page={page}")
                if doc is None:
                    return [], (f"{workflow_file(builder)} is not in {repo.slug}: the post-submit "
                                "workflow has not been delivered (infra-config `qqcfg deliver`)")
                items = doc.get("workflow_runs", [])
                out.extend(r for r in (_own_run(repo, builder, event, i) for i in items) if r)
                if len(items) < 100:
                    break
        return out, ""

    def backfill(self, repo: Repo, builder: str, commit: str) -> None:
        """Run a builder's post-submit on a main commit that has none (infra-config's dispatch
        input; the workflow itself refuses a commit that is not on the default branch)."""
        token = self._need_identity()
        self._send("POST", f"/repos/{repo.slug}/actions/workflows/{workflow_file(builder)}/dispatches",
                   token, {"ref": f"refs/heads/{repo.default_branch}", "inputs": {"commit": commit}})

    # --- evidence -----------------------------------------------------------------------------

    def failed_steps(self, repo: Repo, run_url: str) -> list[str]:
        """Names of the failed steps in a run's jobs, from its html_url (".../actions/runs/<id>")."""
        run_id = run_url.rstrip("/").rsplit("/runs/", 1)[-1].split("/")[0]
        if not run_id.isdigit():
            return []
        doc = self._get(f"/repos/{repo.slug}/actions/runs/{run_id}/jobs?per_page=100") or {}
        return [s["name"] for j in doc.get("jobs", []) for s in j.get("steps", [])
                if s.get("conclusion") in ("failure", "timed_out")]

    # --- forge --------------------------------------------------------------------------------

    def identity_problem(self) -> str:
        return "" if self.write_token else ("no bot identity: QQ_GARDENER_TOKEN (the quirq infra bot's "
                                            "token) is not set, so the "
                                            "gardener cannot push a revert or open its PR")

    def _need_identity(self) -> str:
        if problem := self.identity_problem():
            raise NoIdentity(problem)
        return self.write_token

    def clone_url(self, repo: Repo) -> str:
        return f"https://github.com/{repo.slug}.git"

    def auth_env(self) -> dict:
        """git credentials for github.com, in env only (GIT_CONFIG_*), never in argv or a URL."""
        if not self.write_token:
            return {}
        basic = base64.b64encode(f"x-access-token:{self.write_token}".encode()).decode()
        return {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "http.https://github.com/.extraheader",
                "GIT_CONFIG_VALUE_0": f"AUTHORIZATION: basic {basic}"}

    def existing_revert(self, repo: Repo, culprit: str) -> str:
        from qqgarden.revert import branch_name
        owner = repo.slug.split("/")[0]
        q = urllib.parse.urlencode({"head": f"{owner}:{branch_name(culprit)}", "state": "all"})
        # Any PR from that branch blocks a second revert; only writers can make one, and the
        # gardener's own record of what it opened is the ledger's links/, checked first.
        pulls = self._get(f"/repos/{repo.slug}/pulls?{q}") or []
        return pulls[0]["html_url"] if pulls else ""

    def branch_exists(self, repo: Repo, branch: str) -> bool:
        return self._get(f"/repos/{repo.slug}/branches/{urllib.parse.quote(branch, safe='')}") is not None

    def push(self, workdir: Path, repo: Repo, branch: str) -> None:
        self._need_identity()
        git.run(["push", "--quiet", self.clone_url(repo), f"HEAD:refs/heads/{branch}"], cwd=workdir,
                env=self.auth_env())

    def open_revert(self, repo: Repo, branch: str, base: str, title: str, body: str,
                    assignees: list[str]) -> str:
        token = self._need_identity()
        pr = self._send("POST", f"/repos/{repo.slug}/pulls", token,
                        {"title": title, "head": branch, "base": repo.default_branch, "body": body})
        if assignees:
            self._send("POST", f"/repos/{repo.slug}/issues/{pr['number']}/assignees", token,
                       {"assignees": assignees})
        return pr["html_url"]

    def queue_land(self, repo: Repo, url: str) -> None:
        """Enable auto-merge, which enters the merge queue once required checks pass."""
        token = self._need_identity()
        number = url.rstrip("/").rsplit("/", 1)[-1]
        pr = self._send("GET", f"/repos/{repo.slug}/pulls/{number}", token)
        self._send("POST", "/graphql", token, {
            "query": "mutation($id: ID!) { enablePullRequestAutoMerge(input: {pullRequestId: $id}) "
                     "{ clientMutationId } }", "variables": {"id": pr["node_id"]}})

    def commit_url(self, repo: Repo, sha: str) -> str:
        return f"https://github.com/{repo.slug}/commit/{sha}"

    def landed(self, repo: Repo, url: str) -> str:
        """The revert PR's merge commit once merged, else "". Read-only, so no identity needed."""
        number = url.rstrip("/").rsplit("/", 1)[-1]
        if not number.isdigit():
            return ""
        pr = self._get(f"/repos/{repo.slug}/pulls/{number}") or {}
        sha = pr.get("merge_commit_sha") if pr.get("merged_at") else ""
        return self.commit_url(repo, sha) if sha else ""

    def rerun(self, repo: Repo, run_url: str) -> bool:
        token = self._need_identity()
        run_id = run_url.rstrip("/").rsplit("/runs/", 1)[-1].split("/")[0]
        if not run_id.isdigit():
            return False
        self._send("POST", f"/repos/{repo.slug}/actions/runs/{run_id}/rerun-failed-jobs", token)
        return True

    def _send(self, method: str, path: str, token: str, body: dict | None = None):
        req = urllib.request.Request(API + path, method=method,
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Accept": "application/vnd.github+json",
                                              "X-GitHub-Api-Version": "2022-11-28",
                                              "Authorization": f"Bearer {token}"})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = resp.read()
        except urllib.error.HTTPError as e:
            raise GardenerError(f"GitHub API {method} {path}: HTTP {e.code} {e.reason}") from None
        except (urllib.error.URLError, TimeoutError) as e:
            raise GardenerError(f"GitHub API {method} {path}: {e}") from None
        doc = json.loads(data) if data else {}
        if isinstance(doc, dict) and doc.get("errors") and path == "/graphql":
            raise GardenerError(f"GitHub GraphQL: {doc['errors'][0].get('message', doc['errors'])}")
        return doc

    def _get(self, path: str) -> dict | list | None:
        req = urllib.request.Request(API + path, headers={
            "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28",
            **({"Authorization": f"Bearer {self.token}"} if self.token else {})})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            raise GardenerError(f"GitHub API {path}: HTTP {e.code} {e.reason}") from None
        except (urllib.error.URLError, TimeoutError) as e:
            raise GardenerError(f"GitHub API {path}: {e}") from None


def _own_run(repo: Repo, builder: str, event: str, r: dict) -> BuilderRun | None:
    """Only this repo's own runs of the generated workflow on the default branch count, so another
    workflow, a fork or a same-named file elsewhere cannot paint a builder red or green."""
    if (r.get("event") != event or r.get("path", "").split("@")[0] != f".github/workflows/{workflow_file(builder)}"
            or (r.get("head_repository") or {}).get("full_name") != repo.slug):
        return None
    if event == "push":
        return _run(builder, r, r["head_sha"])
    if r.get("head_branch") != repo.default_branch:
        return None
    name, _, commit = (r.get("display_title") or "").rpartition(" ")
    if name != builder or len(commit) != 40 or any(ch not in "0123456789abcdef" for ch in commit):
        return None
    return _run(builder, r, commit, backfill=True)   # tree_status checks head_sha is on main


def _run(builder: str, r: dict, commit: str, backfill: bool = False) -> BuilderRun:
    return BuilderRun(
        builder=builder, commit=commit, backfill=backfill, head_sha=r.get("head_sha", ""),
        status=r.get("status") or "",
        conclusion=r.get("conclusion") or "", id=str(r["id"]), attempt=int(r.get("run_attempt") or 1),
        url=r.get("html_url", ""),
        finished_at=r.get("updated_at", "") if r.get("status") == "completed" else "")
