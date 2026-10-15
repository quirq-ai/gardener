"""groups/groups.json: why each red range broke, published next to the status files for the
dashboard. The attempt it reads, what it carries over, and all-or-nothing writing."""
import json

import pytest

from qqgarden import cli, groupsfile
from qqgarden.model import RedSpan, TreeStatus

A, B, C = "a" * 40, "b" * 40, "c" * 40
RUN = "https://github.com/quirq-ai/demo/actions/runs/7"


def status(*spans: RedSpan, repo: str = "demo") -> TreeStatus:
    return TreeStatus.from_dict({"repo": repo, "state": "closed", "red": [s.__dict__ for s in spans]})


def span(builder="demo-postsubmit", url=RUN, attempt=1, running=False, last_good=A, first_bad=C):
    return RedSpan(builder=builder, first_bad=first_bad, latest_bad=first_bad, last_good=last_good,
                   suspects=[B, C], url=url, first_bad_attempt=attempt, first_bad_running=running)


class Reader:
    """Failed steps per (run, attempt); None is an attempt that is not complete."""

    def __init__(self, steps):
        self.steps = steps
        self.calls = []

    def __call__(self, repo, url, n):
        self.calls.append((url, n))
        return self.steps.get((url, n))


def test_the_first_attempt_decides_whatever_a_rerun_shows():
    read = Reader({(RUN, 1): ["build (node-app)"], (RUN, 2): ["fetch (npm)"]})
    doc = groupsfile.build([status(span(attempt=2))], read)
    assert doc["schema"] == "qq-gardener-groups/1"
    [g] = doc["groups"]
    assert g["kind"] == "build" and g["failed_steps"] == ["demo-postsubmit: build (node-app)"]
    assert read.calls == [(RUN, 1)]


def test_a_rerun_in_progress_keeps_attempt_ones_type():
    read = Reader({(RUN, 1): ["test (pytest)"]})          # attempt 2 is running: None
    [g] = groupsfile.build([status(span(attempt=2, running=True))], read)["groups"]
    assert g["kind"] == "test"


@pytest.mark.parametrize("first", [[], None])
def test_falls_back_to_the_newest_completed_attempt_with_a_failed_step(first):
    # attempt 1 green (no failed step) or cancelled before any job finished; 3 is still running
    read = Reader({(RUN, 1): first, (RUN, 2): ["build (go)"]})
    [g] = groupsfile.build([status(span(attempt=3, running=True))], read)["groups"]
    assert g["kind"] == "build"
    assert read.calls == [(RUN, 1), (RUN, 2)]
    assert groupsfile.attempts_to_read(span(attempt=3, running=True)) == [1, 2]
    assert groupsfile.attempts_to_read(span(attempt=3)) == [1, 3, 2]


def test_no_failed_step_anywhere_is_unknown():
    [g] = groupsfile.build([status(span(attempt=2))], Reader({}))["groups"]
    assert g["kind"] == "unknown" and g["failed_steps"] == []


def test_builders_on_one_range_share_a_group_and_infra_wins():
    other = RUN.replace("/7", "/8")
    read = Reader({(RUN, 1): ["build (x)"], (other, 1): ["fetch (y)"]})
    [g] = groupsfile.build([status(span(), span(builder="lint-postsubmit", url=other))], read)["groups"]
    assert g["builders"] == ["demo-postsubmit", "lint-postsubmit"] and g["kind"] == "infra"


def test_an_unchanged_range_is_carried_over_and_a_moved_one_is_read_again():
    read = Reader({(RUN, 1): ["build (x)"]})
    first = groupsfile.build([status(span())], read)
    again = Reader({})
    assert groupsfile.build([status(span())], again, first) == first
    assert again.calls == []
    # the range moved (a new last green), so it is read again
    moved = groupsfile.build([status(span(last_good=B))], again, first)
    assert again.calls == [(RUN, 1)] and moved["groups"][0]["kind"] == "unknown"
    # a new first red run on the same range is read again too
    again.calls.clear()
    groupsfile.build([status(span(url=RUN.replace("/7", "/9")))], again, first)
    assert again.calls == [(RUN.replace("/7", "/9"), 1)]
    # a previous file of another schema is ignored
    again.calls.clear()
    groupsfile.build([status(span())], again, {**first, "schema": "qq-gardener-groups/2"})
    assert again.calls == [(RUN, 1)]


def test_an_unknown_range_is_read_again_until_a_completed_attempt_shows_its_type():
    # attempt 1 had no jobs (a startup failure): unknown
    first = groupsfile.build([status(span())], Reader({}))
    assert first["groups"][0]["kind"] == "unknown"
    # its re-run completed red with a failed step: same range and run, read again
    rerun = Reader({(RUN, 2): ["build (x)"]})
    again = groupsfile.build([status(span(attempt=2))], rerun, first)
    assert rerun.calls == [(RUN, 1), (RUN, 2)] and again["groups"][0]["kind"] == "build"


def test_a_carried_entry_keeps_only_its_known_fields_or_is_read_again():
    read = Reader({(RUN, 1): ["build (x)"]})
    first = groupsfile.build([status(span())], read)
    planted = json.loads(json.dumps(first))
    planted["groups"][0]["title"] = "a commit title that must never be published"
    again = Reader({})
    assert groupsfile.build([status(span())], again, planted) == first and again.calls == []
    for broken in ({"kind": None}, {"failed_steps": None}, {"runs": [[RUN]]}, {"runs": [{"u": RUN}]}, {"repo": 1}):
        bad = json.loads(json.dumps(first))
        bad["groups"][0].update(broken)
        again.calls.clear()
        groupsfile.build([status(span())], again, bad)
        assert again.calls == [(RUN, 1)], broken
    for junk in ({"schema": groupsfile.SCHEMA, "groups": "x"}, {"schema": groupsfile.SCHEMA, "groups": [1, None]}):
        again.calls.clear()
        groupsfile.build([status(span())], again, junk)
        assert again.calls == [(RUN, 1)]


def test_write_replaces_the_file_through_a_temp_file(tmp_path, monkeypatch):
    out = tmp_path / "groups" / "groups.json"
    seen = []
    real = groupsfile.os.replace
    monkeypatch.setattr(groupsfile.os, "replace", lambda a, b: (seen.append((a, b, out.exists())), real(a, b)))
    groupsfile.write(out, {"schema": groupsfile.SCHEMA, "groups": []})
    assert seen == [(out.with_name(".groups.json.tmp"), out, False)]
    assert out.read_text() == groupsfile.to_json({"schema": groupsfile.SCHEMA, "groups": []})


def test_the_file_carries_nothing_that_changes_per_run():
    doc = groupsfile.build([status(span())], Reader({(RUN, 1): ["build (x)"]}))
    text = groupsfile.to_json(doc)
    assert set(doc) == {"schema", "groups"}
    assert set(doc["groups"][0]) == {"repo", "last_good", "first_bad", "builders", "runs", "kind", "failed_steps"}
    assert text == groupsfile.to_json(json.loads(text))      # stable bytes, so unchanged = no commit


def test_cli_writes_all_or_nothing(tmp_path, monkeypatch, config_root):
    statuses = tmp_path / "status"
    statuses.mkdir()
    (statuses / "xo-space.json").write_text(json.dumps(status(span(), repo="xo-space").to_dict()))
    (statuses / "README.md").write_text("# Tree status\n")
    out = tmp_path / "groups" / "groups.json"

    class Backend:
        fail = False

        def attempt_failed_steps(self, repo, url, n):
            if self.fail:
                raise cli.GardenerError("GitHub API: HTTP 502 Bad Gateway")
            return ["build (x)"] if n == 1 else None

    backend = Backend()
    monkeypatch.setattr(cli, "_backend", lambda args, cfg: backend)
    argv = ["groups", "--config", str(config_root), "--from-status", str(statuses), "--previous", str(out), "--out", str(out)]
    assert cli.main(argv) == 0
    good = out.read_text()
    assert json.loads(good)["groups"][0]["kind"] == "build"
    # a failed read leaves the last file exactly as it was, and no temp file behind
    backend.fail = True
    statuses.joinpath("xo-space.json").write_text(json.dumps(status(span(last_good=B), repo="xo-space").to_dict()))
    assert cli.main(argv) == 2
    assert out.read_text() == good
    assert sorted(p.name for p in out.parent.iterdir()) == ["groups.json"]
    # a first run that fails writes no file at all
    fresh = tmp_path / "fresh" / "groups.json"
    assert cli.main(argv[:-4] + ["--out", str(fresh)]) == 2
    assert not fresh.exists()


def test_cli_skips_a_status_file_for_a_repo_no_longer_onboarded(tmp_path, monkeypatch, config_root, capsys):
    statuses = tmp_path / "status"
    statuses.mkdir()
    (statuses / "nope.json").write_text(json.dumps(status(span(), repo="nope").to_dict()))
    (statuses / "xo-space.json").write_text(json.dumps(status(span(), repo="xo-space").to_dict()))

    class Backend:
        def attempt_failed_steps(self, repo, url, n):
            return ["build (x)"] if n == 1 else None

    monkeypatch.setattr(cli, "_backend", lambda args, cfg: Backend())
    out = tmp_path / "groups.json"
    assert cli.main(["groups", "--config", str(config_root), "--from-status", str(statuses), "--out", str(out)]) == 0
    assert [g["repo"] for g in json.loads(out.read_text())["groups"]] == ["xo-space"]
    assert "::warning::status file for nope" in capsys.readouterr().err


def test_cli_refuses_to_write_from_no_status_files(tmp_path, monkeypatch, config_root):
    monkeypatch.setattr(cli, "_backend", lambda args, cfg: object())
    out = tmp_path / "groups.json"
    out.write_text("kept\n")
    for directory in (tmp_path / "missing", tmp_path):
        assert cli.main(["groups", "--config", str(config_root), "--from-status", str(directory), "--out", str(out)]) == 2
    assert out.read_text() == "kept\n"
    # --previous and --out mean nothing without --from-status
    assert cli.main(["groups", "--config", str(config_root), "--out", str(out)]) == 2
