"""`golden` command line: one entry point for every gate decision.

Every gate prints a markdown verdict (also appended to the GitHub job summary),
emits annotations for findings, honours time-boxed waivers from golden.toml,
and exits non-zero only when the merge must be blocked.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from dataclasses import asdict
from pathlib import Path

from golden import __version__, config, coverage, dora, gitdiff, mutation, plan, presence, presets
from golden import scorecard as sc
from golden.globmatch import match_any
from golden.report import annotate, summary, verdict


def _policy(args: argparse.Namespace) -> config.Policy:
    """The policy this run is judged by, anchored to the change's base commit.

    The base comes from `--base` or, inside Actions, from the triggering event, so
    a pull request is always judged by the merged golden.toml, never its own edit.
    """
    base = getattr(args, "base", None) or plan.Event.from_env().base()
    return config.load(args.policy, base=base)


def _waived(policy: config.Policy, gate: str) -> bool:
    waiver = policy.waiver(gate)
    if waiver:
        annotate("warning", f"{gate} is waived until {waiver.expires}: {waiver.reason}")
    return waiver is not None


# ----------------------------------------------------------------- policy
def cmd_policy(args: argparse.Namespace) -> int:
    policy = _policy(args)
    if args.action == "get":
        value = policy.get(args.key)
        print(int(value) if value.is_integer() else value)
        return 0
    rows = [f"| `{k}` | {v:g} | {config.BOUNDS[k].floor:g} |" for k, v in policy.values.items()]
    waivers = [f"- **{w.gate}** until {w.expires}: {w.reason}" for w in policy.waivers.values()]
    deferred = [f"- {note}" for note in policy.deferred]
    for note in policy.deferred:
        annotate("notice", f"golden.toml: {note}")
    summary(
        "\n".join(["### Golden policy", "", "| Key | Value | Floor |", "|---|---|---|", *rows])
        + ("\n\n**Waivers**\n\n" + "\n".join(waivers) if waivers else "")
        + ("\n\n**Not applied to this change**\n\n" + "\n".join(deferred) if deferred else "")
    )
    return 0


# ----------------------------------------------------------------- plan
def cmd_plan(args: argparse.Namespace) -> int:
    inputs = json.loads(os.environ.get("GOLDEN_INPUTS") or "{}")
    outputs = plan.build(args.language, inputs, plan.Event.from_env(), _policy(args), Path())
    target = os.environ.get("GITHUB_OUTPUT")
    if target:
        plan.write_outputs(outputs, Path(target))
        summary(plan.render(args.language, outputs))
    else:
        print(json.dumps(outputs, indent=2))
    return 0


# ----------------------------------------------------------------- changed
def cmd_changed(args: argparse.Namespace) -> int:
    preset = presets.get(args.preset)
    policy = _policy(args)
    changed = gitdiff.changed_files(args.base)
    sources = [p for p in changed if preset.is_source(p) and not match_any(p, policy.exempt)]
    if args.kind == "tests":
        selected = [p for p in changed if preset.is_runnable_test(p)]
    elif args.kind == "sources":
        selected = sources
    else:
        lines = gitdiff.changed_lines(args.base)
        selected = sorted(
            {
                target
                for p in sources
                if p in lines
                for target in preset.mutation_targets(p, lines[p], gitdiff.read_head(p))
            }
        )
    print({"space": " ", "newline": "\n", "comma": ","}[args.sep].join(selected))
    return 0


# ----------------------------------------------------------------- presence
def cmd_presence(args: argparse.Namespace) -> int:
    policy = _policy(args)
    preset = presets.get(args.preset)
    findings = presence.evaluate(
        gitdiff.changed_files(args.base), preset, gitdiff.read_head, policy.exempt
    )
    prefix = gitdiff.repo_prefix() if findings else ""
    for f in findings:
        annotate(
            "error" if not args.report_only else "warning",
            f"missing tests: {f.reason}",
            file=prefix + f.source,
        )
    detail = (
        "Every changed source file has a changed test that references it."
        if not findings
        else "\n".join(f"- `{f.source}`: {f.reason}" for f in findings)
    )
    if args.report_only and findings:
        detail += "\n\n_Report-only: the PR carries the opt-out label; reviewers must confirm._"
    return verdict(
        "Test presence",
        passed=not findings,
        detail=detail,
        waived=args.report_only or _waived(policy, "test-presence"),
    )


# ----------------------------------------------------------------- diff coverage
def cmd_diff_coverage(args: argparse.Namespace) -> int:
    policy = _policy(args)
    minimum = policy.get("coverage.diff_min")
    with tempfile.TemporaryDirectory() as tmp:
        out_json, out_md = Path(tmp, "diff.json"), Path(tmp, "diff.md")
        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "diff_cover.diff_cover_tool",
                *map(str, args.report),
                f"--compare-branch={args.base}",
                "--ignore-staged",
                "--ignore-unstaged",
                f"--format=json:{out_json},markdown:{out_md}",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0 or not out_json.exists():
            print(proc.stdout + proc.stderr, file=sys.stderr)
            return verdict("Diff coverage", passed=False, detail="diff-cover failed to run")
        data = json.loads(out_json.read_text(encoding="utf-8"))
        markdown = out_md.read_text(encoding="utf-8")

    for path, stats in data.get("src_stats", {}).items():
        for line in stats.get("violation_lines", [])[:50]:
            annotate("warning", "changed line not covered by tests", file=path, line=int(line))
    lines = int(data.get("total_num_lines", 0))
    percent = float(data.get("total_percent_covered", 100))
    passed = lines == 0 or percent >= minimum
    detail = f"**{percent:g}%** of {lines} changed coverable lines covered (minimum {minimum:g}%)."
    return verdict(
        "Diff coverage",
        passed=passed,
        detail=detail + "\n\n<details><summary>Report</summary>\n\n" + markdown + "\n</details>",
        waived=_waived(policy, "diff-coverage"),
    )


# ----------------------------------------------------------------- ratchet
def cmd_ratchet(args: argparse.Namespace) -> int:
    policy = _policy(args)
    current = coverage.read_totals(args.report, args.format).percent
    if args.write_baseline:
        coverage.write_baseline(args.write_baseline, current, args.sha)
        summary(f"#### ✅ Coverage ratchet: baseline saved ({current:g}%)")
        return 0
    baseline = coverage.read_baseline(args.baseline) if args.baseline else None
    if baseline is None:
        summary(f"#### ⏭️ Coverage ratchet: no baseline yet (current {current:g}%)")
        return 0
    ok, delta = coverage.ratchet(current, baseline, policy.get("coverage.ratchet_tolerance"))
    return verdict(
        "Coverage ratchet",
        passed=ok,
        detail=f"Total coverage {current:g}% vs default branch {baseline:g}% (Δ {delta:+g} pp, "
        f"tolerance {policy.get('coverage.ratchet_tolerance'):g} pp).",
        waived=_waived(policy, "coverage-ratchet"),
    )


# ----------------------------------------------------------------- mutation
def cmd_mutation(args: argparse.Namespace) -> int:
    policy = _policy(args)
    minimum = policy.get("mutation.min_score")
    lines = gitdiff.changed_lines(args.base) if args.base else None
    tally, lived = mutation.evaluate(args.report, args.format, lines)
    if tally.score is None:
        summary("#### ✅ Mutation testing: no mutants in the changed code")
        return 0
    detail = (
        f"Score **{tally.score:g}%** (minimum {minimum:g}%): {tally.killed} killed, "
        f"{tally.timeout} timed out, {tally.survived} survived, {tally.no_coverage} not covered."
    )
    if tally.unreliable:
        detail += (
            "\n\nMost mutants timed out, so the run proves nothing: raise the mutation "
            "tool's timeout (e.g. gremlins `--timeout-coefficient`)."
        )
    prefix = gitdiff.repo_prefix() if lived else ""
    for m in lived[:50]:
        annotate(
            "warning",
            f"surviving mutant ({m.mutator}): no test noticed this change",
            file=prefix + m.file,
            line=m.line,
        )
    if lived:
        detail += "\n\nSurviving mutants (tests did not notice these bugs):\n\n" + "\n".join(
            f"- `{m.file}:{m.line}` {m.mutator}" for m in lived[:50]
        )
    return verdict(
        "Mutation testing",
        passed=tally.score >= minimum and not tally.unreliable,
        detail=detail,
        waived=_waived(policy, "mutation"),
    )


# ----------------------------------------------------------------- scorecard
def cmd_scorecard(args: argparse.Namespace) -> int:
    policy = _policy(args)
    policy_path = Path(os.path.relpath(args.policy.resolve(), args.root.resolve())).as_posix()
    repo = sc.Repo(args.root, policy, sc.rules_from_env(), policy_path)
    results = sc.evaluate(repo)
    reached = sc.level(results)
    if args.json:
        args.json.write_text(
            json.dumps({"level": reached, "controls": [asdict(r) for r in results]}, indent=2)
        )
    minimum = int(policy.get("scorecard.min_level"))
    summary(sc.render(results))
    return verdict(
        "Scorecard",
        passed=reached >= minimum,
        detail=f"Level {reached} (minimum {minimum}).",
        waived=_waived(policy, "scorecard"),
    )


# ----------------------------------------------------------------- dora
def cmd_dora(args: argparse.Namespace) -> int:
    result = dora.compute(dora.parse_log(args.log.read_text(encoding="utf-8")))
    print(json.dumps(asdict(result), indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="golden", description=__doc__)
    parser.add_argument("--version", action="version", version=f"golden {__version__}")
    parser.add_argument("--policy", type=Path, default=Path("golden.toml"))
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("policy", help="validate golden.toml or read a value")
    p.add_argument("action", choices=["check", "get"])
    p.add_argument("key", nargs="?", choices=sorted(config.BOUNDS))
    p.set_defaults(func=cmd_policy)

    p = sub.add_parser("plan", help="resolve commands, diff base and changed files")
    p.add_argument("--language", required=True, choices=sorted(plan.DEFAULTS))
    p.set_defaults(func=cmd_plan)

    p = sub.add_parser("changed", help="list changed sources, tests or mutation targets")
    p.add_argument("--base", required=True)
    p.add_argument("--preset", required=True, choices=sorted(presets.PRESETS))
    p.add_argument("--kind", choices=["sources", "tests", "mutation-targets"], required=True)
    p.add_argument("--sep", choices=["space", "newline", "comma"], default="space")
    p.set_defaults(func=cmd_changed)

    p = sub.add_parser("presence", help="gate: changed sources ship with tests")
    p.add_argument("--base", required=True)
    p.add_argument("--preset", required=True, choices=sorted(presets.PRESETS))
    p.add_argument("--report-only", action="store_true")
    p.set_defaults(func=cmd_presence)

    p = sub.add_parser("diff-coverage", help="gate: changed lines are covered")
    p.add_argument("--base", required=True)
    p.add_argument("report", type=Path, nargs="+")
    p.set_defaults(func=cmd_diff_coverage)

    p = sub.add_parser("ratchet", help="gate: total coverage does not regress")
    p.add_argument("report", type=Path)
    p.add_argument("--format", choices=["auto", *coverage.PARSERS], default="auto")
    p.add_argument("--baseline", type=Path)
    p.add_argument("--write-baseline", type=Path)
    p.add_argument("--sha", default="")
    p.set_defaults(func=cmd_ratchet)

    p = sub.add_parser("mutation", help="gate: mutation score meets policy")
    p.add_argument("report", type=Path)
    p.add_argument("--format", choices=sorted(mutation.PARSERS), required=True)
    p.add_argument("--base", help="only count mutants on lines changed since BASE")
    p.set_defaults(func=cmd_mutation)

    p = sub.add_parser("scorecard", help="gate: repository maturity level")
    p.add_argument("--root", type=Path, default=Path())
    p.add_argument("--json", type=Path)
    p.set_defaults(func=cmd_scorecard)

    p = sub.add_parser("dora", help="compute DORA metrics from a deployment log")
    p.add_argument("log", type=Path)
    p.set_defaults(func=cmd_dora)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "policy" and args.action == "get" and not args.key:
        build_parser().error("policy get needs a KEY")
    try:
        code: int = args.func(args)
    except (config.PolicyError, gitdiff.GitError, ValueError, OSError) as exc:
        annotate("error", f"golden {args.command}: {exc}")
        return 2
    return code
