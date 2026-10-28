"""`golden` command line: one entry point for every gate decision.

Every gate prints a markdown verdict (also appended to the GitHub job summary),
emits annotations for findings, honours time-boxed waivers from golden.toml,
and exits non-zero only when the merge must be blocked.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import subprocess
import sys
import tempfile
from dataclasses import asdict
from pathlib import Path

from golden import (
    __version__,
    config,
    coverage,
    dora,
    flakescan,
    gitdiff,
    impact,
    local,
    mutation,
    optout,
    osvignores,
    plan,
    presence,
    presets,
    sarif,
)
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
    today = dt.datetime.now(dt.UTC).date()
    ignores = [
        f"- **{i.id}** until {i.until} ({i.file}): {i.reason}"
        + (" _(expired: enforced again)_" if i.until < today else "")
        for i in osvignores.check(Path(), today)
    ]
    summary(
        "\n".join(["### Golden policy", "", "| Key | Value | Floor |", "|---|---|---|", *rows])
        + ("\n\n**Waivers**\n\n" + "\n".join(waivers) if waivers else "")
        + ("\n\n**Not applied to this change**\n\n" + "\n".join(deferred) if deferred else "")
        + ("\n\n**Vulnerability exceptions**\n\n" + "\n".join(ignores) if ignores else "")
    )
    return 0


# ----------------------------------------------------------------- plan
def cmd_plan(args: argparse.Namespace) -> int:
    inputs = plan.inputs_from_env()
    event = plan.Event.from_env()
    repository, token = os.environ.get("GITHUB_REPOSITORY", ""), os.environ.get("GITHUB_TOKEN")
    opt_out = event.opt_out(
        str(inputs.get("skip-presence-label") or ""),
        repository,
        lambda path: optout.api_get(path, token) if repository else None,
    )
    if opt_out.note:
        annotate("notice" if opt_out.report_only else "warning", opt_out.note)
    outputs = plan.build(args.language, inputs, event, _policy(args), Path())
    outputs["presence-report-only"] = "true" if opt_out.report_only else "false"
    outputs["presence-opt-out"] = opt_out.note
    gating = plan.gating_changes(outputs["base"], args.policy)
    for path in gating:
        annotate("warning", plan.GATING_WARNING, file=path)
    outputs["gating-changes"] = " ".join(gating)
    if int(outputs["change-size"]) > plan.LARGE_CHANGE:
        annotate("warning", plan.large_change_warning(int(outputs["change-size"])))
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
        selected = plan.mutation_targets(preset, args.base, policy.exempt)
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
def _unmeasured(args: argparse.Namespace, policy: config.Policy) -> list[str]:
    """Changed source files that no coverage report mentions.

    diff-cover silently skips files missing from the report, so a new module no
    test imports (pytest-cov without `source`, Vitest without `coverage.include`)
    would otherwise pass as "100% covered".
    """
    if not args.preset:
        return []
    preset = presets.get(args.preset)
    measured = set().union(*(coverage.measured_files(r) for r in args.report))
    return [
        path
        for path in sorted(gitdiff.changed_lines(args.base))
        if preset.is_source(path)
        and not match_any(path, policy.exempt)
        and not coverage.is_measured(path, measured)
    ]


def cmd_diff_coverage(args: argparse.Namespace) -> int:
    policy = _policy(args)
    minimum = policy.get("coverage.diff_min")
    unmeasured = _unmeasured(args, policy)
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
    prefix = gitdiff.repo_prefix() if unmeasured else ""
    for path in unmeasured:
        annotate("error", "changed file is missing from the coverage report", file=prefix + path)
    lines = int(data.get("total_num_lines", 0))
    percent = float(data.get("total_percent_covered", 100))
    passed = (lines == 0 or percent >= minimum) and not unmeasured
    detail = f"**{percent:g}%** of {lines} changed coverable lines covered (minimum {minimum:g}%)."
    if unmeasured:
        detail += (
            "\n\nNot measured at all (no test executes them, or coverage is not configured to "
            "include them, e.g. `[tool.coverage.run] source` or Vitest `coverage.include`):\n\n"
            + "\n".join(f"- `{p}`" for p in unmeasured)
        )
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
        # Visible, not silent: an evicted cache entry (7 days unused) also lands
        # here, and a gate that quietly stops checking is worse than a red one.
        annotate(
            "warning",
            "coverage ratchet skipped: no default-branch baseline (first run, or the cache "
            "entry was evicted; the next push to the default branch saves one)",
        )
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
    lines = None
    if args.base:
        lines = (
            impact.mutation_lines(args.base, presets.get(args.preset), policy.exempt)
            if args.preset
            else gitdiff.changed_lines(args.base)
        )
    tally, lived = mutation.evaluate(args.report, args.format, lines, arid_filter=args.arid_filter)
    if tally.inconclusive:
        return verdict(
            "Mutation testing",
            passed=False,
            detail=f"{tally.invalid} mutants in the changed code got no verdict (compile errors "
            "or an interrupted run), so the run proves nothing." + tally.arid_note,
            waived=_waived(policy, "mutation"),
        )
    if tally.score is None:
        summary("#### ✅ Mutation testing: no mutants in the changed code" + tally.arid_note)
        return 0
    detail = (
        f"Score **{tally.score:g}%** (minimum {minimum:g}%): {tally.killed} killed, "
        f"{tally.timeout} timed out, {tally.survived} survived, {tally.no_coverage} not covered."
        + tally.arid_note
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
    detail += tally.survivors_note
    return verdict(
        "Mutation testing",
        passed=tally.score >= minimum and not tally.unreliable,
        detail=detail,
        waived=_waived(policy, "mutation"),
    )


# ----------------------------------------------------------------- sast
def cmd_sast(args: argparse.Namespace) -> int:
    policy = _policy(args)
    changed, outside = sarif.narrow(sarif.read(args.report), gitdiff.changed_lines(args.base))
    shown = changed[: sarif.MAX_LISTED]
    prefix = gitdiff.repo_prefix() if shown else ""
    for f in shown:
        annotate(
            "error" if f.blocks(args.min_level) else "warning",
            f"{f.rule}: {f.message}",
            file=prefix + f.file,
            line=f.line,
        )
    return verdict(
        "Static analysis",
        passed=not any(f.blocks(args.min_level) for f in changed),
        detail=sarif.detail(changed, outside, args.min_level),
        waived=_waived(policy, "sast"),
    )


# ----------------------------------------------------------------- scorecard
def cmd_scorecard(args: argparse.Namespace) -> int:
    policy = _policy(args)
    policy_path = Path(os.path.relpath(args.policy.resolve(), args.root.resolve())).as_posix()
    repo = sc.Repo(args.root, policy, sc.rules_from_env(), policy_path, sc.tag_rulesets_from_env())
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


# ----------------------------------------------------------------- flaky-scan
def cmd_flaky_scan(args: argparse.Namespace) -> int:
    if args.print_command:
        print(flakescan.REPORT_COMMANDS[args.print_command])
        return 0
    result = flakescan.scan(args.report, args.format)
    for level, message in flakescan.findings(result):
        annotate(level, message)
    if args.json:
        args.json.write_text(json.dumps(flakescan.as_dict(result), indent=2) + "\n")
    summary(flakescan.render(result))
    return 0 if result.clean else 1


# ----------------------------------------------------------------- local
def cmd_local(args: argparse.Namespace) -> int:
    inputs = plan.inputs_from_env()
    for pair in args.set:
        key, sep, value = pair.partition("=")
        if not sep:
            raise ValueError(f"--set expects KEY=VALUE, got {pair!r}")
        inputs[key] = value
    base = args.base if args.base is not None else local.default_base()
    opts = local.Options(base, args.policy.resolve(), fast=args.fast, mutation=args.mutation)
    # Judged as CI will judge it: by the policy at the merge-base, tightened by this change.
    plan_steps = local.steps(args.language, inputs, config.load(args.policy, base=base), opts)
    if args.dry_run:
        for step in plan_steps:
            env = " ".join(f"{k}={v!r}" for k, v in step.env.items())
            print(f"{step.name}: {env + ' ' if env else ''}{' '.join(step.argv)}")
        return 0
    dirty = bool(base) and gitdiff.uncommitted()
    if dirty:
        annotate("warning", local.DIRTY)
    outcomes = local.run(plan_steps)
    print("\n" + local.render(outcomes, base, dirty=dirty))
    return 0 if all(o.passed for o in outcomes) else 1


def _add_local_parser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    p = sub.add_parser("local", help="run the pipeline's checks and gates before pushing")
    p.add_argument("--language", required=True, choices=sorted(plan.DEFAULTS))
    p.add_argument("--base", help="diff base (default: merge-base with the default branch)")
    p.add_argument(
        "--set",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="override a workflow input, e.g. test-command='make test'",
    )
    p.add_argument("--fast", action="store_true", help="policy, format, lint, types only")
    p.add_argument("--mutation", action="store_true", help="also mutate the changed code")
    p.add_argument("--dry-run", action="store_true", help="print the steps, run nothing")
    p.set_defaults(func=cmd_local)


def _add_mutation_parser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    p = sub.add_parser("mutation", help="gate: mutation score meets policy")
    p.add_argument("report", type=Path)
    p.add_argument("--format", choices=sorted(mutation.PARSERS), required=True)
    p.add_argument("--base", help="only count mutants on lines changed since BASE")
    p.add_argument(
        "--preset",
        choices=sorted(presets.PRESETS),
        help="with --base, also count mutants in source functions the changed tests touch",
    )
    p.add_argument(
        "--no-arid-filter",
        dest="arid_filter",
        action="store_false",
        help="score mutants on logging/debug-only lines too (stryker and gremlins reports; "
        "for debugging the filter)",
    )
    p.set_defaults(func=cmd_mutation)


def _add_flaky_scan_parser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    p = sub.add_parser("flaky-scan", help="find flaky tests in the reports of repeated runs")
    p.add_argument("report", type=Path, nargs="*", help="one test report per run, same commit")
    p.add_argument("--format", choices=["auto", *flakescan.PARSERS], default="auto")
    p.add_argument("--json", type=Path, help="write the results here as JSON")
    p.add_argument(
        "--print-command",
        choices=sorted(flakescan.REPORT_COMMANDS),
        metavar="LANGUAGE",
        help="print the default command that writes one report to $GOLDEN_REPORT, then exit",
    )
    p.set_defaults(func=cmd_flaky_scan)


def _add_sast_parser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    p = sub.add_parser("sast", help="gate: no new static-analysis findings on changed lines")
    p.add_argument("report", type=Path, nargs="+", help="SARIF 2.1.0 report(s)")
    p.add_argument("--base", required=True)
    p.add_argument(
        "--min-level",
        choices=sarif.THRESHOLDS,
        default="error",
        help="lowest SARIF level that blocks the merge (default: error)",
    )
    p.set_defaults(func=cmd_sast)


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
    p.add_argument(
        "--preset",
        choices=sorted(presets.PRESETS),
        help="also fail when a changed source file is missing from the report",
    )
    p.add_argument("report", type=Path, nargs="+")
    p.set_defaults(func=cmd_diff_coverage)

    p = sub.add_parser("ratchet", help="gate: total coverage does not regress")
    p.add_argument("report", type=Path)
    p.add_argument("--format", choices=["auto", *coverage.PARSERS], default="auto")
    p.add_argument("--baseline", type=Path)
    p.add_argument("--write-baseline", type=Path)
    p.add_argument("--sha", default="")
    p.set_defaults(func=cmd_ratchet)

    _add_mutation_parser(sub)
    _add_sast_parser(sub)

    p = sub.add_parser("scorecard", help="gate: repository maturity level")
    p.add_argument("--root", type=Path, default=Path())
    p.add_argument("--json", type=Path)
    p.set_defaults(func=cmd_scorecard)

    _add_local_parser(sub)

    _add_flaky_scan_parser(sub)

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
