import json

from history import NOW, history

from qqgarden import cli


def snapshot(tmp_path, xo: str, inn: str):
    data = {"repos": {}}
    for repo, states in (("xo-space", xo), ("innernet", inn)):
        commits, runs = history(states, builder=f"{repo}-postsubmit")
        data["repos"][repo] = {"commits": [c.__dict__ for c in commits], "runs": [r.__dict__ for r in runs]}
    p = tmp_path / "snap.json"
    p.write_text(json.dumps(data))
    return p


def run(config_root, snap, *extra):
    return cli.main(["status", "--config", str(config_root), "--backend", "snapshot", "--snapshot", str(snap),
                     "--now", NOW.isoformat(), *extra])


def test_every_commit_covered_passes(config_root, tmp_path, capsys):
    snap = snapshot(tmp_path, "gggrg", "gggg")
    out = tmp_path / "out"
    assert run(config_root, snap, "--require-coverage", "--out", str(out)) == 0
    xo = json.loads((out / "xo-space.json").read_text())
    assert xo["state"] == "open" and xo["schema"] == "qq-tree-status/1"
    assert "| innernet | **open** |" in (out / "README.md").read_text()


def test_a_hole_fails_coverage(config_root, tmp_path, capsys):
    snap = snapshot(tmp_path, "ggmgg", "ggcg")
    assert run(config_root, snap, "--require-coverage") == 1
    err = capsys.readouterr().err
    assert "xo-space-postsubmit: no post-submit result" in err
    assert "innernet-postsubmit: post-submit run ended without a verdict" in err


def test_no_results_fails_coverage(config_root, tmp_path, capsys):
    snap = snapshot(tmp_path, "mm", "gg")
    assert run(config_root, snap, "--require-coverage") == 1
    assert "xo-space: no post-submit result on any listed main commit" in capsys.readouterr().err


def test_red_is_reported(config_root, tmp_path, capsys):
    snap = snapshot(tmp_path, "ggmr", "gg")
    assert run(config_root, snap, "--repo", "xo-space", "--json") == 0
    [s] = json.loads(capsys.readouterr().out)
    assert s["state"] == "closed"
    assert len(s["red"][0]["suspects"]) == 2


def test_unknown_repo_is_an_error(config_root, tmp_path, capsys):
    snap = snapshot(tmp_path, "g", "g")
    assert run(config_root, snap, "--repo", "nope") == 2
    assert "not onboarded" in capsys.readouterr().err
