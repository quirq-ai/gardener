"""A product repo on a local bare remote with a planted build break, plus the snapshot the gardener
would see for it: green post-submits before the break, red from the break on."""
import json
import subprocess
from datetime import timedelta
from pathlib import Path

from history import NOW


def g(cwd, *a):
    return subprocess.run(["git", *a], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def build(tmp: Path, repo="xo-space", n=6, brk=5, step_min=10, red_after_break=True, conflict=False,
          rerun_red=True):
    """Commits 1..n land step_min apart, the newest `step_min` ago... the break at `brk` only touches
    `state`; other commits add their own file, so reverting the break is clean (unless `conflict`)."""
    work, remote = tmp / f"{repo}-work", tmp / f"{repo}.git"
    work.mkdir()
    g(work, "init", "-q", "-b", "main")
    g(work, "config", "user.email", "dev@example.invalid")
    g(work, "config", "user.name", "dev")
    commits, runs, steps = [], [], {}
    for i in range(1, n + 1):
        landed = NOW - timedelta(minutes=step_min * (n - i + 1))
        if i == 1:
            (work / "state").write_text("ok\n")
        if i == brk:
            (work / "state").write_text("broken\n")
        elif conflict and i > brk:
            (work / "state").write_text(f"broken {i}\n")
        (work / f"f{i}").write_text(f"{i}\n")
        g(work, "add", "-A")
        when = landed.strftime("%Y-%m-%dT%H:%M:%SZ")
        subprocess.run(["git", "commit", "-q", "-m", f"commit {i}"], cwd=work, check=True,
                       env={"GIT_COMMITTER_DATE": when, "GIT_AUTHOR_DATE": when, "PATH": "/usr/bin:/bin"})
        sha = g(work, "rev-parse", "HEAD")
        commits.insert(0, {"sha": sha, "landed_at": when, "title": f"commit {i}"})
        red = i >= brk and (red_after_break or i == brk)
        url = f"https://example.invalid/{repo}/runs/{100 + i}"
        runs.append({"builder": f"{repo}-postsubmit", "commit": sha, "status": "completed",
                     "conclusion": "failure" if red else "success", "id": str(100 + i), "attempt": 1,
                     "url": url})
        if red:
            steps[url] = ["build (demo)"]
        if i == brk and rerun_red:   # the culprit's post-submit was re-run, and is red again
            runs.append({**runs[-1], "attempt": 2})
    g(tmp, "clone", "-q", "--bare", str(work), str(remote))
    return {"commits": commits, "runs": runs, "failed_steps": steps, "remote": remote.name}


def snapshot(tmp: Path, **repos) -> Path:
    p = tmp / "snap.json"
    p.write_text(json.dumps({"repos": repos}))
    return p


def remote_main_state(tmp: Path, repo="xo-space") -> str:
    return g(tmp / f"{repo}.git", "show", "main:state")
