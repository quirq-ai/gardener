from qqgarden import config


def test_onboarded_repos_have_postsubmit_builders(cfg):
    repos = {r.name: r for r in config.repos(cfg)}
    assert set(repos) >= {"xo-space", "innernet"}
    for r in repos.values():
        assert r.postsubmit, f"{r.name} has no post-submit builder"
        assert r.slug.startswith("quirq-ai/")
        assert r.timeout_minutes > 0


def test_no_postsubmit_builder_is_cancellable(cfg):
    assert config.cancellable(cfg) == []


def test_cancellable_builder_is_reported(cfg):
    b = next(b for b in cfg["pipelines"]["builder"] if b["pipeline"] == "postsubmit")
    del b["cancel_in_progress"]
    assert b["name"] in config.cancellable(cfg)
