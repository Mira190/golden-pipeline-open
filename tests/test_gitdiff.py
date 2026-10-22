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
