"""Builds commit histories and post-submit runs for tests: a compact way to write a timeline."""
from datetime import datetime, timedelta, timezone

from qqgarden.model import BuilderRun, Commit

NOW = datetime(2026, 10, 4, 12, 0, tzinfo=timezone.utc)


def sha(i: int) -> str:
    return f"{i:040x}"


def history(states: str, builder: str = "b-postsubmit", step_min: int = 20,
            newest_age_min: int = 60) -> tuple[list[Commit], list[BuilderRun]]:
    """`states` lists commits oldest first, one letter each:
    g green, r red, p running, m no run, c cancelled, R red then re-run green, G green then re-run red.
    Returns commits newest first, as a backend does."""
    commits, runs = [], []
    n = len(states)
    for i, ch in enumerate(states):
        age = newest_age_min + (n - 1 - i) * step_min
        c = Commit(sha=sha(i + 1), landed_at=(NOW - timedelta(minutes=age)).isoformat().replace("+00:00", "Z"),
                   title=f"commit {i + 1}")
        commits.append(c)
        rid = str(1000 + i)
        url = f"https://example.invalid/runs/{rid}"
        if ch == "g":
            runs.append(BuilderRun(builder, c.sha, "completed", "success", rid, 1, url))
        elif ch == "r":
            runs.append(BuilderRun(builder, c.sha, "completed", "failure", rid, 1, url))
        elif ch == "p":
            runs.append(BuilderRun(builder, c.sha, "in_progress", "", rid, 1, url))
        elif ch == "c":
            runs.append(BuilderRun(builder, c.sha, "completed", "cancelled", rid, 1, url))
        elif ch == "R":
            runs.append(BuilderRun(builder, c.sha, "completed", "failure", rid, 1, url))
            runs.append(BuilderRun(builder, c.sha, "completed", "success", rid, 2, url))
        elif ch == "G":
            runs.append(BuilderRun(builder, c.sha, "completed", "success", rid, 1, url))
            runs.append(BuilderRun(builder, c.sha, "completed", "failure", rid, 2, url))
        elif ch != "m":
            raise ValueError(ch)
    return list(reversed(commits)), runs
