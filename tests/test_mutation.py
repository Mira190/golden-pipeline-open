import json

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
    ("fmt", "data"), [("mutmut", {}), ("stryker", {"files": {}}), ("gremlins", {"files": []})]
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
