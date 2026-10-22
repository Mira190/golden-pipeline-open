import pytest
from hypothesis import given
from hypothesis import strategies as st

from golden.globmatch import match, match_any


@pytest.mark.parametrize(
    ("path", "pattern", "expected"),
    [
        ("a.py", "*.py", True),
        ("src/pkg/a.py", "*.py", True),
        ("src/pkg/a.py", "src/*.py", False),
        ("src/pkg/a.py", "src/**/*.py", True),
        ("src/a.py", "src/**/*.py", True),
        ("tests/unit/test_a.py", "tests/**", True),
        ("tests", "tests/**", False),
        ("node_modules/x/index.js", "node_modules/", True),
        ("a/node_modules/x.js", "node_modules/", True),
        ("src/a.test.ts", "*.test.{ts,tsx}", True),
        ("src/a.test.tsx", "*.test.{ts,tsx}", True),
        ("src/a.test.js", "*.test.{ts,tsx}", False),
        ("a.py", "?.py", True),
        ("ab.py", "?.py", False),
        ("a/b.py", "a?b.py", False),
        ("x{y.py", "x{y.py", True),
        ("./src/a.py", "./src/a.py", False),
        ("src/a.py", "./src/a.py", True),
        ("a.pyc", "*.py", False),
    ],
)
def test_match(path, pattern, expected):
    assert match(path, pattern) is expected


def test_match_any():
    assert match_any("x/y.go", ("*.py", "*.go"))
    assert not match_any("x/y.rs", ["*.py", "*.go"])


segment = st.text(alphabet="abcxyz_-", min_size=1, max_size=6)
paths = st.lists(segment, min_size=1, max_size=5).map("/".join)


@given(paths)
def test_double_star_matches_everything(path):
    assert match(path, "**")


@given(paths)
def test_literal_path_matches_itself(path):
    assert match(path, path)


@given(st.lists(segment, min_size=1, max_size=4))
def test_star_never_crosses_directories(segments):
    path = "x/" + "/".join(segments) + ".py"
    assert match(path, "x/*.py") is (len(segments) == 1)
    assert match(path, "x/**/*.py")
