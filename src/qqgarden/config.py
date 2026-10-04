"""Read infra-config through its own loader and validator (qqcfg), from a checkout at a pinned commit.

The gardener does not parse config itself: it imports `tools/qqcfg.py` from the checkout, refuses to
act on a config that fails `qqcfg validate`, and then reads `qqcfg.load`. Every cap comes from here;
nothing in this repo carries a default for one.
"""
from __future__ import annotations

import importlib.util
import sys
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType

from qqgarden.errors import GardenerError


def qqcfg_module(root: Path) -> ModuleType:
    path = Path(root) / "tools" / "qqcfg.py"
    if not path.is_file():
        raise GardenerError(f"{root} is not an infra-config checkout: {path} is missing")
    name = f"qqcfg_{abs(hash(str(path.resolve())))}"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise GardenerError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module  # dataclasses and friends look modules up by name
    try:
        spec.loader.exec_module(module)
    except Exception as e:
        del sys.modules[name]
        raise GardenerError(f"{path} failed to load: {type(e).__name__}: {e}") from None
    return module


def load(root: Path, validate: bool = True) -> dict:
    """Return infra-config as qqcfg.load returns it, after qqcfg validate passes."""
    qqcfg = qqcfg_module(root)
    if validate:
        errors, _ = qqcfg.validate(Path(root))
        if errors:
            raise GardenerError("infra-config fails qqcfg validate, so the gardener refuses to use it:\n  "
                                + "\n  ".join(errors))
    try:
        return qqcfg.load(Path(root))
    except qqcfg.ConfigError as e:
        raise GardenerError(f"infra-config: {e}") from None


@dataclass(frozen=True)
class Repo:
    name: str              # the infra-config name, e.g. "xo-space"
    source: str            # "github.com/<owner>/<repo>"
    default_branch: str
    postsubmit: tuple[str, ...]   # post-submit builder names; each must report on every commit
    timeout_minutes: int          # the longest post-submit builder timeout

    @property
    def slug(self) -> str:
        """"<owner>/<repo>" for a github.com source."""
        host, _, slug = self.source.partition("/")
        return slug if host == "github.com" else ""


def repos(cfg: dict) -> list[Repo]:
    """Onboarded repos with the post-submit builders that run on every commit landing on main."""
    builders = cfg.get("pipelines", {}).get("builder", [])
    default_timeout = cfg.get("pipelines", {}).get("defaults", {}).get("timeout_minutes")
    out = []
    for r in cfg.get("repos", {}).get("repo", []):
        post = [b for b in builders
                if b.get("repo") == r["name"] and b.get("pipeline") == "postsubmit"
                and "land" in b.get("triggers", [])]
        timeouts = [b.get("timeout_minutes", default_timeout) for b in post]
        out.append(Repo(
            name=r["name"],
            source=r["source"],
            default_branch=r.get("default_branch", "main"),
            postsubmit=tuple(b["name"] for b in post),
            timeout_minutes=max((t for t in timeouts if t), default=0),
        ))
    return out


def cancellable(cfg: dict) -> list[str]:
    """Post-submit builders that config lets a newer commit cancel. Each one breaks GAR-01's
    promise that every main commit gets a verdict, so culprits stay findable."""
    return [b["name"] for b in cfg.get("pipelines", {}).get("builder", [])
            if b.get("pipeline") == "postsubmit" and b.get("cancel_in_progress", True) is not False]
