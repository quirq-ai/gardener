from datetime import timedelta

from history import NOW, history, sha

from qqgarden.model import RunState, TreeState
from qqgarden.postsubmit import tree_status

B = "b-postsubmit"
GRACE = timedelta(minutes=15)


def status(states, **kw):
    commits, runs = history(states, **kw)
    return tree_status("demo", "main", [B], commits, runs, NOW, GRACE)


def test_all_green_is_open_and_covered():
    s = status("ggggg")
    assert s.state == TreeState.OPEN
    assert s.head == sha(5)
    assert s.builders[B].state == "green" and s.builders[B].commit == sha(5)
    assert s.coverage.complete and s.coverage.commits == 5 and s.coverage.since == sha(1)
    assert s.red == []


def test_red_closes_the_tree_with_its_regression_range():
    # 1 green, 2 and 3 never ran (pushed together), 4 red, 5 red, 6 still running.
    s = status("gmmrrp")
    assert s.state == TreeState.CLOSED
    [span] = s.red
    assert span.last_good == sha(1)
    assert span.first_bad == sha(4)
    assert span.latest_bad == sha(5)
    assert span.suspects == [sha(2), sha(3), sha(4)]   # oldest first; 5 is red already, not first
    assert span.url.endswith("/1003")
    assert s.builders[B].commit == sha(5)


def test_green_after_red_reopens():
    s = status("ggrrg")
    assert s.state == TreeState.OPEN
    assert s.red == []


def test_red_with_no_green_in_window():
    s = status("mrr")
    [span] = s.red
    assert span.last_good == ""
    assert span.suspects == [sha(1), sha(2)]


def test_rerun_attempt_replaces_the_first():
    assert status("gR").state == TreeState.OPEN       # flaky red, re-run green
    assert status("gG").state == TreeState.CLOSED     # re-run red wins too


def test_missing_and_cancelled_break_coverage():
    s = status("gmgcg")
    assert s.state == TreeState.OPEN
    assert s.coverage.missing == [f"{sha(2)} {B}"]
    assert s.coverage.cancelled == [f"{sha(4)} {B}"]
    assert not s.coverage.complete


def test_recent_commit_without_run_is_pending_not_missing():
    commits, runs = history("gm", newest_age_min=5)
    s = tree_status("demo", "main", [B], commits, runs, NOW, GRACE)
    assert s.coverage.pending == [f"{sha(2)} {B}"]
    assert s.coverage.complete


def test_commits_before_onboarding_do_not_count():
    s = status("mmmgg")
    assert s.coverage.since == sha(4)
    assert s.coverage.commits == 2
    assert s.coverage.complete


def test_no_runs_at_all_is_unknown():
    s = status("mmm")
    assert s.state == TreeState.UNKNOWN
    assert s.builders[B].state == "none"
    assert s.coverage.since == ""


def test_running_only_is_unknown_not_open():
    s = status("p")
    assert s.state == TreeState.UNKNOWN


def test_any_red_builder_closes_the_tree():
    c1, r1 = history("gg", builder="one")
    _, r2 = history("gr", builder="two")
    s = tree_status("demo", "main", ["one", "two"], c1, r1 + r2, NOW, GRACE)
    assert s.state == TreeState.CLOSED
    assert [x.builder for x in s.red] == ["two"]


def test_no_builders_is_unknown():
    commits, runs = history("g")
    s = tree_status("demo", "main", [], commits, runs, NOW, GRACE)
    assert s.state == TreeState.UNKNOWN


def test_only_real_failures_are_red():
    from qqgarden.model import BuilderRun
    for conclusion, want in [("failure", RunState.RED), ("timed_out", RunState.RED),
                             ("cancelled", RunState.CANCELLED), ("skipped", RunState.CANCELLED),
                             ("something-new", RunState.CANCELLED), ("success", RunState.GREEN)]:
        assert BuilderRun(B, sha(1), "completed", conclusion).state is want


def test_json_round_trip():
    from qqgarden.model import TreeStatus
    s = status("gmmrrp")
    assert TreeStatus.from_dict(s.to_dict()) == s
    assert s.to_json() == TreeStatus.from_dict(s.to_dict()).to_json()


def test_a_later_run_of_the_same_commit_wins_over_an_older_rerun():
    from qqgarden.model import BuilderRun
    from qqgarden.postsubmit import latest_runs
    old = BuilderRun(B, sha(1), "completed", "failure", "100", 2)
    new = BuilderRun(B, sha(1), "completed", "success", "200", 1)
    assert latest_runs([old, new])[(B, sha(1))] is new


def test_naive_time_is_utc():
    from qqgarden.postsubmit import parse_time
    assert parse_time("2026-10-04T12:00:00") == NOW


def test_a_backfill_never_replaces_a_push_verdict():
    """Audit S2: a green dispatch on a commit whose own push run was red does not open the tree."""
    from dataclasses import replace
    commits, runs = history("grg")
    # commit 2's push run is red; a later dispatch from main's tip (commit 3) says it is green
    runs.append(replace(runs[1], id="9000", conclusion="success", backfill=True, head_sha=sha(3)))
    runs = [r for r in runs if r.commit != sha(3)]                     # commit 3 has no verdict yet
    s = tree_status("demo", "main", [B], commits, runs, NOW, GRACE)
    assert s.state == TreeState.CLOSED


def test_a_dispatch_run_from_its_own_commit_is_its_verdict():
    """A push that skipped CI leaves only a dispatch; run from the commit itself it is that
    commit's own verdict, and a red one names a culprit like a push run would."""
    from dataclasses import replace
    commits, runs = history("gr")
    runs[-1] = replace(runs[-1], backfill=True, head_sha=sha(2))
    s = tree_status("demo", "main", [B], commits, runs, NOW, GRACE)
    assert s.state == TreeState.CLOSED and not s.red[0].first_bad_backfill


def test_rerunning_a_red_head_keeps_the_tree_closed():
    """Audit S4: the verifying re-run of a red head must not open the tree while it runs."""
    from qqgarden.model import BuilderRun
    commits, runs = history("gr")
    runs.append(BuilderRun(B, sha(2), "in_progress", "", "1001", 2, runs[-1].url, prior="failure"))
    s = tree_status("demo", "main", [B], commits, runs, NOW, GRACE)
    assert s.state == TreeState.CLOSED and s.red[0].first_bad_running


def test_a_future_dated_commit_is_a_hole_not_pending_forever():
    from dataclasses import replace
    commits, runs = history("gg")
    commits[0] = replace(commits[0], landed_at=(NOW + timedelta(days=30)).isoformat())
    runs = [r for r in runs if r.commit != commits[0].sha]
    s = tree_status("demo", "main", [B], commits, runs, NOW, GRACE)
    assert f"{commits[0].sha} {B}" in s.coverage.missing


def test_green_names_the_newest_all_green_commit():
    assert status("ggrr").green == sha(2)
    assert status("gggp").green == sha(3)
    assert status("rr").green == ""
