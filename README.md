# gardener

Part of **quirq infra** ("qq"), quirq-ai's CI/CD system for repos in any language. This repo keeps
`main` green in every onboarded repo, as an agent plus a small service: it watches the post-submit
run on every main commit, publishes a tree status, groups failures by regression range, bisects
them to a culprit and opens clean reverts within the caps in infra-config's `auto_revert.toml`.

**Chromium counterpart:** the gardener (sheriff) rotations, Sheriff-o-Matic (failure grouping by
regression range) and LUCI Bisection (culprit finding and auto-revert with daily caps), plus the
tree closers.

Plan and every v0 item: [quirq-ai/infra-config](https://github.com/quirq-ai/infra-config),
`docs/plan.md` and `docs/v0.md`.

## Rules it lives by

- Every cap is read from infra-config (`auto_revert.toml`) at the commit pinned in `pins.toml`,
  never hard-coded. Changing a cap is a policy change for suraj.
- infra-config generates the post-submit builders; the gardener consumes their results, through
  [test-pipelines](https://github.com/quirq-ai/test-pipelines) and the backend.
- GitHub-specific code sits behind the `backend` field (`github` now, `launchpad` later).
- Agents land clean build-break reverts alone, within the caps. Test-failure reverts are proposed
  only. Reverts in xo-space and innernet go to suraj to merge during v0.

## Tree status and red detection (V0-GAR-01)

infra-config generates a post-submit builder for each onboarded repo (`pipelines.toml`, pipeline
`postsubmit`, trigger `land`, `cancel_in_progress = false`) and delivers it as
`.github/workflows/qq-<builder>.yml`. The gardener reads each repo's first-parent `main` history
and those runs, and works out:

- **a state per commit and builder**: green, red, pending (running, or landed under 15 minutes
  ago), missing (no run), or cancelled (ended without a verdict). Only a real failure is red; a
  re-run replaces its first attempt.
- **the tree status per repo**: `closed` while any post-submit builder's newest verdict is red,
  `open` when all are green, `unknown` while a builder has no verdict yet. A missing signal is not
  a healthy one.
- **red detection**: for each red builder, its regression range (last green .. first red) and the
  suspects in it, oldest first. V0-GAR-02 bisects these.
- **coverage**: every main commit since the repo's first post-submit run must have a result. A
  missing or cancelled one is a hole a culprit can hide in, and fails the check.

```sh
qqgarden status --config <infra-config checkout>                 # live, github backend
qqgarden status --config ... --out status/ --require-coverage    # what the workflow runs
qqgarden status --config ... --backend snapshot --snapshot tests/fixtures/every-commit.json
```

The `tree-status` workflow runs it every 10 minutes and publishes `status/<repo>.json` (schema
`qq-tree-status/1`) and `status/README.md` to this repo's `tree-status` branch, committing only
when something changes, so that branch's log is the tree's open and close history. Readers (the
release `lkgr` advancer, the gate in v1, the gardener agent) read that branch.

## v0 status

| Item | What | PR | State |
|---|---|---|---|
| V0-GAR-01 | Post-submit on every commit: red detection and tree status | #2 | in review |
| V0-GAR-02 | Group failures by regression range and bisect | | not started |
| V0-GAR-03 | Auto-revert within caps | | not started |
| V0-GAR-04 | Failure record and postmortem stub per revert | | not started |

Out of scope for v0: test-failure reverts that land, revert precision, postmortem drafting and
canary bisection (v1); agents holding the rotation (v2).

## Working here

See [AGENTS.md](AGENTS.md). Run the checks with `python -m pytest`.
