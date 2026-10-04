"""qqgarden: the gardener's command line.

    qqgarden status --config <infra-config checkout> [--repo NAME]... [--out DIR] [--require-coverage]
    qqgarden groups --config <infra-config checkout> [--repo NAME]... [--store DIR]
    qqgarden bisect --config <infra-config checkout> --repo-dir DIR --good SHA --bad SHA --run CMD
    qqgarden cycle  --config <infra-config checkout> --ledger DIR [--dry-run]
    qqgarden revert --config <infra-config checkout> --ledger DIR --repo NAME --culprit SHA --kind build|test
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from qqgarden import backends, bisect, config, git, groups, postsubmit
from qqgarden.errors import GardenerError
from qqgarden.model import TreeStatus

# How long after a commit lands its post-submit run may take to show up before the commit counts as
# missing a result. A run is listed as soon as it is queued, so this only absorbs event delivery lag.
# TODO(expert): move to infra-config if it ever needs tuning per repo.
GRACE_MINUTES = 15


def _backend(args, cfg: dict):
    name = args.backend or cfg.get("pipelines", {}).get("defaults", {}).get("backend", "")
    if not name:
        raise GardenerError("no backend: pass --backend or set pipelines.toml [defaults] backend")
    return backends.load(name, path=args.snapshot, cache=args.cache,
                         forge_dir=getattr(args, "forge_dir", None))


def statuses(args, cfg: dict | None = None, backend=None) -> list[TreeStatus]:
    cfg = cfg or config.load(Path(args.config))
    repos = config.repos(cfg)
    if args.repo:
        unknown = set(args.repo) - {r.name for r in repos}
        if unknown:
            raise GardenerError(f"not onboarded in infra-config repos.toml: {', '.join(sorted(unknown))}")
        repos = [r for r in repos if r.name in args.repo]
    cancellable = set(config.cancellable(cfg))
    backend = backend or _backend(args, cfg)
    now = postsubmit.parse_time(args.now) if args.now else datetime.now(timezone.utc)
    out = []
    for repo in repos:
        notes = [f"{b}: config lets a newer commit cancel it (set cancel_in_progress = false)"
                 for b in repo.postsubmit if f"{repo.name}/{b}" in cancellable]
        out.append(postsubmit.observe(backend, repo, args.limit, now,
                                      timedelta(minutes=args.grace_minutes), notes)[0])
    return out


def summary(items: list[TreeStatus]) -> str:
    lines = ["# Tree status", "",
             "| Repo | Tree | Why | Commits checked | Missing | Cancelled |", "|---|---|---|---|---|---|"]
    for s in items:
        cov = s.coverage
        lines.append(f"| {s.repo} | **{s.state}** | {s.reason} | {cov.commits} | {len(cov.missing)} "
                     f"| {len(cov.cancelled)} |")
    for s in items:
        for r in s.red:
            rng = f"{r.last_good[:12] or '(no green in window)'}..{r.first_bad[:12]}"
            lines.append(f"\n- {s.repo} `{r.builder}` red since {r.first_bad[:12]}; regression range "
                         f"{rng}, {len(r.suspects)} suspect(s). First red run: {r.url}")
        for n in s.notes:
            lines.append(f"\n- {s.repo}: {n}")
    return "\n".join(lines) + "\n"


def cmd_status(args) -> int:
    items = statuses(args)
    if args.out:
        out = Path(args.out)
        out.mkdir(parents=True, exist_ok=True)
        for s in items:
            (out / f"{s.repo}.json").write_text(s.to_json())
        (out / "README.md").write_text(summary(items))
    if args.json:
        print(json.dumps([s.to_dict() for s in items], sort_keys=True, indent=2))
    else:
        print(summary(items), end="")
    if args.require_coverage:
        bad = []
        for s in items:
            if not s.coverage.since:
                # Not onboarded yet: coverage starts with a repo's first post-submit run.
                print(f"::warning::{s.repo}: no post-submit result on any listed main commit yet; "
                      + "; ".join(s.notes or ["is its post-submit workflow delivered?"]), file=sys.stderr)
            bad += [f"{s.repo}: {x}: no post-submit result" for x in s.coverage.missing]
            bad += [f"{s.repo}: {x}: post-submit run ended without a verdict" for x in s.coverage.cancelled]
        for b in bad:
            print(f"::error::{b}", file=sys.stderr)
        return 1 if bad else 0
    return 0


def cmd_groups(args) -> int:
    from qqgarden.evidence import Evidence
    cfg = config.load(Path(args.config))
    backend = _backend(args, cfg)
    ev = Evidence(backend, {r.name: r for r in config.repos(cfg)}, Path(args.store) if args.store else None)
    found = [g for s in statuses(args, cfg, backend) for g in groups.group(s, ev)]
    if args.json:
        print(json.dumps([g.to_dict() for g in found], sort_keys=True, indent=2))
    else:
        for g in found:
            print(f"{g.key}  {g.kind}  builders={','.join(g.builders)}  suspects={len(g.suspects)}"
                  + (f"  tests={len(g.tests)}" if g.tests else ""))
        if not found:
            print("no red post-submit builders")
    return 0


def cmd_bisect(args) -> int:
    cfg = config.load(Path(args.config))
    verify = cfg["auto_revert"]["policy"]["require_culprit_verification"]
    repo_dir = Path(args.repo_dir)
    if git.run(["merge-base", "--is-ancestor", args.good, args.bad], cwd=repo_dir, check=False).returncode:
        raise GardenerError(f"{args.good} is not an ancestor of {args.bad}; nothing to bisect between them")
    out = git.run(["rev-list", "--first-parent", "--reverse", f"{args.good}..{args.bad}"], cwd=repo_dir).stdout
    suspects = out.split()
    if not suspects:
        raise GardenerError(f"{args.good}..{args.bad} has no first-parent commits")
    # Verify against the culprit's real first parent, which is `good` only when good is on the
    # first-parent line.
    good = git.run(["rev-parse", f"{suspects[0]}^1"], cwd=repo_dir).stdout.strip()
    probe = bisect.CommandProbe(repo_dir, args.run, args.timeout)
    res = bisect.bisect(suspects, good, probe, verify)
    if args.json:
        print(json.dumps(res.to_dict(), indent=2))
    else:
        for commit, answer in res.probes:
            print(f"probe {commit[:12]}: {answer}")
        if not res.culprit and probe.last_output:
            print("last probe output (tail):\n" + probe.last_output)
        print(res.reason)
    return 0 if res.culprit else 1


def _repos(cfg: dict, names: list[str] | None):
    repos = config.repos(cfg)
    if names:
        unknown = set(names) - {r.name for r in repos}
        if unknown:
            raise GardenerError(f"not onboarded in infra-config repos.toml: {', '.join(sorted(unknown))}")
        repos = [r for r in repos if r.name in names]
    return repos


def _report(outcomes, as_json: bool) -> None:
    if as_json:
        print(json.dumps([o.to_dict() for o in outcomes], sort_keys=True, indent=2))
        return
    if not outcomes:
        print("nothing to do: no red post-submit builders")
    for o in outcomes:
        print(f"{o.repo}: {o.step}: {o.group} ({o.kind})" + (f" culprit {o.culprit[:12]}" if o.culprit else "")
              + (f" -> {o.revert}" if o.revert else "") + f"\n    {o.reason}")


def cmd_cycle(args) -> int:
    from qqgarden import cycle
    from qqgarden.evidence import Evidence
    from qqgarden.ledger import Ledger
    from qqgarden.policy import Policy
    cfg = config.load(Path(args.config))
    policy = Policy.from_config(cfg)
    backend = _backend(args, cfg)
    repos = _repos(cfg, args.repo)
    ev = Evidence(backend, {r.name: r for r in config.repos(cfg)}, Path(args.store) if args.store else None)
    now = postsubmit.parse_time(args.now) if args.now else datetime.now(timezone.utc)
    outcomes = cycle.run(cfg, repos, backend, Ledger(Path(args.ledger), args.publish_ledger), policy, now,
                         timedelta(minutes=args.grace_minutes), args.limit, ev, args.dry_run)
    _report(outcomes, args.json)
    return 0


def cmd_revert(args) -> int:
    """Revert a culprit that bisection named (the gardener agent's path for longer ranges)."""
    from qqgarden import cycle
    from qqgarden.ledger import Ledger
    from qqgarden.policy import Policy
    cfg = config.load(Path(args.config))
    policy = Policy.from_config(cfg)
    if policy.require_culprit_verification and not args.verified:
        raise GardenerError("auto_revert.toml requires a verified culprit: pass --verified only after "
                            "`qqgarden bisect` reported it verified")
    backend = _backend(args, cfg)
    if not args.repo or len(args.repo) != 1:
        raise GardenerError("revert needs exactly one --repo")
    [repo] = _repos(cfg, args.repo)
    commits = {c.sha: c for c in backend.commits(repo, args.limit)}
    culprit = next((c for sha, c in commits.items() if sha.startswith(args.culprit)), None)
    if culprit is None:
        raise GardenerError(f"{args.culprit} is not among the last {args.limit} commits on {repo.default_branch}")
    g = groups.Group(repo=repo.name, first_bad=culprit.sha, last_good="", suspects=[culprit.sha],
                     builders=["bisected"], kind=args.kind)
    now = postsubmit.parse_time(args.now) if args.now else datetime.now(timezone.utc)
    o = cycle.revert_culprit(cfg, repo, g, culprit, backend, Ledger(Path(args.ledger), args.publish_ledger),
                             policy, now,
                             args.dry_run, verified=True)
    _report([o], args.json)
    return 0 if o.step in ("proposed", "reverted", "dry-run") else 1


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="qqgarden", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    def live(sp):
        sp.add_argument("--config", required=True, help="infra-config checkout at the pins.toml commit")
        sp.add_argument("--repo", action="append", help="limit to these onboarded repos")
        sp.add_argument("--backend", help="override pipelines.toml [defaults] backend (e.g. snapshot)")
        sp.add_argument("--snapshot", help="snapshot JSON for the snapshot backend")
        sp.add_argument("--cache", default=".qq/git", help="where the github backend keeps its clones")
        sp.add_argument("--limit", type=int, default=100, help="main commits to check, newest first")
        sp.add_argument("--grace-minutes", type=int, default=GRACE_MINUTES)
        sp.add_argument("--now", help="RFC 3339 time to evaluate at (tests and replays)")
        sp.add_argument("--json", action="store_true")

    s = sub.add_parser("status", help="tree status, red detection and post-submit coverage (V0-GAR-01)")
    live(s)
    s.add_argument("--out", help="write <repo>.json and README.md here")
    s.add_argument("--require-coverage", action="store_true",
                   help="exit 1 unless every main commit since onboarding has a post-submit result")
    s.set_defaults(func=cmd_status)

    g = sub.add_parser("groups", help="red builders grouped by regression range, with failure type (V0-GAR-02)")
    live(g)
    g.add_argument("--store", help="results store checkout (test-pipelines `results` branch) for failing tests")
    g.set_defaults(func=cmd_groups)

    b = sub.add_parser("bisect", help="bisect good..bad by running a command on each commit (V0-GAR-02)")
    b.add_argument("--config", required=True, help="infra-config checkout (culprit verification policy)")
    b.add_argument("--repo-dir", required=True, help="a clone of the repo holding both commits")
    b.add_argument("--good", required=True)
    b.add_argument("--bad", required=True)
    b.add_argument("--run", required=True, help="shell command; exit 0 pass, 125 can't tell, else fail")
    b.add_argument("--timeout", type=int, default=1800, help="seconds per probe; a timeout is can't tell")
    b.add_argument("--json", action="store_true")
    b.set_defaults(func=cmd_bisect)

    c = sub.add_parser("cycle", help="observe, group and revert verified culprits within the caps (V0-GAR-03)")
    live(c)
    c.add_argument("--ledger", required=True, help="the revert ledger (this repo's `ledger` branch)")
    c.add_argument("--publish-ledger", default="", metavar="BRANCH",
                   help="commit and push each ledger record to this branch before acting on it")
    c.add_argument("--store", help="results store checkout, for failing tests")
    c.add_argument("--forge-dir", help="snapshot backend: where the local forge keeps its PRs")
    c.add_argument("--dry-run", action="store_true", help="decide and report; create nothing")
    c.set_defaults(func=cmd_cycle)

    r = sub.add_parser("revert", help="revert one bisected culprit within the caps (V0-GAR-03)")
    live(r)
    r.add_argument("--ledger", required=True)
    r.add_argument("--publish-ledger", default="", metavar="BRANCH")
    r.add_argument("--culprit", required=True)
    r.add_argument("--kind", required=True, choices=["build", "test"])
    r.add_argument("--verified", action="store_true", help="bisection verified the culprit")
    r.add_argument("--forge-dir", help="snapshot backend: where the local forge keeps its PRs")
    r.add_argument("--dry-run", action="store_true")
    r.set_defaults(func=cmd_revert)

    args = p.parse_args(argv)
    try:
        return args.func(args)
    except GardenerError as e:
        print(f"qqgarden: {e}", file=sys.stderr)
        return 2
    except Exception as e:  # never let a crash look like exit 1, which means "coverage hole"
        print(f"qqgarden: internal error: {type(e).__name__}: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
