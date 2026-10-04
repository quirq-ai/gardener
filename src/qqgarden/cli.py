"""qqgarden: the gardener's command line.

    qqgarden status --config <infra-config checkout> [--repo NAME]... [--out DIR] [--require-coverage]
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from qqgarden import backends, config, postsubmit
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
    return backends.load(name, path=args.snapshot, cache=args.cache)


def statuses(args) -> list[TreeStatus]:
    cfg = config.load(Path(args.config))
    repos = config.repos(cfg)
    if args.repo:
        unknown = set(args.repo) - {r.name for r in repos}
        if unknown:
            raise GardenerError(f"not onboarded in infra-config repos.toml: {', '.join(sorted(unknown))}")
        repos = [r for r in repos if r.name in args.repo]
    cancellable = set(config.cancellable(cfg))
    backend = _backend(args, cfg)
    now = postsubmit.parse_time(args.now) if args.now else datetime.now(timezone.utc)
    out = []
    for repo in repos:
        notes = [f"{b}: config lets a newer commit cancel it (set cancel_in_progress = false)"
                 for b in repo.postsubmit if f"{repo.name}/{b}" in cancellable]
        commits = backend.commits(repo, args.limit)
        runs = []
        for b in repo.postsubmit:
            r, note = backend.runs(repo, b)
            runs.extend(r)
            if note:
                notes.append(note)
        out.append(postsubmit.tree_status(repo.name, repo.default_branch, repo.postsubmit, commits,
                                          runs, now, timedelta(minutes=args.grace_minutes), notes))
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


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="qqgarden", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("status", help="tree status, red detection and post-submit coverage (V0-GAR-01)")
    s.add_argument("--config", required=True, help="infra-config checkout at the pins.toml commit")
    s.add_argument("--repo", action="append", help="limit to these onboarded repos")
    s.add_argument("--backend", help="override pipelines.toml [defaults] backend (e.g. snapshot)")
    s.add_argument("--snapshot", help="snapshot JSON for the snapshot backend")
    s.add_argument("--cache", default=".qq/git", help="where the github backend keeps its clones")
    s.add_argument("--limit", type=int, default=100, help="main commits to check, newest first")
    s.add_argument("--grace-minutes", type=int, default=GRACE_MINUTES)
    s.add_argument("--now", help="RFC 3339 time to evaluate at (tests and replays)")
    s.add_argument("--out", help="write <repo>.json and README.md here")
    s.add_argument("--json", action="store_true")
    s.add_argument("--require-coverage", action="store_true",
                   help="exit 1 unless every main commit since onboarding has a post-submit result")
    s.set_defaults(func=cmd_status)

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
