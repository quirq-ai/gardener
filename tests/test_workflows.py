"""The tree-status workflow holds write tokens: what runs next to them stays pinned (audit S5, S6)."""
import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = (ROOT / ".github/workflows/tree-status.yml").read_text()


def steps_using(text: str, needle: str) -> list[str]:
    return [s for s in re.split(r"\n      - ", text) if needle in s]


def test_no_checkout_keeps_credentials():
    checkouts = steps_using(WORKFLOW, "actions/checkout@")
    assert checkouts and all("persist-credentials: false" in s for s in checkouts)


def test_only_hash_checked_wheels_are_installed():
    installs = [line for line in WORKFLOW.splitlines() if "pip install" in line]
    assert installs
    for line in installs:
        assert "--require-hashes" in line and "--no-deps" in line and "--only-binary :all:" in line, line


def test_the_lock_pins_what_pyproject_pins():
    deps = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["dependencies"]
    lock = (ROOT / "requirements/runtime.lock").read_text()
    for d in deps:
        if "==" in d:
            assert re.search(rf"^{re.escape(d)} \\$", lock, re.M), d
    blocks = [b for b in re.split(r"\n(?=\S)", lock) if not b.startswith("#")]
    assert blocks and all("--hash=sha256:" in b for b in blocks)


def test_qqresults_runs_at_the_pinned_commit():
    pins = tomllib.loads((ROOT / "pins.toml").read_text())
    deps = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["dependencies"]
    assert any(d.endswith("@" + pins["test-pipelines"]["commit"]) for d in deps)
    assert "repository: quirq-ai/test-pipelines" in WORKFLOW and "steps.pin.outputs.results" in WORKFLOW


def test_tokens_reach_only_the_steps_that_push():
    names = [re.match(r"name: (.*)", s).group(1) for s in steps_using(WORKFLOW, "${{ github.token }}")]
    assert names == ["compute the tree status", "publish changes", "open the ledger branch",
                     "garden (group, verify, revert within caps)"]


def test_an_empty_ledger_needs_a_person():
    [step] = steps_using(WORKFLOW, "name: open the ledger branch")
    assert "--orphan" in step and '[ "$BOOTSTRAP" = "true" ]' in step
    assert "inputs.bootstrap-ledger" in step and "github.event_name == 'workflow_dispatch'" in step
    # a bootstrap really creates the branch; without the App the cycle reports on a scratch ledger
    assert "commit -q --allow-empty" in step and "push origin HEAD:refs/heads/ledger" in step
    assert '"$HAS_BOT" != "true"' in step and "publish=" in step
