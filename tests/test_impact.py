"""golden.impact: the source a change puts at stake, test edits included."""

import pytest

from golden import impact, presets

PY = presets.get("python")
GO = presets.get("go")

MOD = "def rate(q):\n    return 2 if q > 10 else 1\n\n\ndef fee():\n    return 1\n"
TEST = "from pkg.mod import rate\n\n\ndef test_rate():\n    assert rate(11) == 2\n"


@pytest.mark.parametrize(
    ("ranges", "merged"),
    [
        ([], []),
        ([(5, 9), (1, 2)], [(1, 2), (5, 9)]),
        ([(1, 2), (3, 4)], [(1, 4)]),
        ([(1, 5), (2, 3)], [(1, 5)]),
        ([(1, 2), (4, 5)], [(1, 2), (4, 5)]),
    ],
)
def test_merge(ranges, merged):
    assert impact._merge(ranges) == merged


def files(**texts):
    return lambda path: texts.get(path.replace("/", "_").replace(".", "_"), "")


def test_tested_functions_names_only_what_the_edit_mentions():
    read = files(pkg_mod_py=MOD, tests_test_mod_py=TEST)
    edits = {"tests/test_mod.py": "    assert rate(11) == 2\n    rate(11)"}
    assert impact.tested_functions(PY, edits, ["pkg/mod.py"], read) == {"pkg/mod.py": [(1, 2)]}


def test_tested_functions_needs_the_test_to_reference_the_source():
    read = files(pkg_mod_py=MOD, other_py=MOD, tests_test_mod_py=TEST)
    edits = {"tests/test_mod.py": "rate(11)"}
    assert impact.tested_functions(PY, edits, ["other.py"], read) == {}


def test_tested_functions_of_a_deleted_test_use_its_removed_lines():
    read = files(pkg_mod_py=MOD)  # the test file no longer exists
    edits = {"tests/test_mod.py": "from pkg.mod import rate, fee\nassert rate(1) == fee()"}
    assert impact.tested_functions(PY, edits, ["pkg/mod.py"], read) == {
        "pkg/mod.py": [(1, 2), (5, 6)]
    }


def test_tested_functions_skip_edits_without_names_or_matches():
    read = files(pkg_mod_py=MOD, tests_test_mod_py=TEST)
    assert impact.tested_functions(PY, {"tests/test_mod.py": "== 2"}, ["pkg/mod.py"], read) == {}
    edits = {"tests/test_mod.py": "unrelated_helper()"}
    assert impact.tested_functions(PY, edits, ["pkg/mod.py"], read) == {}


def test_tested_functions_union_over_several_tests():
    go_src = "package p\n\nfunc A() int { return 1 }\n\nfunc B() int {\n\treturn 2\n}\n"
    read = files(p_p_go=go_src, p_a_test_go="package p", p_b_test_go="package p")
    edits = {"p/a_test.go": "A()", "p/b_test.go": "B()", "q/c_test.go": "A() B()"}
    assert impact.tested_functions(GO, edits, ["p/p.go"], read) == {"p/p.go": [(3, 3), (5, 7)]}


def test_mutation_lines_without_test_edits_are_the_changed_lines(repo):
    repo.commit({"pkg/mod.py": MOD})
    base = repo.git("rev-parse", "HEAD")
    repo.commit({"pkg/mod.py": MOD.replace("return 1\n", "return 3\n")})
    assert impact.mutation_lines(base, PY, ()) == {"pkg/mod.py": [(6, 6)]}


def test_mutation_lines_add_functions_a_weakened_test_names(repo):
    repo.commit({"pkg/mod.py": MOD, "tests/test_mod.py": TEST})
    base = repo.git("rev-parse", "HEAD")
    repo.commit({"tests/test_mod.py": TEST.replace("assert rate(11) == 2", "rate(11)")})
    assert impact.mutation_lines(base, PY, ()) == {
        "tests/test_mod.py": [(5, 5)],
        "pkg/mod.py": [(1, 2)],
    }


def test_mutation_lines_merge_with_changed_source_lines(repo):
    repo.commit({"pkg/mod.py": MOD, "tests/test_mod.py": TEST})
    base = repo.git("rev-parse", "HEAD")
    repo.commit(
        {
            "pkg/mod.py": MOD.replace("return 1\n", "return 3\n"),
            "tests/test_mod.py": TEST + "    assert fee() == 3\n",
        }
    )
    lines = impact.mutation_lines(base, PY, ())
    assert lines["pkg/mod.py"] == [(5, 6)]


def test_mutation_lines_respect_exemptions(repo):
    repo.commit({"pkg/mod.py": MOD, "tests/test_mod.py": TEST})
    base = repo.git("rev-parse", "HEAD")
    repo.commit({"tests/test_mod.py": TEST.replace("== 2", "== 2 or True")})
    assert "pkg/mod.py" not in impact.mutation_lines(base, PY, ("pkg/**",))
