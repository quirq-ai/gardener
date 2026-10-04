import subprocess

import pytest

from qqgarden import cli
from qqgarden.bisect import CommandProbe, Probe, bisect
from qqgarden.errors import GardenerError

S = [f"c{i}" for i in range(1, 11)]   # suspects, oldest first; c10 is the known-bad first red


def probe_for(culprit, unknown=(), flaky=()):
    seen = {}

    def probe(c):
        if c in unknown:
            return Probe.UNKNOWN
        if c in flaky:
            seen[c] = seen.get(c, 0) + 1
            return Probe.FAIL if seen[c] == 1 else Probe.PASS
        if c == "good":
            return Probe.PASS
        return Probe.FAIL if int(c[1:]) >= int(culprit[1:]) else Probe.PASS
    return probe


@pytest.mark.parametrize("culprit", S)
def test_finds_every_culprit(culprit):
    r = bisect(S, "good", probe_for(culprit), verify=True)
    assert r.culprit == culprit and r.verified
    assert len(r.probes) <= 4 + 2      # log2(10) rounds up to 4, plus two verification probes


def test_skips_commits_it_cannot_tell():
    r = bisect(S, "good", probe_for("c7", unknown={"c5"}), verify=False)
    assert r.culprit == "c7"
    assert ["c5", "unknown"] in [list(p) for p in r.probes]


def test_an_unknown_parent_leaves_both_in_play():
    r = bisect(S, "good", probe_for("c6", unknown={"c5"}), verify=False)
    assert r.culprit == "" and r.remaining == ["c5", "c6"]


def test_unknown_neighbours_leave_a_range():
    r = bisect(S, "good", probe_for("c6", unknown={"c5", "c6"}), verify=False)
    assert r.culprit == ""
    assert "c5" in r.remaining and "c6" in r.remaining


def test_flaky_culprit_does_not_verify():
    asked = {}

    def probe(c):   # c5 fails the first time only; the real break is c10
        asked[c] = asked.get(c, 0) + 1
        if c == "c5":
            return Probe.FAIL if asked[c] == 1 else Probe.PASS
        return Probe.FAIL if c == "c10" else Probe.PASS
    r = bisect(S, "good", probe, verify=True)
    assert r.culprit == "" and not r.verified
    assert "did not verify" in r.reason


def test_single_suspect():
    r = bisect(["c1"], "good", probe_for("c1"), verify=True)
    assert r.culprit == "c1" and [p[0] for p in r.probes] == ["c1", "good"]


def test_no_suspects():
    with pytest.raises(GardenerError):
        bisect([], "good", probe_for("c1"), verify=False)


def plant(tmp_path, n=12, brk=7):
    out = subprocess.run(["tools/plant-break.sh", str(tmp_path / "repo"), str(n), str(brk)],
                         check=True, capture_output=True, text=True).stdout
    return dict(line.split("=", 1) for line in out.split())


def test_command_probe_on_a_planted_break(tmp_path):
    shas = plant(tmp_path)
    probe = CommandProbe(tmp_path / "repo", "./check.sh")
    assert probe(shas["good"]) is Probe.PASS
    assert probe(shas["culprit"]) is Probe.FAIL
    assert probe(shas["bad"]) is Probe.FAIL


def test_cli_bisects_a_planted_break_to_its_commit(config_root, tmp_path, capsys):
    """V0-GAR-02 done-when."""
    shas = plant(tmp_path)
    rc = cli.main(["bisect", "--config", str(config_root), "--repo-dir", str(tmp_path / "repo"),
                   "--good", shas["good"], "--bad", shas["bad"], "--run", "./check.sh"])
    out = capsys.readouterr().out
    assert rc == 0, out
    assert f"{shas['culprit'][:12]} is the first commit that fails (verified)" in out


def test_exit_125_is_unknown(tmp_path):
    shas = plant(tmp_path)
    assert CommandProbe(tmp_path / "repo", "exit 125")(shas["good"]) is Probe.UNKNOWN
