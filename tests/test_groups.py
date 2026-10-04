from history import NOW, history

from datetime import timedelta

from qqgarden.groups import classify, group
from qqgarden.postsubmit import tree_status


class FakeEvidence:
    def __init__(self, steps=None, tests=None):
        self.steps = steps or {}
        self.tests = tests or {}

    def failed_steps(self, repo, run_url):
        return self.steps.get(run_url, [])

    def unexpected_tests(self, repo, builder, commit):
        return self.tests.get((builder, commit))


def test_classify():
    assert classify(["build (python-service)"], None) == "build"
    assert classify(["fetch (pytest)"], None) == "infra"
    assert classify(["test (pytest)"], None) == "test"
    assert classify([], ["a::b"]) == "test"
    assert classify(["build (x)"], ["a::b"]) == "build"
    assert classify(["qq result sink"], None) == "infra"
    assert classify([], None) == "unknown"


def test_builders_with_the_same_range_form_one_group():
    c, r1 = history("gmr", builder="one")
    _, r2 = history("gmr", builder="two")
    s = tree_status("demo", "main", ["one", "two"], c, r1 + r2, NOW, timedelta(minutes=15))
    ev = FakeEvidence(steps={r1[-1].url: ["test (pytest)"]}, tests={("one", c[0].sha): ["t::x"]})
    [g] = group(s, ev)
    assert g.builders == ["one", "two"]
    assert g.kind == "test" and g.tests == ["t::x"]
    assert g.suspects == [c[1].sha, c[0].sha]
    assert g.key.startswith("demo@") and g.to_dict()["key"] == g.key


def test_different_ranges_are_separate_groups_and_build_wins():
    c, r1 = history("ggr", builder="one")
    _, r2 = history("grr", builder="two")
    s = tree_status("demo", "main", ["one", "two"], c, r1 + r2, NOW, timedelta(minutes=15))
    ev = FakeEvidence(steps={r2[1].url: ["build (node-app)"]})
    gs = group(s, ev)
    assert len(gs) == 2
    assert {g.kind for g in gs} == {"build", "unknown"}


def test_no_red_no_groups():
    c, r = history("gg", builder="one")
    s = tree_status("demo", "main", ["one"], c, r, NOW, timedelta(minutes=15))
    assert group(s) == []
