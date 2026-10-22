# Real-fault benchmark

**Question.** For real bugs that shipped in real open-source projects, would the Golden Pipeline's
gates have flagged the inadequate tests at the exact code where the bug lived, before it shipped?
And would coverage alone have missed it?

This follows Just et al. ("Are mutants a valid substitute for real faults", FSE 2014) and Google's
study of mutants coupled to real bugs (ICSE 2021), at small scale. The repository's other evidence
is synthetic ([drills](../../docs/evaluation.md#3-drills-does-each-gate-catch-what-it-claims), the
[decision benchmark](../README.md)); this is the only part that uses bugs nobody planted.

**Read this first.** The sample is small, it was cut short for time, and the bug-introducing
changes are not reconstructed (see [Limits](#limits)). Treat the numbers as an existence proof with
an honest denominator, not as a rate.

## Totals

Bugs examined: 77 candidate fix commits from 5 projects. Kept after screening: 14. Run end to end: **10**. Not run (time budget): 4. Tool errors: 0.

| Metric (over the 10 bugs run) | Result |
|---|---|
| Faulty lines already executed by the suite of the day (coverage blind spot) | 7/10 (70%) |
| Diff-coverage gate (90%) would have passed the faulty lines | 7/10 (70%) |
| Mutation gate (80%) blocks on the buggy functions | 4/10 (40%) |
| ... same, ignoring mutants that only change a docstring | 4/10 (40%) |
| A surviving mutant sits on a faulty line | 6/10 (60%) |
| ... same, ignoring docstring mutants | 6/10 (60%) |
| The fix itself is a surviving mutant (the tool generated the exact fix and no test noticed) | 1/10 (10%) |
| **Coverage-only pipeline flags the faulty code** | 3/10 (30%) |
| **Full pipeline (diff coverage + mutation) flags it** | 4/10 (40%) |
| Mutation score higher at F than at P (regression test raised it) | 5/10 (50%) (equal 1, lower 4) |
| Mutation gate still blocks at F, with the regression test | 3/10 (30%) |
| Median mutation score | 84.9% at P, 83.1% at F |
| Coverage blind AND mutation gate blocks | 1/10 (10%) |
| Coverage blind AND mutation gate passes (missed by both) | 6/10 (60%) |

By fault kind: 8 bugs where the fix rewrites existing lines (`modified`) and 2 where the fix only adds code (`omission`: a missing guard or branch). For omissions the faulty lines are the statements next to the insertion point, so coverage of them says little.

- modified: coverage blind 6/8, mutation gate blocks 3/8, survivor on faulty line 5/8
- omission: coverage blind 1/2, mutation gate blocks 1/2, survivor on faulty line 1/2

## What the numbers say, and do not say

- **Coverage alone missed 7 of 10.** In seven bugs every faulty line had already been executed by
  the suite of the day. The three coverage did flag (boltons `IndexedSet.update`,
  `_ComplementSet.__le__/__ge__` and the guard next to `_calc_pearson_type`) are lines no test
  ran, which is the easy case. Line coverage cannot see a missing guard at all.
- **The mutation gate blocked 4 of 10**: those three, plus boltons `Bits.__init__`, where the
  suite executes the comparison but never pins its boundary, and the fix (`>` to `>=`) is itself
  one of the surviving mutants. In this sample the mutation gate therefore added **one** catch to
  diff coverage.
- **Mutants were coupled to the bug more often than the gate blocked.** A survivor sat on a
  faulty line in 6 of 10. In two of the six that passed the 80% gate (toolz `partition_all`,
  tabulate asciidoc) the score was high enough to wave the function through while the bug's own
  line had a survivor. The gate blocks only on the score. For mutmut, as used here, it used to
  read only the totals, so a reviewer would not have seen these survivors. This benchmark found
  that gap. The gate now also shows Python survivors on their line, as it already did for Stryker
  and gremlins; see [value](../../docs/value.md#3-real-bugs-from-real-projects).
- **Four bugs were invisible to both gates**: toolz `interpose` (every mutant killed, score 100%:
  the bug is a missing `try`/`except`, which no mutation operator can express), tabulate's
  wide-character hang (missing code again) and both semver `next_version` bugs (no survivor on the faulty
  line: every change mutmut tried there was caught, yet the version-ordering property was violated).
- **The four blocks are all boltons**, and the gate there reflects how thinly the whole function
  is tested (the two comparison methods had no test at all at P: 22 mutants, none run, score 0%), not that it
  recognised the bug. At F, with the regression test, three of the ten functions (all boltons)
  still score below 80%, so the gate would also have blocked the fix. The regression test raised
  the score in 5 of 10, left it level in 1 and lowered it in 4: the fixed function has different
  mutants, and several fixes add new code that the new test does not fully pin down.
- **Not shown here**: test presence, and anything about the original bug-introducing change.

## Per-bug results

`P` is the parent of the fix commit (bug live, tests of the day), `F` the fix. Gates are the ones `golden.yml` runs with platform defaults: diff coverage >= 90% on the changed lines, mutation score >= 80% on the changed functions. **Test presence is not evaluated**: it needs the change that introduced the bug, which this benchmark does not reconstruct.

| Bug | Fault | Faulty lines executed at P | Diff coverage | Mutation score at P (surv.) | Mutation gate | Coverage-only pipeline | Full pipeline | Survivor on faulty line | Score at F |
|---|---|---|---|---|---|---|---|---|---|
| more-itertools-bucket-falsey-validator | | | | not run | | | | | |
| more-itertools-one-only-falsy-exception | | | | not run | | | | | |
| more-itertools-seekable-peek-maxlen0 | | | | not run | | | | | |
| boltons-pearson-zero-denominator | omission | no (50.0%) | block | 38.96% (47) | block | flags | flags | yes | 64.71% (block) |
| boltons-indexedset-update | modified | no (0.0%) | block | 60.0% (4) | block | flags | flags | yes | 80.0% (pass) |
| boltons-complementset-issuperset-typo | modified | no (0.0%) | block | 0.0% (22) | block | flags | flags | yes | 36.36% (block) |
| boltons-bits-length | modified | yes (100.0%) | pass | 70.77% (19) | block | misses | flags | yes | 73.85% (block) |
| toolz-interpose-empty | modified | yes (100.0%) | pass | 100.0% (0) | pass | misses | misses | no | 100.0% (pass) |
| toolz-partition-all-bad-len | modified | yes (100.0%) | pass | 92.31% (2) | pass | misses | misses | yes | 86.11% (pass) |
| tabulate-asciidoc-trailing-whitespace | modified | yes (100.0%) | pass | 80.88% (13) | pass | misses | misses | yes | 80.0% (pass) |
| tabulate-wrap-wide-char-hang | omission | yes (100.0%) | pass | 88.89% (6) | pass | misses | misses | no | 91.67% (pass) |
| tabulate-empty-table-maxheadercolwidths | | | | not run | | | | | |
| semver-next-version-legacy-counter | modified | yes (100.0%) | pass | 91.57% (7) | pass | misses | misses | no | 90.7% (pass) |
| semver-next-version-token-switch | modified | yes (100.0%) | pass | 92.5% (6) | pass | misses | misses | no | 91.57% (pass) |

## Best surviving mutant per bug

The survivor on a faulty line (not a docstring) that looks most like the fix; one that *equals* the fix is preferred. A survivor is a bug mutmut planted that no test of the day noticed.

### boltons-pearson-zero-denominator

ZeroDivisionError when c0 == 0; omission: the fix adds a guard before the division. Fix: `73e22c90de`, parent `3913220988`, https://github.com/mahmoud/boltons/pull/434.

`xǁStatsǁ_calc_pearson_type__mutmut_53` on line [467]:

```diff
@@ -20,7 +20,7 @@
             elif beta2 > 3:
                 return 7
     elif round(c2, precision) == 0:
-        return 3  # Gamma
+        return 4  # Gamma
     else:
         k = c1 ** 2 / (4 * c0 * c2)
         if k < 0:
```

### boltons-indexedset-update

`chain(others)` instead of `chain.from_iterable(others)`: update() with several iterables adds the iterables, not their items. Fix: `a180020c76`, parent `e00c10a809`, https://github.com/mahmoud/boltons/pull/474.

`xǁIndexedSetǁupdate__mutmut_9` on line [366]:

```diff
@@ -6,6 +6,6 @@
     elif len(others) == 1:
         other = others[0]
     else:
-        other = chain(others)
+        other = chain(None)
     for o in other:
         self.add(o)
```

### boltons-complementset-issuperset-typo

`issupserset` typo: AttributeError on the complement-vs-complement branches of <= and >=. Fix: `ec1ff9f672`, parent `b5c495bec2`, https://github.com/mahmoud/boltons/issues/438.

`xǁ_ComplementSetǁ__le____mutmut_8` on line [827]:

```diff
@@ -8,7 +8,7 @@
         if exc is None:  # - +
             return False
         else:  # - -
-            return self._excluded.issupserset(exc)
+            return self._excluded.issupserset(None)
     else:
         if inc is None:  # + -
             return self._included.isdisjoint(exc)
```

### boltons-bits-length

off-by-one: `val > 2 ** len_` accepts a value that needs len_ + 1 bits. Fix: `c1c25da365`, parent `e66cade323`.

`xǁBitsǁ__init____mutmut_60` on line [164] (this mutant is exactly the fix):

```diff
@@ -22,7 +22,7 @@
         raise ValueError('Bits cannot represent negative values')
     if len_ is None:
         len_ = len(f'{val:b}')
-    if val > 2 ** len_:
+    if val >= 2 ** len_:
         raise ValueError(f'value {val} cannot be represented with {len_} bits')
     self.val = val  # data is stored internally as integer
     self.len = len_
```

### toolz-interpose-empty

interpose(el, []) raises StopIteration from a bare next(); omission: the fix adds the empty case. Fix: `d2eba0374a`, parent `55ce42dc3b`, https://github.com/pytoolz/toolz/issues/635.

No surviving mutant sits on a faulty line.

### toolz-partition-all-bad-len

partition_all trusts len(seq) and silently returns wrong chunks when it is wrong; the fix adds a consistency check (input validation). Fix: `5a7e078c94`, parent `1fb2b9f1b6`, https://github.com/pytoolz/toolz/issues/602.

`x_partition_all__mutmut_14` on line [735]:

```diff
@@ -25,7 +25,7 @@
         try:
             # If seq defines __len__, then
             # we can quickly calculate where no_pad starts
-            yield prev[:len(seq) % n]
+            yield prev[:len(seq) / n]
         except TypeError:
             # Get first index of no_pad without using .index()
             # https://github.com/pytoolz/toolz/issues/387
```

### tabulate-asciidoc-trailing-whitespace

asciidoc rows end in trailing whitespace; the fix right-strips each returned line. Fix: `3aa568c669`, parent `2939dc1d17`, https://github.com/astanin/python-tabulate/issues/408.

`x__asciidoc_row__mutmut_42` on line [286]:

```diff
@@ -27,7 +27,7 @@
     if len(args) == 2:
         # two arguments are passed if called in the context of aboveline
         # print the table header with column widths and optional header tag
-        return make_header_line(False, *args)
+        return make_header_line(None, *args)
 
     elif len(args) == 3:
         # three arguments are passed if called in the context of dataline or headerline
```

### tabulate-wrap-wide-char-hang

infinite loop when a wide character is wider than the column; omission: the fix forces progress with `i = max(i, 2)`. Fix: `032dc0d107`, parent `1818a3b4ce`, https://github.com/astanin/python-tabulate/issues/399.

No surviving mutant sits on a faulty line.

### semver-next-version-legacy-counter

next_version('prerelease') on rc9 returns rc10 of the same patch, which sorts lower than rc9. Fix: `9ef41316db`, parent `bcd151fa63`, https://github.com/python-semver/python-semver/issues/500.

No surviving mutant sits on a faulty line.

### semver-next-version-token-switch

switching to a prerelease token that sorts earlier (rc -> alpha) returns a version lower than the input. Fix: `bcd151fa63`, parent `94debf9d3e`, https://github.com/python-semver/python-semver/issues/497.

No surviving mutant sits on a faulty line.


## Method

For each kept bug there is a fix commit **F** and its parent **P** (the bug is live at P).

1. **Verify.** F's test files are laid over P. The regression test must fail there (a hang counts,
   120 s limit) and pass at F. Candidates where it did not were discarded.
2. **Coverage at P.** The suite of the day runs under coverage.py 7.16.2 with branch coverage on.
   The *faulty lines* are the lines F changes in the buggy functions, in P's numbering
   (`git diff -U0 P F`); comment, blank and docstring edits do not count. If F only inserts code
   (a missing guard), nothing in P is deleted, so the statements just before and after the
   insertion point stand in for it; such bugs are marked `omission`. A bug is a *coverage blind
   spot* when every executable faulty line was executed.
3. **Mutation at P.** mutmut 3.8.0 runs on the buggy functions only, with the selectors the Golden
   Pipeline itself generates (`golden.presets.PythonPreset.mutation_targets`:
   `<module>.x_<func>__mutmut_*`, `<module>.xǁ<Class>ǁ<method>__mutmut_*`) and the same pytest
   arguments as this repository's `[tool.mutmut]` (`-p no:randomly -x -q`). The score and the gate
   verdict come from the real CLI, `python -m golden mutation mutants/mutmut-cicd-stats.json
   --format mutmut`: (killed + timeout) / (killed + timeout + survived + no tests), minimum 80%.
   Each survivor is located with `mutmut show`; it is *on a faulty line* if the line it changes is
   one of the faulty lines.
4. **Control at F.** The same selectors, with F's source and F's tests (including the regression
   test).

"Coverage-only pipeline" means the diff-coverage gate alone (changed lines >= 90% covered); a bug
is "flagged" when some faulty line was not executed. "Full pipeline" adds the mutation gate.

The suite of the day is what each project's CI runs: pytest over the tests plus doctests
(`--doctest-modules`) for boltons, toolz, tabulate and semver. Python 3.12, an uninstalled
checkout, dependencies resolved as of 2026-10-14. Per-project settings (extra pytest arguments,
copied files, one deselected package-metadata test in toolz, mutmut's `forkserver` isolation for
semver, whose tests reuse single-use context managers) are in `manifest.toml`.

## Selection

- **Pool.** For each project, `git log --no-merges -i -E --grep='\b(fix|fixes|fixed|bug|regression)\b'`
  from a pinned snapshot HEAD (2026-10-22; the SHAs are in `manifest.toml`), examined in that order,
  newest first, with no skipping.
- **Criteria.** The fix commit (1) changes non-test library source in one or two functions of one
  file (top-level functions or direct methods: what mutmut mutates and what Golden's selectors
  name); (2) adds or changes a test in the same commit; (3) fixes a logic bug (boundary,
  condition, edge case, wrong call), not docs, typing, lint, packaging, performance, a
  refactor or a feature, and is small (about 15 changed code lines or fewer); (4) has a
  regression test that fails on P and passes on F; (5) leaves P's own suite green in the
  environment above.
- **Quota.** Examination of a project stopped once it had a quota of kept bugs (more-itertools 3,
  boltons 4, tabulate 3, semver 2, toolz 2). The quotas were chosen to spread the sample over
  projects and to fit the time available; they are not statistical.
- **Every candidate is recorded** in `manifest.toml`: a `[[bug]]` entry (kept) or a `[[discarded]]`
  entry with the reason.

Of the 77 candidates examined (more-itertools 8, boltons 12, toolz 37, tabulate 18, semver 2), 63 were discarded:

- 25: no library source file changed (tests, docs, CI, packaging only)
- 25: no test added or changed in the same commit (the fix may be paired with a test in a neighbouring commit)
- 6: judged by hand (not a defect fix, feature-sized, or suite cannot run)
- 5: touches more than two functions or files
- 2: change outside any function body (constants, imports, tables)

## Excluded, and why

- **Not run, time budget (4 bugs).** Three more-itertools bugs and the tabulate bug in the
  300-line `tabulate()`. They stay in the manifest with a `skip` reason, and their regression
  tests were verified (fail on P, pass on F). A mutmut run takes about 8 minutes on the
  more-itertools suite (50 s of tests run three times, plus mutant generation), and `tabulate()`
  has hundreds of mutants. The benchmark was cut to what could finish in the time left. They are
  not counted in any number above. Run one with `--only` (below) to add it.
- **toolz history before 2019-10.** Candidates there do not run on Python >= 3.10, which mutmut
  3.8 needs (SyntaxError at collection, or unrelated failing tests). Three were tried; the project
  therefore has 2 bugs, not its quota.
- **Projects tried and dropped.** None. humanize was considered and not set up (its tests need a
  generated `_version.py` and compiled translations); it was never run, so nothing was dropped.

## Limits

- **Small n.** A handful of bugs from five small pure-Python libraries. No confidence interval
  is meaningful; one bug moves a percentage by several points.
- **Selection bias.** Requiring a regression test in the fix commit, a small fix in one or two
  functions and a green suite favours tidy, well-tested projects and bugs that someone cared to
  write a test for. The commits are recent (2025-2026) edge-case fixes, several clearly
  AI-assisted by their messages; they are real bugs that were live in released code, but they may
  be easier or harder to catch than the high-priority production bugs of the Google study.
- **Bug-introducing changes are not reconstructed.** P is the code when the fix landed, often years
  after the bug was written. The question answered is "if a change touching these functions were
  gated today with the tests that existed then", not "would the gates have blocked the original
  commit". Test presence could only be judged on the original commit, so it is not evaluated.
- **The faulty lines are an approximation.** For `omission` bugs they are the neighbouring
  statements, so "coverage blind" and "survivor on a faulty line" are weak signals there.
- **The mutation gate scores whole functions.** A function can fail the gate because of survivors
  far from the bug (several functions are large) and pass while the bug's own line has a survivor.
  Both are in the table. mutmut also mutates docstrings, which no suite pins down; the "without
  docstring mutants" rows show the result when those survivors are discounted.
- **Single mutation tool.** mutmut 3.8.0 only. A bug is invisible to it when none of its operators
  can express the fault (missing code, wrong call target), whatever the tests do.
- **F's mutants are not P's mutants.** The control compares scores of two different programs; it
  shows the bar is reachable, not which mutant the regression test killed.
- **One run.** Timeouts and test timing can move a mutant between killed and timeout; nothing was
  repeated.

## Reproduce

Everything below needs network access, `git` and `uv`. It is not part of `pytest`. The ten bugs
that are not marked `skip` took 25 minutes on a 4-core machine (`runtime_seconds` in
`results.json`); a skipped more-itertools bug takes about 8 minutes more (delete its `skip` line).

```console
$ uv run python evals/realfaults/run.py                       # whole manifest: results.json, results.md
$ uv run python evals/realfaults/run.py --only boltons-bits-length --workdir /tmp/rf --out /tmp/rf-out
$ uv run python evals/realfaults/run.py --verify-only        # only check each regression test fails on P, passes on F
```

`--only` takes a bug id from `manifest.toml` (repeatable) and writes to a temp directory unless
`--out` is given, so a partial run never overwrites `results.json`. Scratch clones, virtualenvs and
worktrees live under `--workdir` (a temp dir by default); `--keep` leaves it in place. A bug that
fails for a tool reason is recorded with an `error` and left out of the numbers.

## How the committed results were produced

`results.json` and `results.md` come from one run of the command above on 2026-10-22 (Python 3.12
virtualenvs, mutmut 3.8.0, coverage 7.16.2, pytest 9.1.1). One operator intervention is part of
the record: the tabulate bug in the 300-line `tabulate()` was started, then forced to fail (the
`mutmut` binary was swapped for a stub) so the run could finish within the time budget. It was
afterwards marked `skip` in the manifest, its entry in `results.json` was rewritten as skipped,
and the headline numbers were recomputed from the per-bug records with the runner's own `headline()`.
Re-running the committed manifest skips that bug from the start and gives the same set of bugs.
Every per-bug record, including each survivor with its mutant diff, is in `results.json`.
