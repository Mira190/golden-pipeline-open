import json
import shutil
import time
from pathlib import Path

import pytest

from golden import mutation


def write(tmp_path, data):
    path = tmp_path / "report.json"
    path.write_text(json.dumps(data))
    return path


def stryker(*statuses):
    return {
        "schemaVersion": "3",
        "files": {
            "src/a.ts": {
                "mutants": [
                    {"status": s, "mutatorName": "Arith", "location": {"start": {"line": i + 1}}}
                    for i, s in enumerate(statuses)
                ]
            }
        },
    }


def gremlins(*statuses):
    return {
        "files": [
            {
                "file_name": "pricing/pricing.go",
                "mutations": [
                    {"line": i + 1, "type": "CONDITIONALS_BOUNDARY", "status": s}
                    for i, s in enumerate(statuses)
                ],
            }
        ]
    }


def test_score_definition():
    assert mutation.Tally(killed=6, timeout=2, survived=1, no_coverage=1).score == 80.0
    assert mutation.Tally().score is None


@pytest.mark.parametrize(
    ("tally", "unreliable"),
    [
        (mutation.Tally(killed=1, timeout=1), False),
        (mutation.Tally(killed=1, timeout=2), True),
        (mutation.Tally(), False),
    ],
)
def test_unreliable_when_most_mutants_time_out(tally, unreliable):
    assert tally.unreliable is unreliable


def test_mutmut(tmp_path):
    path = write(
        tmp_path,
        {
            "killed": 5,
            "timeout": 1,
            "suspicious": 1,
            "segfault": 1,
            "survived": 1,
            "no_tests": 1,
            "skipped": 9,
            "total": 19,
        },
    )
    tally, lived = mutation.evaluate(path, "mutmut", {"ignored.py": [(1, 1)]})
    assert tally == mutation.Tally(killed=5, timeout=3, survived=1, no_coverage=1)
    assert tally.score == 80.0
    assert lived == []


def test_stryker_excludes_invalid_mutants(tmp_path):
    path = write(
        tmp_path,
        stryker(
            "Killed",
            "Killed",
            "Timeout",
            "Survived",
            "NoCoverage",
            "CompileError",
            "RuntimeError",
            "Ignored",
            "Pending",
        ),
    )
    tally, lived = mutation.evaluate(path, "stryker")
    assert tally.score == 60.0
    assert lived == [
        mutation.Mutant("src/a.ts", 4, mutation.SURVIVED, "Arith"),
        mutation.Mutant("src/a.ts", 5, mutation.NO_COVERAGE, "Arith"),
    ]


def test_gremlins(tmp_path):
    path = write(
        tmp_path,
        gremlins("KILLED", "KILLED", "TIMED OUT", "LIVED", "NOT VIABLE", "NOT COVERED", "SKIPPED"),
    )
    tally, lived = mutation.evaluate(path, "gremlins")
    assert tally.score == 60.0
    assert [(m.line, m.status) for m in lived] == [(4, "survived"), (6, "no-coverage")]


@pytest.mark.parametrize(
    ("lines", "score"),
    [
        (None, 60.0),
        ({"pricing/pricing.go": [(1, 2)]}, 100.0),
        ({"pricing/pricing.go": [(4, 4), (6, 7)]}, 0.0),
        ({"pricing/pricing.go": [(5, 5)]}, None),
        ({"other.go": [(1, 9)]}, None),
    ],
)
def test_filter_to_changed_lines(tmp_path, lines, score):
    path = write(
        tmp_path,
        gremlins("KILLED", "KILLED", "TIMED OUT", "LIVED", "NOT VIABLE", "NOT COVERED", "SKIPPED"),
    )
    assert mutation.evaluate(path, "gremlins", lines)[0].score == score


@pytest.mark.parametrize("fmt", mutation.PARSERS)
def test_missing_report_fails_closed(tmp_path, fmt):
    with pytest.raises(FileNotFoundError, match="did the mutation tool run"):
        mutation.evaluate(tmp_path / "nope.json", fmt)


@pytest.mark.parametrize(
    ("fmt", "data"),
    [("mutmut", {"killed": 0}), ("stryker", {"files": {}}), ("gremlins", {"files": []})],
)
def test_empty_report_has_no_score(tmp_path, fmt, data):
    assert mutation.evaluate(write(tmp_path, data), fmt)[0].score is None


def test_formats():
    assert set(mutation.PARSERS) == {"gremlins", "mutation-testing-elements", "mutmut", "stryker"}


@pytest.mark.parametrize("fmt", ["stryker", "mutation-testing-elements"])
def test_elements_report_with_absolute_paths(tmp_path, monkeypatch, fmt):
    monkeypatch.chdir(tmp_path)
    data = stryker("Survived", "Killed")
    data["files"] = {"/build/proj/src/a.ts": data["files"]["src/a.ts"]}
    data["projectRoot"] = "/build/proj"
    tally, lived = mutation.evaluate(write(tmp_path, data), fmt, {"src/a.ts": [(1, 1)]})
    assert (tally.survived, tally.killed) == (1, 0)
    assert lived == [mutation.Mutant("src/a.ts", 1, mutation.SURVIVED, "Arith")]


def test_absolute_paths_relative_to_working_directory(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    data = stryker("Survived")
    data["files"] = {f"{tmp_path}/src/a.ts": data["files"]["src/a.ts"]}
    tally, _ = mutation.evaluate(write(tmp_path, data), "stryker", {"src/a.ts": [(1, 1)]})
    assert tally.survived == 1


@pytest.mark.parametrize(
    ("name", "root", "expected"),
    [
        ("src/a.ts", "/p", "src/a.ts"),
        ("/p/src/a.ts", "/p", "src/a.ts"),
        ("/p/src/a.ts", None, "/p/src/a.ts"),
        ("/elsewhere/a.ts", "/p", "/elsewhere/a.ts"),
    ],
)
def test_relative(tmp_path, monkeypatch, name, root, expected):
    monkeypatch.chdir(tmp_path)
    assert mutation._relative(name, root) == expected


def test_stryker_counts_mutants_without_a_verdict(tmp_path):
    path = write(tmp_path, stryker("Killed", "CompileError", "RuntimeError", "Pending", "Ignored"))
    tally, _ = mutation.evaluate(path, "stryker")
    assert tally == mutation.Tally(killed=1, invalid=3)
    assert not tally.inconclusive


@pytest.mark.parametrize(
    ("fmt", "data"),
    [
        ("stryker", {"files": {"a.ts": {"mutants": [{"status": "Survived"}]}}}),
        ("stryker", {"files": {"a.ts": {"mutants": [{"location": {"start": {"line": 1}}}]}}}),
        ("stryker", {"files": []}),
        ("gremlins", {"files": [{"mutations": [{"line": 1, "status": "LIVED"}]}]}),
        ("gremlins", {"files": [{"file_name": "a.go", "mutations": [{"line": "x"}]}]}),
        ("mutmut", {"killed": "many"}),
        ("mutmut", []),
    ],
)
def test_malformed_report_fails_closed(tmp_path, fmt, data):
    with pytest.raises(ValueError, match=f"malformed {fmt} report"):
        mutation.evaluate(write(tmp_path, data), fmt)


@pytest.mark.parametrize(
    ("fmt", "data"),
    [
        ("stryker", stryker("CompileError", "CompileError")),
        ("gremlins", gremlins("NOT VIABLE")),
    ],
)
def test_run_without_any_verdict_is_inconclusive(tmp_path, fmt, data):
    tally, _ = mutation.evaluate(write(tmp_path, data), fmt)
    assert tally.score is None
    assert tally.inconclusive


@pytest.mark.parametrize(
    ("fmt", "data"), [("stryker", stryker("Ignored")), ("gremlins", gremlins("SKIPPED"))]
)
def test_deliberately_ignored_mutants_are_not_inconclusive(tmp_path, fmt, data):
    assert not mutation.evaluate(write(tmp_path, data), fmt)[0].inconclusive


def test_interrupted_mutmut_run_fails_closed(tmp_path):
    path = write(tmp_path, {"killed": 3, "check_was_interrupted_by_user": 1})
    with pytest.raises(ValueError, match="interrupted"):
        mutation.evaluate(path, "mutmut")


@pytest.mark.parametrize("name", ["./pricing/pricing.go", "{cwd}/pricing/pricing.go"])
def test_report_paths_are_normalised_before_filtering(tmp_path, monkeypatch, name):
    monkeypatch.chdir(tmp_path)
    data = gremlins("LIVED")
    data["files"][0]["file_name"] = name.format(cwd=tmp_path)
    tally, _ = mutation.evaluate(
        write(tmp_path, data), "gremlins", {"pricing/pricing.go": [(1, 1)]}
    )
    assert tally.survived == 1


def test_absolute_path_outside_cwd_is_kept(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    data = gremlins("LIVED")
    data["files"][0]["file_name"] = "/elsewhere/pricing.go"
    tally, _ = mutation.evaluate(
        write(tmp_path, data), "gremlins", {"/elsewhere/pricing.go": [(1, 1)]}
    )
    assert tally.survived == 1


# ---------------------------------------------------------------- arid lines
JS_ARID_LINES = [
    'console.log("x");',
    "  console.log(x)",
    "console.debug('a', b);",
    "console.info(x);",
    "console.warn(x)",
    "\tconsole.error(err);",
    "console.trace()",
    "console.log(x);   ",
    "console.log(x);\r",  # a CRLF checkout
    'debug("worker");',
    "debug('worker:%d', n)",
    "logger.debug(a + b)",
    "logger.warn('x')",
    "logger.error(err);",
    "logger.trace()",
    "log.info('x');",
    "log.debug(a)",
    "this.logger.error(e);",
    "this.log.warn(e)",
    'console.log("(");',  # parentheses inside a string do not count
    "console.log('a;b', \"c)\");",
    "console.log(a == b, a === b, a != b, a !== b, a <= b, a >= b);",
]
JS_CODE_LINES = [
    # A nested call or a template interpolation may do work: its mutants stay.
    "logger.info(`done ${n}`);",
    "console.log(fmt(a), (b + c) * 2);",
    "console.log(items.map((x) => x * 2));",
    "console.log(`${a}`, 'it\\'s');",
    "console.log(apply_discount(x));",
    "const log = 1;",
    "let debug = 2;",
    "const debug = require('debug')('app');",
    "debug = false;",
    "debugger;",
    "log.level = 'info';",
    "return x || console.log(y);",
    "x = console.log(a);",
    "if (a) console.log(b);",
    "const f = () => console.log(a);",
    "await console.log(a);",
    "console.log(a); return b;",
    "console.log(a); go(b);",
    "console.log(a); go()",
    "console.log(a) || go(b);",
    "console.log(a), go(b);",
    "console.log(a) + (b);",
    "console.log(a).then(go);",
    "logger.info(a).then(go);",
    "console.log(",  # a call that continues on the next line
    '  "message" + count,',
    ")",
    'console.log("x"); // see #12',
    "console.table(rows);",
    "console.logger(x);",
    "console.log;",
    "console.log.bind(c);",
    "logger.fatal(e);",
    "logger.level(2);",
    "mylogger.info(x);",
    "catalog.error(x);",
    "dialog.info(x);",
    "blog.debug(x);",
    "debug.enabled('x');",
    "debugMode(x);",
    "debug(x).then(y);",
    "foo.logger.info(x);",
    "console.log(i++);",
    "console.log(--n);",
    "console.log(x = 2);",
    "console.log(total += 1);",
    "console.log(x ??= 2);",
    "console.log(await load());",
    "return console.log(a);",
    "",
    "   ",
]
GO_ARID_LINES = [
    'log.Println("x")',
    'log.Printf("a %d", n)',
    "log.Print(x)",
    "log.Fatal(err)",
    'log.Fatalf("x: %v", err)',
    "log.Fatalln(err)",
    'log.Panic("x")',
    'log.Panicf("x %d", n)',
    "log.Panicln(x)",
    '\tfmt.Println("x")',
    'fmt.Printf("%d\\n", n)',
    "fmt.Print(x)",
    'slog.Debug("x", "k", v)',
    'slog.Info("x")',
    'slog.Warn("x")',
    'slog.Error("x", "err", err)',
    'slog.InfoContext(ctx, "x")',
    'logger.Info("x")',
    'logger.Debug("x");',
    'logger.Warn("x")',
    'logger.Error("x")',
    'logger.Infof("x %d", n)',
    'logger.Errorw("x", "k", v)',
    "logger.Errorln(err)",
    's.logger.Debugf("x %d", n)',
    'l.deps.logger.Warnw("x", "k", v)',
    '\t\tlog.Printf("%s", a+b)',
    'log.Printf("%s", `C:\\temp`)',
    "log.Printf(\"%c\", ')')",
]
GO_CODE_LINES = [
    # A nested call may do work: its mutants stay.
    'log.Printf("%q (%d)", name, len(f(x)))',
    'log.Printf("%d", applyDiscount(x))',
    'x := fmt.Sprintf("x %d", n)',
    'fmt.Sprintf("x")',
    'return fmt.Errorf("x: %w", err)',
    'fmt.Errorf("x")',
    'fmt.Fprintf(w, "x")',
    "fmt.Fprintln(os.Stderr, x)",
    "fmt.Sprint(x)",
    "fmt.Printer(x)",
    "fmt.Print",
    "log.SetFlags(0)",
    'log.New(os.Stderr, "", 0)',
    "log.Println",
    "logrus.Info(x)",
    "slog.New(h)",
    "slog.SetDefault(l)",
    "slog.Infof(x)",
    "slog.Fatal(x)",
    'logger.With("k", v)',
    "logger.Sync()",
    "logger.Fatal(err)",
    "logger.Infoz(x)",
    "mylog.Println(x)",
    "blog.Println(x)",
    "defer log.Println(x)",
    "go log.Println(x)",
    "if err != nil { log.Println(err) }",
    "return log.Println(x)",
    "log.Println(x) // trailing",
    'log.Printf("x"); return nil',
    'log.Printf("x"); return f(y)',
    'log.Printf("x") || f(y)',
    "log.Printf(",
    '\t"message", n,',
    "log.Println(<-ch)",
    "log.Println(func() int { n++; return n }())",
    "log.Println(n--)",
    "",
]


@pytest.mark.parametrize("line", JS_ARID_LINES)
def test_js_arid_lines(line):
    assert mutation.is_arid_line(line, mutation.JS_ARID)


@pytest.mark.parametrize("line", JS_CODE_LINES)
def test_js_lines_with_logic_are_not_arid(line):
    assert not mutation.is_arid_line(line, mutation.JS_ARID)


@pytest.mark.parametrize("line", GO_ARID_LINES)
def test_go_arid_lines(line):
    assert mutation.is_arid_line(line, mutation.GO_ARID)


@pytest.mark.parametrize("line", GO_CODE_LINES)
def test_go_lines_with_logic_are_not_arid(line):
    assert not mutation.is_arid_line(line, mutation.GO_ARID)


@pytest.mark.parametrize("line", [*GO_ARID_LINES, *JS_ARID_LINES])
def test_no_patterns_match_nothing(line):
    assert not mutation.is_arid_line(line, ())


@pytest.mark.parametrize(
    "line", ['console.log("x")', 'debug("x")', 'log.Println("x")', 'fmt.Println("x")']
)
def test_each_language_has_its_own_patterns(line):
    in_js = mutation.is_arid_line(line, mutation.JS_ARID)
    in_go = mutation.is_arid_line(line, mutation.GO_ARID)
    assert in_js != in_go


@pytest.mark.parametrize(
    ("patterns", "lines"),
    [(mutation.JS_ARID, JS_ARID_LINES), (mutation.GO_ARID, GO_ARID_LINES)],
)
def test_every_pattern_has_a_positive_example(patterns, lines):
    for pattern in patterns:
        assert any(pattern.match(line) for line in lines), pattern.pattern


@pytest.mark.parametrize(
    ("args", "pure"),
    [
        ("", True),
        ("a, b", True),
        ("f(a), (b)", False),
        ("apply(x)", False),
        ("`${x}`", False),
        ("`$x {x}`", True),
        ('"${x}"', False),
        ('"(", x', True),
        ("'(' + \"(\"", True),
        ("`)`", True),
        ('"a\\"(", b', True),
        ("a == b, a != b, a <= b, a >= b, a === b, a !== b, x => y", True),
        ("a) || go(b", False),
        ("a) + b", False),
        (")", False),
        ("a), go(b", False),
        ("a) + (b", False),
        ("(a", False),
        ("a(", False),
        (")(", False),
        ("i++", False),
        ("--i", False),
        ("a--", False),
        ("x = 1", False),
        ("x=1", False),
        ("x += 1", False),
        ("x ??= 1", False),
        ("await f()", False),
        ("awaited, awaiting", True),
        ("<-ch", False),
        ('"i++", "x = 1", "await", "<-"', True),
    ],
)
def test_pure_args(args, pure):
    assert mutation._pure_args(args) is pure


def test_arid_patterns_by_extension():
    js = {".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".mts", ".cts"}
    assert {s for s, p in mutation.ARID_PATTERNS.items() if p is mutation.JS_ARID} == js
    assert mutation.ARID_PATTERNS[".go"] is mutation.GO_ARID
    assert len(mutation.ARID_PATTERNS) == len(js) + 1


def src(tmp_path, monkeypatch, files):
    """Sources live in the working directory, as in CI."""
    monkeypatch.chdir(tmp_path)
    for name, text in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode() if isinstance(text, str) else text)


def test_source_lines(tmp_path, monkeypatch):
    src(tmp_path, monkeypatch, {"a.ts": "one\ntwo\r\nthree\n", "empty.ts": ""})
    assert mutation._source_lines("a.ts") == ["one", "two", "three", ""]  # CRLF is one break
    assert mutation._source_lines("empty.ts") == [""]


@pytest.mark.parametrize(
    "name", ["missing.ts", "dir.ts", "binary.ts", "nul\0.ts", "", "../outside.ts"]
)
def test_unreadable_source_is_none_not_an_error(tmp_path, monkeypatch, name):
    src(tmp_path, monkeypatch, {"binary.ts": b"\xff\xfe\x00console.log(1)\n"})
    (tmp_path / "dir.ts").mkdir()
    assert mutation._source_lines(name) is None


def m(file, line, status=mutation.SURVIVED):
    return mutation.Mutant(file, line, status, "M")


def test_drop_arid_mutants(tmp_path, monkeypatch):
    src(
        tmp_path,
        monkeypatch,
        {
            "src/a.ts": "const a = 1;\nconsole.log(a);\nreturn a > 0;\nlogger.info(a)\n",
            "pkg/b.go": 'x := 1\nlog.Println("x")\nfmt.Sprintf("%d", x)\n',
            "c.py": 'print("x")\nconsole.log(1)\n',
        },
    )
    mutants = [
        m("src/a.ts", 1),
        m("src/a.ts", 2),
        m("src/a.ts", 3, mutation.KILLED),
        m("src/a.ts", 4, mutation.NO_COVERAGE),
        m("pkg/b.go", 1),
        m("pkg/b.go", 2),
        m("pkg/b.go", 3),
        m("c.py", 2),
        m("README", 1),
    ]
    kept, dropped = mutation.drop_arid(mutants)
    assert dropped == 3
    assert kept == [mutants[i] for i in (0, 2, 4, 6, 7, 8)]


def test_drop_arid_with_nothing_to_drop():
    assert mutation.drop_arid([]) == ([], 0)


@pytest.mark.parametrize(
    ("file", "arid"),
    [("src/a.ts", True), ("src/A.TS", True), ("src/a.Go", False), ("src/a.cs", False)],
)
def test_file_suffix_selects_the_language(tmp_path, monkeypatch, file, arid):
    src(tmp_path, monkeypatch, {file: "console.log(1)\n"})
    assert mutation._is_arid(m(file, 1), {}) is arid


@pytest.mark.parametrize(("line", "arid"), [(-1, False), (0, False), (1, True), (2, False)])
def test_line_numbers_outside_the_file_are_never_arid(tmp_path, monkeypatch, line, arid):
    src(tmp_path, monkeypatch, {"a.ts": "console.log(1)"})
    assert mutation._is_arid(m("a.ts", line), {}) is arid


def test_line_zero_is_not_the_last_line(tmp_path, monkeypatch):
    src(tmp_path, monkeypatch, {"a.ts": "go(1)\nconsole.log(1)\n\nconsole.log(2)"})
    assert not mutation._is_arid(m("a.ts", 0), {})
    assert not mutation._is_arid(m("a.ts", 5), {})
    assert mutation._is_arid(m("a.ts", 4), {})


def test_each_source_is_read_once(tmp_path, monkeypatch):
    src(tmp_path, monkeypatch, {"a.ts": "console.log(1)\nx()\n", "b.ts": "x()\n"})
    reads = []
    real = mutation._source_lines
    monkeypatch.setattr(mutation, "_source_lines", lambda f: reads.append(f) or real(f))
    mutation.drop_arid([m("a.ts", 1), m("a.ts", 2), m("b.ts", 1), m("a.ts", 1), m("b.ts", 1)])
    assert reads == ["a.ts", "b.ts"]


def test_unreadable_source_is_read_once_and_never_arid(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    reads = []
    real = mutation._source_lines
    monkeypatch.setattr(mutation, "_source_lines", lambda f: reads.append(f) or real(f))
    kept, dropped = mutation.drop_arid([m("gone.ts", 1), m("gone.ts", 2)])
    assert (len(kept), dropped, reads) == (2, 0, ["gone.ts"])


def test_languages_without_patterns_are_not_read(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(mutation, "_source_lines", lambda f: pytest.fail(f"read {f}"))
    assert mutation.drop_arid([m("a.py", 1), m("a", 1)])[1] == 0


LOGGY = (
    "const total = a + b;\n"  # 1
    "console.log(total);\n"  # 2
    "if (total > 10) {\n"  # 3
    '  logger.info("big");\n'  # 4
    "}\n"  # 5
    "return total;\n"  # 6
)


def stryker_at(*mutants, file="src/a.ts"):
    return {
        "files": {
            file: {
                "mutants": [
                    {"status": s, "mutatorName": "M", "location": {"start": {"line": n}}}
                    for n, s in mutants
                ]
            }
        }
    }


def test_arid_mutants_are_neither_detected_nor_survived(tmp_path, monkeypatch):
    src(tmp_path, monkeypatch, {"src/a.ts": LOGGY})
    path = write(
        tmp_path,
        stryker_at((1, "Killed"), (2, "Survived"), (3, "Killed"), (4, "Survived"), (6, "Survived")),
    )
    tally, lived = mutation.evaluate(path, "stryker")
    assert tally == mutation.Tally(killed=2, survived=1, arid=2)
    assert tally.score == pytest.approx(66.67)
    assert lived == [mutation.Mutant("src/a.ts", 6, mutation.SURVIVED, "M")]


def test_arid_filter_can_be_switched_off(tmp_path, monkeypatch):
    src(tmp_path, monkeypatch, {"src/a.ts": LOGGY})
    path = write(tmp_path, stryker_at((1, "Killed"), (2, "Survived"), (4, "NoCoverage")))
    tally, lived = mutation.evaluate(path, "stryker", arid_filter=False)
    assert tally == mutation.Tally(killed=1, survived=1, no_coverage=1)
    assert [x.line for x in lived] == [2, 4]
    assert mutation.evaluate(path, "stryker", arid_filter=True)[0].arid == 2


def test_arid_filter_is_on_by_default_for_gremlins(tmp_path, monkeypatch):
    src(tmp_path, monkeypatch, {"pricing/pricing.go": 'x := 1\nlog.Println("x")\n'})
    path = write(tmp_path, gremlins("KILLED", "LIVED"))
    tally, lived = mutation.evaluate(path, "gremlins")
    assert (tally.score, tally.arid, lived) == (100.0, 1, [])
    assert mutation.evaluate(path, "gremlins", arid_filter=False)[0].score == 50.0


def test_only_arid_mutants_in_the_changed_lines_are_counted(tmp_path, monkeypatch):
    src(tmp_path, monkeypatch, {"src/a.ts": LOGGY})
    path = write(
        tmp_path, stryker_at((1, "Killed"), (2, "Survived"), (4, "Survived"), (6, "Killed"))
    )
    tally, _ = mutation.evaluate(path, "stryker", {"src/a.ts": [(2, 3)]})
    assert tally == mutation.Tally(arid=1)
    tally, _ = mutation.evaluate(path, "stryker", {"src/a.ts": [(1, 6)]})
    assert tally == mutation.Tally(killed=2, arid=2)
    assert mutation.evaluate(path, "stryker", {"other.ts": [(1, 6)]})[0] == mutation.Tally()


@pytest.mark.parametrize(
    "statuses", [["Survived", "Killed"], ["Survived", "CompileError"], ["CompileError", "Ignored"]]
)
def test_a_report_of_only_arid_mutants_has_no_score_and_is_not_inconclusive(
    tmp_path, monkeypatch, statuses
):
    src(tmp_path, monkeypatch, {"src/a.ts": LOGGY})
    path = write(tmp_path, stryker_at((2, statuses[0]), (4, statuses[1])))
    tally, lived = mutation.evaluate(path, "stryker")
    assert tally == mutation.Tally(arid=2)
    assert tally.score is None
    assert not tally.inconclusive
    assert lived == []


def test_a_run_without_verdicts_outside_arid_lines_is_still_inconclusive(tmp_path, monkeypatch):
    src(tmp_path, monkeypatch, {"src/a.ts": LOGGY})
    path = write(tmp_path, stryker_at((2, "Killed"), (6, "CompileError")))
    tally, _ = mutation.evaluate(path, "stryker")
    assert tally.inconclusive
    assert tally.arid == 1


def test_unreadable_sources_exclude_nothing(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    path = write(tmp_path, stryker_at((2, "Survived"), (4, "Killed")))
    tally, lived = mutation.evaluate(path, "stryker")
    assert tally == mutation.Tally(killed=1, survived=1)
    assert [x.line for x in lived] == [2]


def test_malformed_reports_are_still_errors_with_the_arid_filter(tmp_path, monkeypatch):
    src(tmp_path, monkeypatch, {"src/a.ts": LOGGY})
    data = stryker_at((2, "Survived"))
    del data["files"]["src/a.ts"]["mutants"][0]["location"]
    with pytest.raises(ValueError, match="malformed stryker report"):
        mutation.evaluate(write(tmp_path, data), "stryker")


def test_mutmut_has_no_lines_to_filter(tmp_path, monkeypatch):
    monkeypatch.setattr(mutation, "_source_lines", lambda f: pytest.fail(f"read {f}"))
    tally, _ = mutation.evaluate(write(tmp_path, {"killed": 1, "survived": 1}), "mutmut")
    assert tally == mutation.Tally(killed=1, survived=1)


def test_tally_of_records_the_excluded_mutants():
    assert mutation.Tally.of([m("a", 1, mutation.KILLED)], 4) == mutation.Tally(killed=1, arid=4)
    assert mutation.Tally.of([]) == mutation.Tally()
    assert mutation.Tally.of([]).arid == 0


def test_arid_note():
    assert mutation.Tally().arid_note == ""
    assert mutation.Tally(killed=1).arid_note == ""
    assert mutation.Tally(arid=3).arid_note == (
        "\n\n3 mutants excluded as arid (logging/debug output)."
    )


# ------------------------------------------------------------ mutmut survivors
# tests/fixtures/mutmut is real mutmut 3.8.0 output, captured once (mutmut is never run here):
# the project's source, and what `mutmut run` + `mutmut export-cicd-stats` left in mutants/.
# It has killed and surviving mutants, a method, a staticmethod, an async function with a
# nested def, a default argument, a multi-line signature and call, and a function no test reaches.
# Captured with `mutmut run` and `mutmut export-cicd-stats` on src/shop/pricing.py (here
# pricing.py.original) and tests that call each function; `pricing.meta` is mutants/src/shop/
# pricing.py.meta, renamed so that mutmut's own copy of tests/ (it copies every file) holds no
# file that looks like one of its result files.
FIXTURES = Path(__file__).parent / "fixtures" / "mutmut"
PRICING = "src/shop/pricing.py"
STATS = Path("mutants/mutmut-cicd-stats.json")
# What the stats file says: 12 killed, 9 survived, 2 "no tests", of 23 mutants.
FIXTURE_TALLY = mutation.Tally(killed=12, survived=9, no_coverage=2)

# Every mutant of the fixture and the source line it changes: checked against `mutmut show`.
LINE_OF = {
    "x_discount__mutmut_1": 8,
    "x_discount__mutmut_2": 8,
    "x_discount__mutmut_3": 9,
    "x_discount__mutmut_4": 10,
    "x_total__mutmut_1": 15,
    "x_scale__mutmut_1": 20,  # the default argument, on the def line
    "x_scale__mutmut_2": 21,
    "x_unused__mutmut_1": 25,
    "x_unused__mutmut_2": 25,
    "xǁCartǁ__init____mutmut_1": 30,
    "xǁCartǁadd__mutmut_1": 34,
    "xǁCartǁadd__mutmut_2": 34,
    "xǁCartǁadd__mutmut_3": 34,
    "xǁCartǁadd__mutmut_4": 34,
    "xǁCartǁadd__mutmut_5": 36,  # `(label, price, 1)` becomes `None`: the call has fewer lines
    "xǁCartǁadd__mutmut_6": 38,  # the `1` on the third line of the tuple
    "xǁCartǁtax__mutmut_1": 44,  # a staticmethod: its decorator is not the def line
    "xǁCartǁtax__mutmut_2": 44,
    "x_fetch__mutmut_1": 49,  # an async function: the mutation is in the nested def
    "x_fetch__mutmut_2": 49,
    "x_fetch__mutmut_3": 50,
    "x_fetch__mutmut_4": 50,
    "x_fetch__mutmut_5": 50,
}
# The mutants that survived or were not covered, as mutmut recorded them.
SURVIVORS = [
    (8, "survived", "shop.pricing.x_discount__mutmut_1"),
    (8, "survived", "shop.pricing.x_discount__mutmut_2"),
    (15, "survived", "shop.pricing.x_total__mutmut_1"),
    (20, "survived", "shop.pricing.x_scale__mutmut_1"),
    (25, "no-coverage", "shop.pricing.x_unused__mutmut_1"),
    (25, "no-coverage", "shop.pricing.x_unused__mutmut_2"),
    (34, "survived", "shop.pricing.xǁCartǁadd__mutmut_1"),
    (34, "survived", "shop.pricing.xǁCartǁadd__mutmut_3"),
    (34, "survived", "shop.pricing.xǁCartǁadd__mutmut_4"),
    (36, "survived", "shop.pricing.xǁCartǁadd__mutmut_5"),
    (38, "survived", "shop.pricing.xǁCartǁadd__mutmut_6"),
]


def mutmut_tree(tmp_path, monkeypatch, *paths):
    """The fixture as mutmut leaves it, once per module path: the source in the project and
    the trampolined module and its .meta in mutants/. The working directory is the project."""
    monkeypatch.chdir(tmp_path)
    for path in paths or (PRICING,):
        for name, target in (
            ("pricing.py.original", path),
            ("pricing.py.mutated", f"mutants/{path}"),
            ("pricing.meta", f"mutants/{path}.meta"),
        ):
            (tmp_path / target).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(FIXTURES / name, tmp_path / target)
    shutil.copy(FIXTURES / "mutmut-cicd-stats.json", tmp_path / STATS)
    return STATS


def where(lived):
    return [(m.file, m.line, m.status, m.mutator) for m in lived]


def set_exit_codes(codes, path=PRICING):
    """Rewrite the exit codes of the fixture's .meta; `codes` maps a mutant's short name."""
    meta = Path("mutants", path + ".meta")
    data = json.loads(meta.read_text(encoding="utf-8"))
    data["exit_code_by_key"] = {
        key: codes(key.rpartition(".")[2], code) for key, code in data["exit_code_by_key"].items()
    }
    meta.write_text(json.dumps(data), encoding="utf-8")


def edit(path, old, new):
    """Change the text of a file of the tree, which must contain `old`."""
    text = Path(path).read_text(encoding="utf-8")
    assert old in text
    Path(path).write_text(text.replace(old, new), encoding="utf-8")


def test_the_fixture_is_what_the_stats_file_says(tmp_path, monkeypatch):
    stats = mutmut_tree(tmp_path, monkeypatch)
    meta = json.loads(Path(f"mutants/{PRICING}.meta").read_text(encoding="utf-8"))
    codes = list(meta["exit_code_by_key"].values())
    assert (codes.count(0), codes.count(33), len(codes)) == (9, 2, 23)
    assert set(codes) == {0, 1, 33}
    assert mutation.evaluate(stats, "mutmut")[0] == FIXTURE_TALLY


def test_survivors_are_placed_on_their_source_lines(tmp_path, monkeypatch):
    stats = mutmut_tree(tmp_path, monkeypatch)
    tally, lived = mutation.evaluate(stats, "mutmut")
    assert where(lived) == [(PRICING, line, status, key) for line, status, key in SURVIVORS]
    assert tally.unlocated == ()
    assert tally.locate_failure == ""
    assert tally.survivors_note == ""


def test_the_survivors_never_change_the_tally(tmp_path, monkeypatch):
    stats = mutmut_tree(tmp_path, monkeypatch)
    with_tree = mutation.evaluate(stats, "mutmut")[0]
    shutil.rmtree("mutants/src")
    without_tree = mutation.evaluate(stats, "mutmut")[0]
    assert with_tree == without_tree == FIXTURE_TALLY
    assert with_tree.score == without_tree.score == pytest.approx(52.17)


def test_every_mutant_is_placed_on_the_line_it_changes(tmp_path, monkeypatch):
    stats = mutmut_tree(tmp_path, monkeypatch)
    set_exit_codes(lambda name, code: 0)
    lived = mutation.evaluate(stats, "mutmut")[1]
    assert [(m.line, m.mutator.rpartition(".")[2]) for m in lived] == sorted(
        ((line, name) for name, line in LINE_OF.items()), key=lambda item: item[0]
    )


@pytest.mark.parametrize(
    ("ranges", "lines"),
    [
        (None, [8, 8, 15, 20, 25, 25, 34, 34, 34, 36, 38]),
        ([(34, 36)], [34, 34, 34, 36]),
        ([(8, 8)], [8, 8]),
        ([(7, 7)], []),
        ([(9, 14)], []),
        ([(15, 15)], [15]),
        ([(16, 19)], []),
        ([(1, 7), (36, 36), (38, 40)], [36, 38]),
        ([], []),
    ],
)
def test_survivors_are_narrowed_to_the_changed_lines(tmp_path, monkeypatch, ranges, lines):
    stats = mutmut_tree(tmp_path, monkeypatch)
    scope = None if ranges is None else {PRICING: ranges}
    tally, lived = mutation.evaluate(stats, "mutmut", scope)
    assert [m.line for m in lived] == lines
    assert tally == FIXTURE_TALLY  # the score still covers the whole run


def test_survivors_of_other_files_are_out_of_scope(tmp_path, monkeypatch):
    stats = mutmut_tree(tmp_path, monkeypatch)
    assert mutation.evaluate(stats, "mutmut", {"src/shop/other.py": [(1, 99)]})[1] == []


def test_files_out_of_scope_are_not_read(tmp_path, monkeypatch):
    stats = mutmut_tree(tmp_path, monkeypatch)
    monkeypatch.setattr(mutation, "_exit_codes", lambda meta: pytest.fail(f"read {meta}"))
    assert mutation.evaluate(stats, "mutmut", {"src/other.py": [(1, 9)]})[1] == []


def test_files_without_survivors_are_not_parsed(tmp_path, monkeypatch):
    stats = mutmut_tree(tmp_path, monkeypatch)
    set_exit_codes(lambda name, code: 1)
    monkeypatch.setattr(mutation, "_defs", lambda path: pytest.fail(f"parsed {path}"))
    tally, lived = mutation.evaluate(stats, "mutmut")
    assert (lived, tally.unlocated, tally.locate_failure) == ([], (), "")


def test_a_file_without_survivors_does_not_end_the_search(tmp_path, monkeypatch):
    stats = mutmut_tree(tmp_path, monkeypatch, "src/aaa.py", PRICING)  # aaa comes first
    set_exit_codes(lambda name, code: 1, "src/aaa.py")
    lived = mutation.evaluate(stats, "mutmut")[1]
    assert {m.file for m in lived} == {PRICING}
    assert len(lived) == len(SURVIVORS)


@pytest.mark.parametrize(
    ("code", "status"),
    [(0, "survived"), (5, "no-coverage"), (33, "no-coverage")],
)
def test_exit_codes_that_mean_a_survivor(tmp_path, monkeypatch, code, status):
    stats = mutmut_tree(tmp_path, monkeypatch)
    set_exit_codes(lambda name, old: code if name == "x_total__mutmut_1" else 1)
    lived = mutation.evaluate(stats, "mutmut")[1]
    assert where(lived) == [(PRICING, 15, status, "shop.pricing.x_total__mutmut_1")]


@pytest.mark.parametrize(
    "code",
    [1, 3, 2, 4, 34, 35, 36, 37, 24, -24, 152, 255, -9, -11, 99, -1, None],
)
def test_exit_codes_that_do_not_mean_a_survivor(tmp_path, monkeypatch, code):
    stats = mutmut_tree(tmp_path, monkeypatch)
    set_exit_codes(lambda name, old: code)
    tally, lived = mutation.evaluate(stats, "mutmut")
    assert (lived, tally.unlocated, tally.locate_failure) == ([], (), "")


def test_survivors_are_sorted_by_file_then_line(tmp_path, monkeypatch):
    stats = mutmut_tree(tmp_path, monkeypatch, "src/zzz/mod.py", PRICING, "src/aaa.py")
    set_exit_codes(lambda name, code: 0 if name.startswith("x_total") else 1, "src/zzz/mod.py")
    set_exit_codes(lambda name, code: 0 if name.startswith("x_scale") else 1, PRICING)
    set_exit_codes(lambda name, code: 0 if name.startswith("x_discount") else 1, "src/aaa.py")
    lived = mutation.evaluate(stats, "mutmut")[1]
    assert [(m.file, m.line) for m in lived] == [
        ("src/aaa.py", 8),
        ("src/aaa.py", 8),
        ("src/aaa.py", 9),
        ("src/aaa.py", 10),
        (PRICING, 20),
        (PRICING, 21),
        ("src/zzz/mod.py", 15),
    ]


def test_survivors_on_one_line_keep_mutmuts_order(tmp_path, monkeypatch):
    stats = mutmut_tree(tmp_path, monkeypatch)
    set_exit_codes(lambda name, old: 0 if name.startswith("xǁCartǁadd") else 1)
    keys = [m.mutator.rpartition("_")[2] for m in mutation.evaluate(stats, "mutmut")[1]]
    assert keys == ["1", "2", "3", "4", "5", "6"]


def test_the_report_may_be_named_by_an_absolute_path(tmp_path, monkeypatch):
    mutmut_tree(tmp_path, monkeypatch)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    lived = mutation.evaluate(tmp_path / STATS, "mutmut")[1]
    assert where(lived) == [(PRICING, line, status, key) for line, status, key in SURVIVORS]


def test_the_report_may_be_named_from_inside_mutants(tmp_path, monkeypatch):
    mutmut_tree(tmp_path, monkeypatch)
    monkeypatch.chdir(tmp_path / "mutants")
    lived = mutation.evaluate(Path("mutmut-cicd-stats.json"), "mutmut")[1]
    assert [m.line for m in lived] == [line for line, _, _ in SURVIVORS]


def test_only_python_results_are_read(tmp_path, monkeypatch):
    stats = mutmut_tree(tmp_path, monkeypatch)
    Path("mutants/data.meta").write_text("not json", encoding="utf-8")
    Path("mutants/Thing.cs.meta").write_text("guid: 1", encoding="utf-8")
    tally, lived = mutation.evaluate(stats, "mutmut")
    assert len(lived) == len(SURVIVORS)
    assert tally.locate_failure == ""


# A survivor that cannot be placed is named, never put on a guessed line.
def stems(tally):
    """The functions whose survivors are named, as mutmut spells them (`x_f`, `xǁCǁm`)."""
    return {key.rpartition(".")[2].rpartition("__mutmut_")[0] for key in tally.unlocated}


@pytest.mark.parametrize(
    ("old", "new", "unplaced"),
    [
        pytest.param("if qty >= 50:", "if qty >= 49:", "x_discount", id="changed-line"),
        pytest.param("keep the bigger", "keep a bigger", "x_total", id="changed-comment"),
        pytest.param("factor=2):", "factor=5):", "x_scale", id="changed-def-line"),
        pytest.param("def total(a, b):", "def total2(a, b):", "x_total", id="renamed"),
        pytest.param("        return len(self.items)\n", "", "xǁCartǁadd", id="removed-line"),
        pytest.param('"item:" + name', "'item:' + name", "xǁCartǁadd", id="changed-method"),
        pytest.param("class Cart:", "class Basket:", "xǁCartǁadd", id="other-class"),
    ],
)
def test_survivors_of_a_function_changed_since_the_run_are_named(
    tmp_path, monkeypatch, old, new, unplaced
):
    stats = mutmut_tree(tmp_path, monkeypatch)
    edit(PRICING, old, new)
    tally, lived = mutation.evaluate(stats, "mutmut")
    assert stems(tally) == {unplaced}
    # Every other survivor is still placed, and each one is either placed or named.
    assert {m.mutator for m in lived} | set(tally.unlocated) == {k for _, _, k in SURVIVORS}
    assert len(lived) + len(tally.unlocated) == len(SURVIVORS)
    assert tally == FIXTURE_TALLY


def test_changes_outside_the_function_do_not_unplace_its_survivors(tmp_path, monkeypatch):
    stats = mutmut_tree(tmp_path, monkeypatch)
    edit(PRICING, '"""Pricing."""\n', '"""Pricing."""\n\n\nOTHER = 1\n')  # 3 lines more
    edit(PRICING, "def unused(x):", "# docs\n\n\ndef unused(x):")  # 3 more, above unused
    tally, lived = mutation.evaluate(stats, "mutmut")
    assert tally.unlocated == ()
    assert [m.line for m in lived] == [line + (3 if line < 24 else 6) for line, _, _ in SURVIVORS]


def test_survivors_are_placed_on_the_lines_of_the_file_as_it_is_now(tmp_path, monkeypatch):
    stats = mutmut_tree(tmp_path, monkeypatch)
    edit(PRICING, '"""Pricing."""\n', '"""Pricing."""\n\n\nOTHER = 1\n')  # 3 lines more
    assert mutation.evaluate(stats, "mutmut", {PRICING: [(15, 15)]})[1] == []
    lived = mutation.evaluate(stats, "mutmut", {PRICING: [(18, 18)]})[1]
    assert where(lived) == [(PRICING, 18, "survived", "shop.pricing.x_total__mutmut_1")]


def test_every_survivor_is_named_when_the_source_is_gone(tmp_path, monkeypatch):
    stats = mutmut_tree(tmp_path, monkeypatch)
    Path(PRICING).unlink()
    tally, lived = mutation.evaluate(stats, "mutmut")
    assert lived == []
    assert tally.unlocated == tuple(key for _, _, key in SURVIVORS)


@pytest.mark.parametrize("target", [PRICING, f"mutants/{PRICING}"])
@pytest.mark.parametrize("content", [b"def broken(:\n", b"x = 1\n\x00", b"\xff\xfe", b""])
def test_every_survivor_is_named_when_a_file_cannot_be_parsed(
    tmp_path, monkeypatch, target, content
):
    stats = mutmut_tree(tmp_path, monkeypatch)
    Path(target).write_bytes(content)
    tally, lived = mutation.evaluate(stats, "mutmut")
    assert lived == []
    assert tally.unlocated == tuple(key for _, _, key in SURVIVORS)


def test_every_survivor_is_named_when_the_mutated_file_is_gone(tmp_path, monkeypatch):
    stats = mutmut_tree(tmp_path, monkeypatch)
    Path(f"mutants/{PRICING}").unlink()
    tally, lived = mutation.evaluate(stats, "mutmut")
    assert (lived, len(tally.unlocated)) == ([], len(SURVIVORS))


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("def x_total__mutmut_orig(", "def x_total__mutmut_original("),  # no verbatim copy
        ("def x_total__mutmut_1(", "def x_total__mutmut_one("),  # no such mutant
        ("if a >= b:", "if a > b:"),  # a mutant identical to the original
    ],
)
def test_survivors_are_named_when_the_mutated_file_lacks_the_change(
    tmp_path, monkeypatch, old, new
):
    stats = mutmut_tree(tmp_path, monkeypatch)
    edit(f"mutants/{PRICING}", old, new)
    tally, lived = mutation.evaluate(stats, "mutmut")
    assert tally.unlocated == ("shop.pricing.x_total__mutmut_1",)
    assert len(lived) == len(SURVIVORS) - 1


def test_a_key_that_is_not_a_mutant_is_named(tmp_path, monkeypatch):
    stats = mutmut_tree(tmp_path, monkeypatch)
    meta = Path(f"mutants/{PRICING}.meta")
    data = json.loads(meta.read_text(encoding="utf-8"))
    data["exit_code_by_key"] = {"shop.pricing.helper": 0, "shop.pricing.x_total__mutmut_1": 0}
    meta.write_text(json.dumps(data), encoding="utf-8")
    tally, lived = mutation.evaluate(stats, "mutmut")
    assert tally.unlocated == ("shop.pricing.helper",)
    assert [m.line for m in lived] == [15]


def test_unplaced_survivors_are_named_when_their_file_is_in_scope(tmp_path, monkeypatch):
    stats = mutmut_tree(tmp_path, monkeypatch)
    edit(PRICING, "if qty >= 50:", "if qty >= 49:")
    in_scope = mutation.evaluate(stats, "mutmut", {PRICING: [(15, 15)]})
    assert [m.line for m in in_scope[1]] == [15]
    assert in_scope[0].unlocated == (
        "shop.pricing.x_discount__mutmut_1",
        "shop.pricing.x_discount__mutmut_2",
    )
    out_of_scope = mutation.evaluate(stats, "mutmut", {"src/other.py": [(1, 99)]})
    assert out_of_scope[0].unlocated == ()


MALFORMED = [
    b"not json",
    b"",
    b"[]",
    b"{}",
    b"null",
    b'{"exit_code_by_key": null}',
    b'{"exit_code_by_key": []}',
    b'{"exit_code_by_key": "x"}',
    b'{"exit_code_by_key": {"k": "0"}}',
    b'{"exit_code_by_key": {"k": true}}',
    b'{"exit_code_by_key": {"k": 0.0}}',
    b'{"exit_code_by_key": {"k": [0]}}',
    b"\xff\xfe\x00",
]


@pytest.mark.parametrize("content", MALFORMED)
def test_a_malformed_meta_never_changes_the_verdict(tmp_path, monkeypatch, content):
    stats = mutmut_tree(tmp_path, monkeypatch)
    Path(f"mutants/{PRICING}.meta").write_bytes(content)
    tally, lived = mutation.evaluate(stats, "mutmut")
    assert lived == []
    assert tally == FIXTURE_TALLY
    assert tally.unlocated == ()
    assert tally.locate_failure == "mutants/src/shop/pricing.py.meta is unreadable or malformed"


def test_an_unreadable_meta_never_changes_the_verdict(tmp_path, monkeypatch):
    stats = mutmut_tree(tmp_path, monkeypatch)
    meta = Path(f"mutants/{PRICING}.meta")
    meta.unlink()
    meta.mkdir()  # reading a directory fails with an OSError
    tally, lived = mutation.evaluate(stats, "mutmut")
    assert (lived, tally) == ([], FIXTURE_TALLY)
    assert tally.locate_failure == "mutants/src/shop/pricing.py.meta is unreadable or malformed"


def test_one_malformed_meta_hides_every_survivor(tmp_path, monkeypatch):
    stats = mutmut_tree(tmp_path, monkeypatch, PRICING, "src/other.py")
    Path("mutants/src/other.py.meta").write_text("{", encoding="utf-8")
    tally, lived = mutation.evaluate(stats, "mutmut")
    assert lived == []  # a partial list would read as complete
    assert tally.locate_failure == "mutants/src/other.py.meta is unreadable or malformed"


def test_a_malformed_meta_of_a_file_out_of_scope_is_not_read(tmp_path, monkeypatch):
    stats = mutmut_tree(tmp_path, monkeypatch, PRICING, "src/other.py")
    Path("mutants/src/other.py.meta").write_text("{", encoding="utf-8")
    tally, lived = mutation.evaluate(stats, "mutmut", {PRICING: [(8, 8)]})
    assert [m.line for m in lived] == [8, 8]
    assert tally.locate_failure == ""


def test_survivors_are_not_looked_for_outside_a_mutants_directory(tmp_path, monkeypatch):
    mutmut_tree(tmp_path, monkeypatch)
    shutil.copy(STATS, "report.json")
    tally, lived = mutation.evaluate(Path("report.json"), "mutmut")
    assert (lived, tally) == ([], FIXTURE_TALLY)
    assert tally.locate_failure == "the report is not in a mutants/ directory"


def test_a_mutants_directory_without_results(tmp_path, monkeypatch):
    stats = mutmut_tree(tmp_path, monkeypatch)
    Path(f"mutants/{PRICING}.meta").unlink()
    tally, lived = mutation.evaluate(stats, "mutmut")
    assert (lived, tally) == ([], FIXTURE_TALLY)
    assert tally.locate_failure == "mutants/ has no .meta files"


@pytest.mark.parametrize(
    "stats",
    [{"killed": 5}, {"killed": 3, "skipped": 4, "timeout": 1}, {"killed": 0}],
)
def test_nothing_to_locate_is_nothing_to_say(tmp_path, monkeypatch, stats):
    monkeypatch.chdir(tmp_path)
    Path("report.json").write_text(json.dumps(stats), encoding="utf-8")
    tally, lived = mutation.evaluate(Path("report.json"), "mutmut")
    assert (lived, tally.locate_failure, tally.survivors_note) == ([], "", "")


@pytest.mark.parametrize(
    "stats",
    [
        {"survived": 1},
        {"no_tests": 1},
        {"survived": 2, "killed": 9},
        {"survived": 1, "no_tests": 1},
    ],
)
def test_survivors_that_cannot_be_located_are_said_so(tmp_path, monkeypatch, stats):
    monkeypatch.chdir(tmp_path)
    Path("report.json").write_text(json.dumps(stats), encoding="utf-8")
    tally = mutation.evaluate(Path("report.json"), "mutmut")[0]
    assert tally.locate_failure == "the report is not in a mutants/ directory"


def test_the_stats_file_still_fails_closed_with_a_tree(tmp_path, monkeypatch):
    stats = mutmut_tree(tmp_path, monkeypatch)
    stats.write_text(json.dumps({"killed": 1, "check_was_interrupted_by_user": 1}))
    with pytest.raises(ValueError, match="interrupted"):
        mutation.evaluate(stats, "mutmut")
    stats.write_text(json.dumps({"killed": "many"}))
    with pytest.raises(ValueError, match="malformed mutmut report"):
        mutation.evaluate(stats, "mutmut")


def test_other_formats_have_no_survivor_notes(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    tally = mutation.evaluate(write(tmp_path, gremlins("LIVED")), "gremlins")[0]
    assert (tally.unlocated, tally.locate_failure, tally.survivors_note) == ((), "", "")


def test_survivors_note():
    assert mutation.Tally().survivors_note == ""
    assert mutation.Tally(locate_failure="because").survivors_note == (
        "\n\nSurviving mutants could not be located (because); "
        "the verdict rests on mutmut's stats file alone."
    )
    assert mutation.Tally(unlocated=("a.x_f__mutmut_1", "a.x_g__mutmut_2")).survivors_note == (
        "\n\nSurviving mutants without a source line (not annotated):\n\n"
        "- `a.x_f__mutmut_1`\n- `a.x_g__mutmut_2`"
    )
    both = mutation.Tally(locate_failure="why", unlocated=("k",)).survivors_note
    assert both.startswith("\n\nSurviving mutants could not be located (why);")
    assert both.endswith("\n\n- `k`")


def test_survivors_note_lists_at_most_fifty_names():
    note = mutation.Tally(unlocated=tuple(f"k{n}" for n in range(60))).survivors_note
    assert note.count("\n- `") == 50
    assert "- `k49`" in note
    assert "k50" not in note


def test_survivor_notes_are_no_part_of_the_tally_identity():
    plain = mutation.Tally(killed=1, survived=1)
    noted = mutation.Tally(killed=1, survived=1, unlocated=("k",), locate_failure="why")
    assert plain == noted
    assert hash(plain) == hash(noted)
    assert plain != mutation.Tally(killed=1, survived=2)


def test_exit_codes_map_to_statuses():
    assert mutation.MUTMUT_LIVED == {
        0: mutation.SURVIVED,
        5: mutation.NO_COVERAGE,
        33: mutation.NO_COVERAGE,
    }


# ----------------------------------------------------------- _first_change, _defs
def func(start, *lines):
    return (start, list(lines))


SOURCE = {("", "f"): func(10, "def f(x):", "    y = x", "    return y + 1")}


def mutated(*mutant_lines, owner=""):
    return {
        (owner, "x_f__mutmut_orig"): func(
            3, "def x_f__mutmut_orig(x):", "    y = x", "    return y + 1"
        ),
        (owner, "x_f__mutmut_1"): func(8, "def x_f__mutmut_1(x):", *mutant_lines),
    }


@pytest.mark.parametrize(
    ("mutant_lines", "line"),
    [
        (("    y = None", "    return y + 1"), 11),
        (("    y = x", "    return y - 1"), 12),
        (("    y = x",), 12),  # the mutant lost its last line
        ((), 11),
        (("    y = x", "    return y + 1", "    extra"), None),  # only a line more
        (("    y = x", "    return y + 1"), None),  # identical to the original
    ],
)
def test_first_change(mutant_lines, line):
    assert mutation._first_change("a.b.x_f__mutmut_1", SOURCE, mutated(*mutant_lines)) == line


def test_first_change_on_the_def_line():
    source = {("", "f"): func(10, "def f(x=1):", "    return x")}
    mutants = {
        ("", "x_f__mutmut_orig"): func(3, "def x_f__mutmut_orig(x=1):", "    return x"),
        ("", "x_f__mutmut_1"): func(7, "def x_f__mutmut_1(x=2):", "    return x"),
    }
    assert mutation._first_change("m.x_f__mutmut_1", source, mutants) == 10


def test_first_change_renames_only_the_name_on_the_def_line():
    source = {("", "f"): func(4, 'def f(a="x_f__mutmut_orig"):', "    return a")}
    mutants = {
        ("", "x_f__mutmut_orig"): func(
            1, 'def x_f__mutmut_orig(a="x_f__mutmut_orig"):', "    return a"
        ),
        ("", "x_f__mutmut_1"): func(
            9, 'def x_f__mutmut_1(a="x_f__mutmut_orig"):', "    return None"
        ),
    }
    assert mutation._first_change("m.x_f__mutmut_1", source, mutants) == 5


@pytest.mark.parametrize(
    ("owner", "name", "mangled"),
    [
        ("", "_mutmut_f", "x__mutmut_f"),
        ("C", "_mutmut_f", "xǁCǁ_mutmut_f"),
        ("", "f__mutmut_2", "x_f__mutmut_2"),
    ],
)
def test_first_change_of_a_function_named_like_a_mutant(owner, name, mangled):
    # mutmut itself cannot tell such a name from the mutant suffix; the key still can be read.
    indent = "    " if owner else ""
    source = {(owner, name): func(10, f"{indent}def {name}(x):", f"{indent}    return 1")}
    mutants = {
        (owner, f"{mangled}__mutmut_orig"): func(
            3, f"{indent}def {mangled}__mutmut_orig(x):", f"{indent}    return 1"
        ),
        (owner, f"{mangled}__mutmut_7"): func(
            8, f"{indent}def {mangled}__mutmut_7(x):", f"{indent}    return 2"
        ),
    }
    assert mutation._first_change(f"m.{mangled}__mutmut_7", source, mutants) == 11


def test_first_change_of_a_method():
    source = {("C", "m"): func(20, "    def m(self):", "        return 1")}
    mutants = {
        ("C", "xǁCǁm__mutmut_orig"): func(
            5, "    def xǁCǁm__mutmut_orig(self):", "        return 1"
        ),
        ("C", "xǁCǁm__mutmut_2"): func(9, "    def xǁCǁm__mutmut_2(self):", "        return 2"),
    }
    assert mutation._first_change("pkg.mod.xǁCǁm__mutmut_2", source, mutants) == 21
    # Same name, other class: not the function mutmut mutated.
    assert mutation._first_change("pkg.mod.xǁDǁm__mutmut_2", source, mutants) is None


@pytest.mark.parametrize(
    "key",
    [
        "a.b.x_f__mutmut_",
        "a.b.x_f__mutmut_one",
        "a.b.x_f__mutmut_٣",  # not an ASCII digit
        "a.b.x_f__mutmut_1 ",
        "a.b.x_f__mutmut_1x",
        "a.b.xx_f__mutmut_1",
        "a.b.y_f__mutmut_1",
        "a.b.xf__mutmut_1",
        "a.b.x_f_mutmut_1",
        "a.b.xǁfǁ__mutmut_1",
    ],
)
def test_first_change_ignores_keys_that_are_not_mutants(key):
    # The mutated file has a function of that very name, so only the key's shape can refuse it.
    generated = key.rpartition(".")[2]
    both = mutated("    y = None")
    both[("", generated)] = both[("", "x_f__mutmut_1")]
    assert mutation._first_change(key, SOURCE, both) is None


@pytest.mark.parametrize("drop", ["source", "orig", "mutant"])
def test_first_change_needs_all_three_functions(drop):
    source, both = dict(SOURCE), mutated("    y = None", "    return y + 1")
    if drop == "source":
        source = {}
    else:
        del both[("", "x_f__mutmut_orig" if drop == "orig" else "x_f__mutmut_1")]
    assert mutation._first_change("a.b.x_f__mutmut_1", source, both) is None


def test_first_change_needs_the_copy_to_be_the_source():
    other = {("", "f"): func(10, "def f(x):", "    y = x", "    return y + 2")}
    assert mutation._first_change("a.x_f__mutmut_1", other, mutated("    y = None")) is None
    longer = {("", "f"): func(10, "def f(x):", "    y = x", "    return y + 1", "    z")}
    assert mutation._first_change("a.x_f__mutmut_1", longer, mutated("    y = None")) is None


def test_first_change_ignores_the_module_part_of_the_key():
    expected = mutation._first_change("x_f__mutmut_1", SOURCE, mutated("    y = None"))
    assert expected == 11
    assert mutation._first_change("a.b.c.x_f__mutmut_1", SOURCE, mutated("    y = None")) == 11


DEFS = """import os
@deco
@other(1)
def a():
    def inner():
        pass
    return 1

async def b(): ...
class C:
    x = 1

    @staticmethod
    def m(self):
        return 2

    class D:
        def n(self):
            pass
if os.name:
    def cond():
        pass
def multi(x,
          y):
    return x
"""


def test_defs(tmp_path):
    path = tmp_path / "m.py"
    path.write_text(DEFS, encoding="utf-8")
    assert mutation._defs(path) == {
        ("", "a"): (4, ["def a():", "    def inner():", "        pass", "    return 1"]),
        ("", "b"): (9, ["async def b(): ..."]),
        ("C", "m"): (14, ["    def m(self):", "        return 2"]),
        ("", "multi"): (23, ["def multi(x,", "          y):", "    return x"]),
    }


@pytest.mark.parametrize(
    "content",
    [b"", b"x = 1\n", b"def f(:\n", b"def f():\n    pass\n\x00", b"\xff\xfe", b"class C:\n"],
)
def test_defs_of_a_file_that_is_not_python_functions(tmp_path, content):
    path = tmp_path / "m.py"
    path.write_bytes(content)
    assert mutation._defs(path) == {}


def test_defs_of_a_missing_file(tmp_path):
    assert mutation._defs(tmp_path / "missing.py") == {}
    assert mutation._defs(tmp_path) == {}


def test_defs_reads_lines_as_python_does(tmp_path):
    # A form feed or a Unicode line separator is no line break to Python, nor to its line numbers.
    path = tmp_path / "m.py"
    path.write_bytes(
        b"x = 1  # \x0c \xe2\x80\xa8 \xc2\x85 \xc2\xa0\r\ndef f():\r\n    return 1\r\n"
    )
    assert mutation._defs(path) == {("", "f"): (2, ["def f():", "    return 1"])}


# ------------------------------------------------------------- found by tests/test_fuzz.py
DEEP = "[" * 100_000 + "]" * 100_000


def write_text(tmp_path, content, name="report.json"):
    path = tmp_path / name
    path.write_bytes(content if isinstance(content, bytes) else content.encode())
    return path


@pytest.mark.parametrize("fmt", mutation.PARSERS)
@pytest.mark.parametrize(
    "content",
    [
        b"",
        b"{",
        b"not json",
        b"\xff\xfe",
        b"\xef\xbb\xbf{}",
        pytest.param(DEEP, id="deeply-nested"),
    ],
)
def test_a_report_that_is_not_json_is_a_value_error_that_names_it(tmp_path, fmt, content):
    path = write_text(tmp_path, content)
    with pytest.raises(
        ValueError, match=r"mutation report .*report\.json is not valid JSON"
    ) as raised:
        mutation.evaluate(path, fmt)
    assert type(raised.value) is ValueError


def one_stryker(line=1, status="Survived", **fields):
    mutant = {"status": status, "mutatorName": "M", "location": {"start": {"line": line}}}
    return {"files": {"a.ts": {"mutants": [{**mutant, **fields}]}}}


def one_gremlins(line=1, status="LIVED", **fields):
    mutation_ = {"line": line, "type": "T", "status": status, **fields}
    return {"files": [{"file_name": "a.go", "mutations": [mutation_]}]}


@pytest.mark.parametrize(("fmt", "make"), [("stryker", one_stryker), ("gremlins", one_gremlins)])
@pytest.mark.parametrize("line", [0, -1, 1.0, 1.5, "1", True, None, []])
def test_a_mutant_that_is_not_on_a_line_is_an_error(tmp_path, fmt, make, line):
    with pytest.raises(ValueError, match=f"malformed {fmt} report .*is not a line number"):
        mutation.evaluate(write(tmp_path, make(line=line)), fmt)


@pytest.mark.parametrize("line", ["Infinity", "1e999", "-Infinity"])
@pytest.mark.parametrize("fmt", ["stryker", "gremlins"])
def test_a_line_that_is_infinite_is_an_error_not_an_overflow(tmp_path, fmt, line):
    # int(float("inf")) is an OverflowError.
    text = json.dumps(one_stryker() if fmt == "stryker" else one_gremlins())
    path = write_text(tmp_path, text.replace('"line": 1', f'"line": {line}'))
    with pytest.raises(ValueError, match=f"malformed {fmt} report .*is not a line number"):
        mutation.evaluate(path, fmt)


def test_the_first_line_is_a_line(tmp_path):
    for fmt, data in (("stryker", one_stryker(line=1)), ("gremlins", one_gremlins(line=1))):
        _, lived = mutation.evaluate(write(tmp_path, data), fmt)
        assert [m.line for m in lived] == [1]


@pytest.mark.parametrize("status", [None, 5, True, [], ["Survived"], {"a": 1}])
@pytest.mark.parametrize("fmt", ["stryker", "gremlins"])
def test_a_status_that_is_not_a_string_cannot_hide_a_survivor(tmp_path, fmt, status):
    # It used to count as "no verdict" and drop the survivor from the score.
    data = one_stryker(status=status) if fmt == "stryker" else one_gremlins(status=status)
    with pytest.raises(ValueError, match=f"malformed {fmt} report .*status .* is not a string"):
        mutation.evaluate(write(tmp_path, data), fmt)


@pytest.mark.parametrize("fmt", ["stryker", "gremlins"])
def test_a_status_the_tool_defines_but_scores_nothing_is_still_no_verdict(tmp_path, fmt):
    data = one_stryker(status="Whatever") if fmt == "stryker" else one_gremlins(status="WHATEVER")
    tally, lived = mutation.evaluate(write(tmp_path, data), fmt)
    assert (tally, lived) == (mutation.Tally(invalid=1), [])


@pytest.mark.parametrize("name", ["mutatorName", "type"])
def test_a_mutator_name_that_is_not_a_string_is_an_error(tmp_path, name):
    fmt = "stryker" if name == "mutatorName" else "gremlins"
    data = one_stryker(**{name: 5}) if fmt == "stryker" else one_gremlins(**{name: ["x"]})
    with pytest.raises(ValueError, match=f"malformed {fmt} report .*{name} .* is not a string"):
        mutation.evaluate(write(tmp_path, data), fmt)


@pytest.mark.parametrize(
    ("fmt", "data"),
    [
        ("stryker", {}),
        ("stryker", {"thresholds": {}}),
        ("stryker", {"files": {"a.ts": {}}}),
        ("stryker", {"files": {"a.ts": {"mutations": []}}}),
        ("mutation-testing-elements", {}),
        ("gremlins", {}),
        ("gremlins", {"go_module": "m"}),
        ("gremlins", {"files": [{"file_name": "a.go"}]}),
        ("gremlins", {"files": [{"file_name": "a.go", "mutants": []}]}),
    ],
)
def test_a_report_without_its_files_or_mutants_is_not_a_report_without_mutants(tmp_path, fmt, data):
    # A renamed key must not read as a clean run.
    with pytest.raises(ValueError, match=f"malformed {fmt} report"):
        mutation.evaluate(write(tmp_path, data), fmt)


@pytest.mark.parametrize(
    "data",
    [
        {"foo": 1},
        {},
        {"total": 12, "skipped": 3},  # not one of the counts the score is made of
        [],
        None,
        "killed",
    ],
)
def test_mutmut_stats_without_any_count_are_an_error(tmp_path, data):
    with pytest.raises(ValueError, match="malformed mutmut report"):
        mutation.evaluate(write(tmp_path, data), "mutmut")


@pytest.mark.parametrize("data", [{}, {"foo": 1}, {"total": 12, "skipped": 3}, []])
def test_the_reason_mutmut_stats_are_refused_is_named(tmp_path, data):
    with pytest.raises(ValueError, match=r"malformed mutmut report ") as raised:
        mutation.evaluate(write(tmp_path, data), "mutmut")
    assert str(raised.value.__cause__) == "none of mutmut's counts is in the report"


@pytest.mark.parametrize("key", mutation.MUTMUT_COUNTS)
@pytest.mark.parametrize("value", [-1, "1", 1.0, 1.5, True, None, [1], float("inf")])
def test_a_mutmut_count_must_be_a_whole_number_from_zero(tmp_path, key, value):
    path = write(tmp_path, {"killed": 5, "survived": 1, key: value})
    with pytest.raises(ValueError, match=rf"malformed mutmut report .*{key} is .*, not a count"):
        mutation.evaluate(path, "mutmut")


def test_mutmut_counts_that_are_zero_or_absent_are_fine(tmp_path):
    stats = dict.fromkeys(mutation.MUTMUT_COUNTS, 0)
    tally, _ = mutation.evaluate(write(tmp_path, stats), "mutmut")
    assert tally == mutation.Tally()
    tally, _ = mutation.evaluate(write(tmp_path, {"survived": 4}), "mutmut")
    assert tally == mutation.Tally(survived=4)


# The survivors are an extra: whatever mutants/ holds, the verdict is the stats file's.
def test_a_meta_file_nested_too_deeply_is_a_note_not_a_crash(tmp_path, monkeypatch):
    stats = mutmut_tree(tmp_path, monkeypatch)
    Path(f"mutants/{PRICING}.meta").write_text(DEEP, encoding="utf-8")
    tally, lived = mutation.evaluate(stats, "mutmut")
    assert (lived, tally) == ([], FIXTURE_TALLY)
    assert tally.locate_failure == f"mutants/{PRICING}.meta is unreadable or malformed"


@pytest.mark.parametrize(
    "body",
    [
        "    return " + "1" + " + 1" * 100_000,  # a deep AST: RecursionError in the parser
        "    return " + "-" * 100_000 + "1",
        "    return " + "(" * 300 + "1" + ")" * 300,  # SyntaxError: too many nested parentheses
    ],
    ids=["sum", "negations", "parentheses"],
)
def test_a_source_file_python_cannot_parse_leaves_the_survivors_unplaced(
    tmp_path, monkeypatch, body
):
    stats = mutmut_tree(tmp_path, monkeypatch)
    Path(PRICING).write_text(f"def discount(a):\n{body}\n", encoding="utf-8")
    tally, lived = mutation.evaluate(stats, "mutmut")
    assert lived == []
    assert tally.survived == FIXTURE_TALLY.survived
    assert tally.unlocated
    assert tally.locate_failure == ""


@pytest.mark.parametrize("trailer", [" ", "\t", "\r", " " * 40_000])
def test_trailing_whitespace_does_not_change_what_is_a_logging_call(trailer):
    assert mutation.is_arid_line("console.log(x);" + trailer, mutation.JS_ARID)
    assert mutation.is_arid_line("console.log(x)" + trailer, mutation.JS_ARID)
    assert mutation.is_arid_line("log.Println(x)" + trailer, mutation.GO_ARID)
    assert not mutation.is_arid_line("console.log(x); y()" + trailer, mutation.JS_ARID)


def test_a_long_run_of_spaces_after_a_call_does_not_stall_the_arid_check():
    # `\)\s*;?\s*$` backtracked quadratically: 30,000 spaces took 20 seconds, a few seconds
    # of CI for each such line a pull request adds. Now it takes milliseconds.
    started = time.perf_counter()
    line = "console.log(a)" + " " * 30_000 + "x"
    assert not mutation.is_arid_line(line, mutation.JS_ARID)
    assert not mutation.is_arid_line(
        "console.log(" + ")" * 30_000 + " " * 30_000 + "x", mutation.JS_ARID
    )
    assert time.perf_counter() - started < 2


@pytest.mark.parametrize(
    ("fmt", "data"),
    [
        (
            "stryker",
            {
                "files": {
                    "a.ts": {
                        "mutants": [{"status": "Survived", "location": {"start": {"line": 3}}}]
                    }
                }
            },
        ),
        (
            "gremlins",
            {"files": [{"file_name": "a.go", "mutations": [{"line": 3, "status": "LIVED"}]}]},
        ),
    ],
)
def test_a_mutant_without_a_mutator_name_has_an_empty_one(tmp_path, fmt, data):
    _, lived = mutation.evaluate(write(tmp_path, data), fmt)
    assert [(m.line, m.mutator) for m in lived] == [(3, "")]
