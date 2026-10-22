"""Invariants the gates' safety rests on, checked for generated inputs, not examples.

Each property is a promise the pipeline makes to the people it gates:

- a change never grades itself: the policy it is judged by is never looser than the base;
- no workflow input can smuggle an extra job output or close a multi-line value early;
- no file name or message can break out of a workflow-command annotation;
- the changed lines git reports are exactly what changed, in HEAD's numbering.

Run more examples with `HYPOTHESIS_PROFILE=ci uv run pytest tests/test_properties.py`.
"""

import contextlib
import datetime as dt
import importlib.util
import io
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from golden import config, gitdiff, plan, report

settings.register_profile("ci", max_examples=2000, deadline=None)
settings.load_profile(os.environ.get("HYPOTHESIS_PROFILE", "default"))

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("properties_runner", ROOT / "evals" / "runner.py")
runner = importlib.util.module_from_spec(_spec)
sys.modules["properties_runner"] = runner
_spec.loader.exec_module(runner)

TODAY = dt.date(2026, 10, 22)


def _values(bound: config.Bound) -> st.SearchStrategy[float]:
    low, high = sorted((bound.floor, bound.strictest))
    if bound.integer:
        return st.integers(int(low), int(high)).map(float)
    return st.floats(low, high, allow_nan=False, allow_infinity=False)


_waivers = st.lists(st.sampled_from(config.GATES), unique=True).flatmap(
    lambda gates: st.tuples(*(st.integers(0, config.MAX_WAIVER_DAYS) for _ in gates)).map(
        lambda days: {
            g: config.Waiver(g, "reason", TODAY + dt.timedelta(days=d))
            for g, d in zip(gates, days, strict=True)
        }
    )
)
policies = st.builds(
    config.Policy,
    st.fixed_dictionaries({k: _values(b) for k, b in config.BOUNDS.items()}),
    st.lists(st.sampled_from(["docs/**", "gen/**", "scripts/*.sh", "vendor/**"]), unique=True).map(
        tuple
    ),
    _waivers,
)


@given(policies, policies)
def test_merge_never_loosens_the_base_and_announces_what_it_defers(base, head):
    merged = config.merge(base, head)
    for key, bound in config.BOUNDS.items():
        value = merged.values[key]
        assert bound.stricter(value, base.values[key]) == value
        assert bound.stricter(value, head.values[key]) == value
        assert value in (base.values[key], head.values[key])
        bound.check(key, value)
        if value != head.values[key]:
            assert any(note.startswith(f"{key} = ") for note in merged.deferred)
    assert set(merged.exempt) <= set(base.exempt) & set(head.exempt)
    for exempt in set(head.exempt) - set(merged.exempt):
        assert f"presence.exempt {exempt!r} applies after merge" in merged.deferred
    assert set(merged.waivers) <= set(base.waivers) & set(head.waivers)
    for gate, waiver in merged.waivers.items():
        assert waiver.expires <= min(base.waivers[gate].expires, head.waivers[gate].expires)
    for gate in set(head.waivers) - set(base.waivers):
        assert f"waiver for {gate} applies after merge" in merged.deferred


@given(policies)
def test_merging_a_policy_with_itself_changes_nothing(policy):
    merged = config.merge(policy, policy)
    assert (merged.values, merged.exempt, merged.waivers) == (
        policy.values,
        policy.exempt,
        policy.waivers,
    )
    assert merged.deferred == ()


def _github_lines(text: str) -> list[str]:
    # The runner reads $GITHUB_OUTPUT line by line; a line may end in \n, \r\n or \r.
    return re.split(r"\r\n|\r|\n", text)


keys = st.from_regex(r"[a-z][a-z-]{0,20}", fullmatch=True)


@given(st.dictionaries(keys, st.text()))
def test_job_outputs_round_trip_and_no_value_adds_a_key(outputs):
    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp) / "out"
        plan.write_outputs(outputs, target)
        with target.open(encoding="utf-8", newline="") as fh:
            text = fh.read()
    # Read as written, every value comes back byte for byte.
    assert runner.parse_outputs(text) == outputs
    # A reader that also breaks lines at a lone \r may change such a value, but never its key:
    # every line is a key it was given, a line inside a delimited block, or the block's own
    # closing delimiter, so no value can add or override a key.
    lines = iter(_github_lines(text))
    seen = []
    for line in lines:
        if not line:
            continue
        name, sep, _ = line.partition("=")
        if "<<" in name:
            name, delimiter = line.split("<<", 1)
            assert re.fullmatch(r"GOLDEN_[0-9a-f]{32}", delimiter)
            assert delimiter not in outputs[name]
            for body in lines:
                if body == delimiter:
                    break
        else:
            assert sep
        seen.append(name)
    assert seen == list(outputs)


def _unescape(value: str) -> str:
    return re.sub(r"%(25|0D|0A|3A|2C)", lambda m: bytes.fromhex(m[1]).decode(), value)


@given(st.text(min_size=1), st.text(), st.integers(1, 10**6))
def test_annotations_stay_one_command_and_carry_the_exact_text(file, message, line):
    with contextlib.redirect_stdout(buffer := io.StringIO()):
        report.annotate("error", message, file=file, line=line)
    out = buffer.getvalue()
    assert out.endswith("\n")
    command = out[:-1]
    assert "\n" not in command
    assert "\r" not in command
    match = re.fullmatch(r"::error file=([^,:]*),line=(\d+)::(.*)", command, re.DOTALL)
    assert match
    assert _unescape(match[1]) == file
    assert int(match[2]) == line
    assert _unescape(match[3]) == message


lines_of_text = st.lists(
    st.sampled_from(["a", "b", "c", "def f():", "    return 1", ""]), max_size=12
)


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


@settings(max_examples=40, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(lines_of_text, lines_of_text)
def test_changed_lines_are_exactly_the_lines_head_did_not_keep(before, after):
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _git(root, "init", "-q", "-b", "main")
        _git(root, "config", "user.email", "t@example.com")
        _git(root, "config", "user.name", "t")
        _git(root, "config", "commit.gpgsign", "false")
        (root / "f.py").write_text("".join(f"{x}\n" for x in before))
        _git(root, "add", "-A")
        _git(root, "commit", "-q", "--allow-empty", "-m", "base")
        base = _git(root, "rev-parse", "HEAD")
        (root / "f.py").write_text("".join(f"{x}\n" for x in after))
        _git(root, "add", "-A")
        _git(root, "commit", "-q", "--allow-empty", "-m", "head")
        ranges = gitdiff.changed_lines(base, cwd=root).get("f.py", [])
    changed = {n for lo, hi in ranges for n in range(lo, hi + 1)}
    assert changed <= set(range(1, len(after) + 1))
    kept = [x for n, x in enumerate(after, 1) if n not in changed]
    # What git did not report as changed is a subsequence of the base, in order.
    remaining = iter(before)
    assert all(line in remaining for line in kept)
    # A line the base never had is always reported.
    for n, x in enumerate(after, 1):
        if x not in before:
            assert n in changed
    # Deleting lines changes no line of HEAD; an unchanged file reports nothing.
    if before == after:
        assert not changed
