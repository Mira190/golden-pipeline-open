from golden import gitdiff, presence, presets

PY = presets.get("python")


def run(changed, texts=None, exempt=()):
    texts = texts or {}
    return presence.evaluate(changed, PY, lambda p: texts.get(p, ""), exempt)


def test_source_without_tests():
    assert run(["src/orders/pricing.py"]) == [
        presence.Finding("src/orders/pricing.py", "no test files changed")
    ]


def test_unrelated_test_does_not_count():
    findings = run(
        ["src/orders/pricing.py", "tests/test_api.py"],
        {"tests/test_api.py": "from orders.api import app"},
    )
    assert [f.reason for f in findings] == ["no changed test references this file"]


def test_referencing_test_satisfies_gate():
    assert (
        run(
            ["src/orders/pricing.py", "tests/test_pricing.py"],
            {"tests/test_pricing.py": "from orders.pricing import quote"},
        )
        == []
    )


def test_each_source_is_checked_independently():
    findings = run(
        ["src/orders/api.py", "src/orders/pricing.py", "tests/test_pricing.py"],
        {"tests/test_pricing.py": "from orders.pricing import quote"},
    )
    assert [f.source for f in findings] == ["src/orders/api.py"]


def test_non_source_and_exempt_files_are_ignored():
    assert run(["README.md", "docs/conf.py", "src/gen/x.py"], exempt=("src/gen/**",)) == []


def test_test_only_change_is_fine():
    assert run(["tests/test_pricing.py"]) == []


def test_read_head_tolerates_missing_and_binary_files(tmp_path):
    (tmp_path / "bin").write_bytes(b"\xff\xfe\x00")
    assert gitdiff.read_head("missing.py", cwd=tmp_path) == ""
    assert gitdiff.read_head("bin", cwd=tmp_path) == ""
