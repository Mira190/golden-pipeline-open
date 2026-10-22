import pytest

from golden import gitdiff


def test_changed_lines(repo):
    repo.commit({"a.py": "1\n2\n3\n4\n5\n", "gone.py": "x\n", "same.py": "s\n"})
    base = repo.git("rev-parse", "HEAD")
    repo.commit({"a.py": "1\nTWO\n3\n4\n5\n6\n7\n", "new.py": "n\n"})
    repo.git("rm", "-q", "gone.py")
    repo.write({"a.py": "1\nTWO\n3\n4\n5\n6\n7\n"})
    repo.git("commit", "-qam", "rm")
    assert gitdiff.changed_lines(base) == {"a.py": [(2, 2), (6, 7)], "new.py": [(1, 1)]}


def test_pure_deletion_has_no_ranges(repo):
    repo.commit({"a.py": "1\n2\n3\n"})
    base = repo.git("rev-parse", "HEAD")
    repo.commit({"a.py": "1\n3\n"})
    assert gitdiff.changed_lines(base) == {}


def test_changed_files_are_relative_to_cwd(repo, monkeypatch):
    repo.commit({"README.md": "x"})
    base = repo.git("rev-parse", "HEAD")
    repo.commit({"svc/a.py": "a", "other/b.py": "b"})
    monkeypatch.chdir(repo.root / "svc")
    assert gitdiff.changed_files(base) == ["a.py"]
    assert gitdiff.changed_lines(base) == {"a.py": [(1, 1)]}
    assert gitdiff.repo_prefix() == "svc/"


def test_added_line_that_looks_like_a_header_stays_in_its_file(repo):
    # With -U0, an added line "++ x" is printed as "+++ x": it must not start a new file.
    repo.commit({"a.py": "".join(f"{i}\n" for i in range(1, 21))})
    base = repo.git("rev-parse", "HEAD")
    lines = [f"{i}\n" for i in range(1, 21)]
    lines.insert(1, "++ evil\n")
    lines[20] = "CHANGED\n"
    repo.commit({"a.py": "".join(lines)})
    assert gitdiff.changed_lines(base) == {"a.py": [(2, 2), (21, 21)]}


def test_removed_and_no_newline_lines_do_not_desync_hunks(repo):
    repo.commit({"a.py": "-- old\nkeep\nlast", "b.py": "b\n"})
    base = repo.git("rev-parse", "HEAD")
    repo.commit({"a.py": "+++ new\nkeep\nlast changed", "b.py": "b\nc\n"})
    assert gitdiff.changed_lines(base) == {"a.py": [(1, 1), (3, 3)], "b.py": [(2, 2)]}


def test_carriage_return_inside_a_line_does_not_split_it(repo):
    repo.commit({"a.py": "1\n2\n3\n"})
    base = repo.git("rev-parse", "HEAD")
    repo.commit({"a.py": "1\nx = '\r'\n3\n4\n"})
    assert gitdiff.changed_lines(base) == {"a.py": [(2, 2), (4, 4)]}


def test_unusual_file_names_match_between_files_and_lines(repo):
    repo.commit({"README.md": "x"})
    base = repo.git("rev-parse", "HEAD")
    repo.git("config", "core.quotePath", "true")
    repo.git("config", "diff.noprefix", "true")
    names = ["src/café.py", "src/my file.py", 'src/q"uote.py', "src/tab\there.py"]
    repo.commit(dict.fromkeys(names, "x\n"))
    assert gitdiff.changed_files(base) == sorted(names)
    assert gitdiff.changed_lines(base) == {n: [(1, 1)] for n in names}


def test_changed_files_honours_cwd_argument(repo):
    repo.commit({"README.md": "x"})
    base = repo.git("rev-parse", "HEAD")
    repo.commit({"svc/a.py": "a\n"})
    assert gitdiff.changed_files(base, cwd=repo.root / "svc") == ["a.py"]
    assert gitdiff.changed_lines(base, cwd=repo.root / "svc") == {"a.py": [(1, 1)]}


def test_git_failure_raises_git_error(repo):
    with pytest.raises(gitdiff.GitError, match=r"unknown revision|bad revision|ambiguous"):
        gitdiff.changed_files("does-not-exist")


@pytest.mark.parametrize(
    ("quoted", "plain"),
    [
        ("plain.py", "plain.py"),
        ('"caf\\303\\251.py"', "café.py"),
        ('"a\\tb\\\\c\\"d.py"', 'a\tb\\c"d.py'),
        ('"bell\\a.py"', "bell\a.py"),
        ('"', '"'),
    ],
)
def test_unquote(quoted, plain):
    assert gitdiff._unquote(quoted) == plain


def test_changed_text_has_added_and_removed_lines_and_deleted_files(repo):
    repo.commit({"t.py": "keep\nassert a(1)\nassert b(2)\n", "gone.py": "assert c(3)\n"})
    base = repo.git("rev-parse", "HEAD")
    repo.git("rm", "-q", "gone.py")
    repo.commit({"t.py": "keep\nb(2)\n", "new.py": "++ x\n"})
    assert gitdiff.changed_text(base) == {
        "t.py": "assert a(1)\nassert b(2)\nb(2)",
        "gone.py": "assert c(3)",
        "new.py": "++ x",
    }
    # changed_lines ignores the deletion and the removed lines.
    assert gitdiff.changed_lines(base) == {"t.py": [(2, 2)], "new.py": [(1, 1)]}


def test_changed_text_of_unusual_names(repo):
    repo.commit({"my test.py": "a\n", "tést.py": "b\n"})
    base = repo.git("rev-parse", "HEAD")
    repo.git("rm", "-q", "my test.py", "tést.py")
    repo.git("commit", "-qm", "rm")
    assert gitdiff.changed_text(base) == {"my test.py": "a", "tést.py": "b"}


def test_tracked_files_are_relative_to_cwd(repo, monkeypatch):
    repo.commit({"svc/a.py": "a", "svc/pkg/b.py": "b", "other/c.py": "c"})
    repo.write({"svc/untracked.py": "u"})
    monkeypatch.chdir(repo.root / "svc")
    assert gitdiff.tracked_files() == ["a.py", "pkg/b.py"]


def test_changed_anywhere_sees_the_whole_repository(repo, monkeypatch):
    repo.commit({".github/workflows/ci.yml": "x", "svc/golden.toml": "", "CODEOWNERS": "*"})
    base = repo.git("rev-parse", "HEAD")
    repo.git("rm", "-q", "CODEOWNERS")
    repo.commit({".github/workflows/ci.yml": "y", "svc/golden.toml": "[a]", "svc/x.py": "1"})
    monkeypatch.chdir(repo.root / "svc")
    specs = [".github/workflows", "CODEOWNERS", "svc/golden.toml", "golden.toml"]
    assert gitdiff.changed_anywhere(base, specs) == [
        ".github/workflows/ci.yml",
        "CODEOWNERS",
        "svc/golden.toml",
    ]
    assert gitdiff.changed_anywhere(base, ["*.py"]) == []  # literal, not a glob
