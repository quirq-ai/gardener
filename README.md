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
  ago; a commit dated in the future is not pending, so it cannot hide), missing (no run), or
  cancelled (ended without a verdict). Only a real failure is red; a re-run replaces its first
  attempt, and while a re-run is in progress the commit keeps its last verdict, so re-running a
  red head never opens the tree.
- **the tree status per repo**: `closed` while any post-submit builder's newest verdict is red,
  `open` when all are green, `unknown` while a builder has no verdict yet. A missing signal is not
  a healthy one. `green` names the newest commit every builder passed on.
- **red detection**: for each red builder, its regression range (last green .. first red) and the
  suspects in it, oldest first. V0-GAR-02 bisects these.
- **coverage**: every main commit since the repo's first post-submit run must have a result (a repo with
  no run yet is a warning: it is not onboarded). A
  missing or cancelled one is a hole a culprit can hide in, and fails the check.
- **backfill**: a push of several commits runs only the newest, so the gardener's cycle fills each
  hole by dispatching that builder's workflow with the commit (infra-config's `commit` input),
  oldest first, with at most 10 in flight per repo. A dispatched run's head is the branch tip, so
  it is matched by its run-name, `<builder> <commit>`, and counts only when it ran from a `main`
  commit at or after the one it names (checked in the backend, so every reader gets it). It only
  fills a hole: a push run's verdict always wins over a backfill's. A dispatch run from the commit
  itself stands in for a push run only when there is none (or it was cancelled). A backfill runs main's newer workflow on the older commit,
  so a red backfill never names a culprit by itself: the cycle asks for a bisection. A hole whose
  backfill was cancelled too is left for a person, and re-running its push run clears it.
  Dispatching needs the bot identity.

```sh
qqgarden status --config <infra-config checkout>                 # live, github backend
qqgarden status --config ... --out status/ --require-coverage    # what the workflow runs
qqgarden status --config ... --backend snapshot --snapshot tests/fixtures/every-commit.json
```

The `tree-status` workflow runs it every 5 minutes and publishes `status/<repo>.json` (schema
`qq-tree-status/1`) and `status/README.md` to this repo's `tree-status` branch, committing only
when something changes, so that branch's log is the tree's open and close history. People, the
gate in v1 and the gardener agent read that branch. release's `lkgr` advancer does not: it
recomputes the verdicts itself by importing qqgarden at its own pinned commit.

## Grouping and bisection (V0-GAR-02)

`qqgarden groups` turns the red builders of each repo into failure groups, one per regression
range (Sheriff-o-Matic's grouping): builders that went red over the same last-good..first-bad
range share one culprit search. Each group says what failed, because the revert caps differ by
failure type:

- `build` when a `build (...)` step failed (generated builders name each step after its
  capability), `test` when a `test (...)` step failed, `infra` when anything else failed (a
  `fetch (...)` step, the result sink, the runner), else `unknown`. `infra` and `unknown` are
  never reverted. The results store's unexpected tests are shown, but never change the type until
  test-pipelines checks where each record came from;
- the failing tests, read from test-pipelines' results store (`--store`, a checkout of its
  `results` branch).

`qqgarden bisect` finds the culprit in a range by binary search, probing each commit with a
command in a scratch worktree (exit 0 pass, 125 can't tell, anything else fail, as with
`git bisect run`). For a qq repo the command is depot's `qq build` or `qq test`. Commits it can't
tell are skipped. With `require_culprit_verification` (auto_revert.toml) the culprit is probed
again and its parent once more, so a flaky probe can't name a culprit. A range of one suspect
still gets verified.

```sh
qqgarden groups --config <infra-config> --store <results checkout>
qqgarden bisect --config <infra-config> --repo-dir <clone> --good <sha> --bad <sha> --run 'qq test'
```

Presubmit plants a break in a 12-commit repo (`tools/plant-break.sh`) and checks that bisection
names it, verified.

TODO(expert): a probe that re-runs the repo's own post-submit builder on a commit, once
infra-config's generated post-submit accepts a commit input.

## Auto-revert within caps (V0-GAR-03)

`qqgarden cycle` runs every 5 minutes (the `cycle` job of the `tree-status` workflow). For each
failure group it finds a culprit, and reverts it if the caps allow:

- **Culprit.** A range of one commit (its parent is green) names it. It must be verified
  (`require_culprit_verification`) with and without it, for every builder in the group: the
  culprit's own post-submit run is red on its last two attempts and was never green, and its
  parent's is green on its last two and was never red, the last of them started after the
  culprit first failed. A later red commit proves nothing (it may
  be another break). Until then the cycle re-runs the culprit's failed jobs and the parent's whole
  run and waits; after 3 attempts without that, a person looks. A group that errors, or a revert branch left without a
  PR ("stuck", for a person), never stops the rest of the cycle. A longer range needs `qqgarden bisect`,
  which runs the repo's code, so the cycle leaves it to the gardener agent, which then runs
  `qqgarden revert --culprit <sha> --bisect-json <bisect --json output> --ledger <ledger worktree>
  --publish-ledger ledger`. That path keeps the cycle's rules: the culprit must be a suspect of a
  red range now, the failure type comes from that range's runs, the bisection must name this
  culprit verified (a failing probe of it and a passing probe of its first parent), and the caps
  are counted on the freshly pulled shared ledger, a worktree of this repo. The bisection itself
  stays attested by the agent that ran it, whose probe command it chose.
- **After a revert.** Once a revert is on main and the group is still red on it or later, a
  person looks ("still red after its revert"): another break may hide behind the first. The
  gardener never reverts its own revert (`Revert <sha12> (qq gardener)`).
- **Evidence.** Only GitHub's own data decides: this repo's `push` runs of the generated
  `qq-<builder>.yml` and their failed step names. Stored test verdicts are shown but never change
  a failure's type until test-pipelines checks where each record came from.
- **Caps**, all from infra-config's `auto_revert.toml` and none in code: at most `daily_cap` (10)
  reverts created per rolling `window_hours` (24) across all repos and failure types, counting
  every created revert, proposed or landed (suraj can change that default). Beneath it, per type,
  `create_daily_limit`, `submit_daily_limit` and `max_culprit_age_hours`: build breaks land on
  their own at most 4 a day and only for culprits under 6 h old; test failures are proposed only
  (submit limit 0). An `unknown` failure is never reverted.
- **Who may auto-land.** Only repos listed in `auto_land_repos` (asked of infra-config for
  CFG-05). With none, every revert is proposed: in v0, xo-space and innernet reverts go to suraj
  to merge.
- **Clean only.** The revert is made in a scratch clone first; one that conflicts or changes
  nothing is refused and not counted.
- **Ledger.** Each revert is reserved in the `ledger` branch (write-once `reverts/<id>.json`),
  pushed before its branch and PR exist, so a run that dies midway has still counted it. If
  another writer pushed first, the decision is re-made on the merged ledger before it counts. The cap
  counts the gardener's own records, never what a PR or commit claims about itself, and the
  gardener only ever lands a PR it opened in the same run, at the revert commit it made
  (auto-merge pinned to that head). A reservation that decided to land counts against
  `submit_daily_limit` from the moment it is written, so two racing writers cannot both take the
  last slot. A missing `ledger` branch stops the cycle once the App exists (until then it only
  reports); a person starts the first one with a manual run and `bootstrap-ledger`, which pushes
  an empty start commit. TODO(suraj): rulesets on `ledger` and `tree-status` (no deletion or
  force push), asked of gate.
- **Titles.** Revert PRs and commits are titled `Revert <sha12> (qq gardener)`; the culprit's own
  title appears only as inline code in the body, so it cannot mention, link or close anything.
- **Identity.** Revert PRs, re-runs and backfills use a short-lived installation token of the
  gardener's own GitHub App, "quirq gardener" (one App per tool, so no other tool's key can mint
  its permissions). It needs, on the onboarded repos only:
  - contents: write, to push revert branches;
  - pull requests: write, to open revert PRs (and enable auto-merge once `auto_land_repos` allows);
  - actions: write, to re-run a culprit's post-submit (verification) and dispatch backfills; read
    is not enough for either;
  - metadata: read (always granted).

  The workflow mints the token for exactly the repos infra-config onboards and passes it as
  `QQ_GARDENER_TOKEN`. A PR opened with a workflow's own `GITHUB_TOKEN` starts no workflows and
  would never be gated. Without the App the cycle reports what it would do and creates nothing.
  Its secrets, `QQ_GARDENER_APP_CLIENT_ID` and `QQ_GARDENER_APP_PRIVATE_KEY`, live in this repo's
  `quirq-gardener` environment, limited to `main`, so a workflow on another branch cannot mint the
  token. The token has no `workflows` permission, so a culprit
  that changed `.github/workflows/` cannot be reverted automatically (the push is refused).

Presubmit shows both done-whens offline: a planted build break, red on its post-submit and on
that run's re-run (with its parent green twice), is reverted and main is green again, 20 minutes
after it landed; and with 10 reverts in the ledger an 11th is refused. The 30 minutes is offline
only in v0: the test allows auto-landing for xo-space, while v0's `auto_land_repos` is empty, so
live reverts are proposed and land when suraj merges them. With auto-landing, the time to revert
is the post-submit run, plus up to 5 minutes for the next cycle, plus the verifying re-runs and
the cycle after them.

Next to its write tokens the workflow runs only this repo's code and qqresults from source at
their pins, plus the hash-checked wheels in `requirements/runtime.lock`. No checkout keeps
credentials; the job token reaches only the steps that push, scoped to this repo's URL.

## Failure record and postmortem stub per revert (V0-GAR-04)

Every revert the cycle creates gets one failure record, test-pipelines' `Failure` of kind
`auto-revert` opened through qqresults, so qq has one record format. Its id comes from the repo
and the culprit, so a culprit never gets two records. The record links:

- `culprit`: the culprit commit;
- `operation`: the revert PR;
- `postmortem`: a stub issue opened from infra-config's `templates/postmortem.md`, when
  `postmortem.toml`'s `auto-revert` trigger asks for one (`stub` in v0), with the trigger, repo,
  record, timeline and culprit filled in;
- `fix`: the revert's commit, linked by a later cycle once the revert is on main, whether the
  gardener landed it or a person merged it;
- `issue`: its labelled `qq-failure` issue mirror.

It closes when the owner links the covering test (`qqresults failure link`), as postmortem.toml's
`record_needs` asks. Records live in the `ledger` branch under `failures/`; issues and stubs are
opened in this repo with the workflow's token. A security-looking record is never mirrored and
gets no public stub. Presubmit shows the done-when offline: a planted break's revert links
culprit, revert and fix, with one stub.

Each record is mirrored only when it changed, and a revert's landing is polled for 14 days, so a
finished record costs no API calls. A record that fails is reported as `record-failed` and never
stops the cycle. Commit titles appear as inline code in stubs and revert PRs, so a title cannot
mention people or add links.

TODO(suraj): file stubs in the affected repo instead, which needs the bot identity there.

## v0 status

| Item | What | PR | State |
|---|---|---|---|
| V0-GAR-01 | Post-submit on every commit: red detection, tree status and backfill | #2, #6 | merged |
| V0-GAR-02 | Group failures by regression range and bisect | #3 | merged |
| V0-GAR-03 | Auto-revert within caps | #4 | merged |
| V0-GAR-04 | Failure record and postmortem stub per revert | #5 | merged |

Every item's done-when runs offline in presubmit. The wave 4 audit's fixes (B1, B2, S1-S8) are in
the audit-fixes PR. Live runs wait on: the redelivered post-submits with the backfill input
(xo-space #215, innernet #40), the quirq gardener App, one manual run with `bootstrap-ledger`, and
the `ledger`/`tree-status` rulesets. No revert lands on its own until a repo is listed in
`auto_land_repos`.

Out of scope for v0: test-failure reverts that land, revert precision, postmortem drafting and
canary bisection (v1); agents holding the rotation (v2).

## Working here

See [AGENTS.md](AGENTS.md). Run the checks with `python -m pytest`.

## Licence

Apache-2.0, see [LICENSE](LICENSE).
