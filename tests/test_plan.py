import json
from pathlib import Path

import pytest

from golden import cli, config, plan

POLICY = config.from_mapping({})


def pr_event(base="b" * 40, labels=()):
    return plan.Event(
        "pull_request",
        {"pull_request": {"base": {"sha": base}, "labels": [{"name": n} for n in labels]}},
    )


@pytest.mark.parametrize(
    ("event", "base"),
    [
        (pr_event("abc"), "abc"),
        (plan.Event("merge_group", {"merge_group": {"base_sha": "def"}}), "def"),
        (plan.Event("push", {"before": "123"}), ""),
        (plan.Event("schedule", {}), ""),
    ],
)
def test_event_base(event, base):
    assert event.base() == base


def test_event_from_env(tmp_path, monkeypatch):
    path = tmp_path / "event.json"
    path.write_text(json.dumps({"pull_request": {"base": {"sha": "s"}, "labels": []}}))
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(path))
    monkeypatch.setenv("GITHUB_EVENT_NAME", "pull_request")
    assert plan.Event.from_env().base() == "s"
    monkeypatch.delenv("GITHUB_EVENT_PATH")
    monkeypatch.delenv("GITHUB_EVENT_NAME")
    assert plan.Event.from_env() == plan.Event("", {})


def test_defaults_and_overrides(tmp_path):
    cmds = plan.commands("python", {"test-command": "  make test  ", "lint-command": ""}, tmp_path)
    assert cmds["test-command"] == "make test"
    assert cmds["lint-command"] == plan.DEFAULTS["python"]["lint-command"]
    assert cmds["mutation-format"] == "mutmut"
    assert cmds["package-manager"] == ""


@pytest.mark.parametrize(
    ("lockfile", "install", "manager"),
    [
        (None, "npm ci --ignore-scripts", "npm"),
        ("pnpm-lock.yaml", "pnpm install --frozen-lockfile --ignore-scripts", "pnpm"),
        ("yarn.lock", "yarn install --immutable --mode=skip-build", "yarn"),
        ("bun.lock", "bun install --frozen-lockfile --ignore-scripts", "bun"),
    ],
)
def test_node_install_follows_lockfile(tmp_path, lockfile, install, manager):
    if lockfile:
        (tmp_path / lockfile).write_text("")
    cmds = plan.commands("node", {}, tmp_path)
    assert (cmds["install-command"], cmds["package-manager"]) == (install, manager)


def test_unknown_language(tmp_path):
    with pytest.raises(ValueError, match="unsupported language 'rust'"):
        plan.commands("rust", {}, tmp_path)


def test_bad_versions_matrix(tmp_path):
    with pytest.raises(json.JSONDecodeError):
        plan.commands("go", {"versions": "[3.12"}, tmp_path)


@pytest.mark.parametrize("versions", ["[]", '"3.12"', "{}"])
def test_versions_must_be_a_non_empty_list(tmp_path, versions):
    with pytest.raises(ValueError, match="non-empty JSON list"):
        plan.commands("python", {"versions": versions}, tmp_path)


def test_primary_version_is_the_last(tmp_path):
    assert (
        plan.commands("python", {"versions": '["3.11", "3.14"]'}, tmp_path)["primary-version"]
        == "3.14"
    )
    assert plan.commands("go", {}, tmp_path)["primary-version"] == "go.mod"


@pytest.mark.parametrize(
    ("language", "directory"), [("python", "."), ("node", "coverage"), ("go", ".")]
)
def test_coverage_dir(tmp_path, language, directory):
    assert plan.commands(language, {}, tmp_path)["coverage-dir"] == directory


@pytest.mark.parametrize(
    ("directory", "expected"),
    [
        (".", "root"),
        ("./", "root"),
        ("examples/python", "examples-python"),
        ("./svc/api/", "svc-api"),
        ("a b/c_d", "a-b-c-d"),
    ],
)
def test_slug(directory, expected):
    assert plan.slug(directory) == expected


def test_no_base_means_no_changes():
    assert plan.changes("python", "", POLICY) == {
        "tests": "",
        "test-packages": "",
        "mutate": "",
        "sources-changed": "false",
    }


def test_changes_python(repo):
    repo.commit({"src/pkg/a.py": "def f():\n    return 1\n\n\ndef g():\n    return 2\n"})
    base = repo.git("rev-parse", "HEAD")
    repo.commit(
        {
            "src/pkg/a.py": "def f():\n    return 1\n\n\ndef g():\n    return 3\n",
            "tests/test_a.py": "from pkg.a import g\n",
            "README.md": "x",
        }
    )
    assert plan.changes("python", base, POLICY) == {
        "tests": "tests/test_a.py",
        "test-packages": "./tests",
        "mutate": "pkg.a.x_g__mutmut_*",
        "sources-changed": "true",
    }


def test_changes_node_uses_comma_separated_ranges(repo):
    repo.commit({"src/a.ts": "export const a = 1;\n"})
    base = repo.git("rev-parse", "HEAD")
    repo.commit({"src/a.ts": "export const a = 2;\nexport const b = 3;\n", "src/b.ts": "x\n"})
    assert plan.changes("node", base, POLICY)["mutate"] == "src/a.ts:1-2,src/b.ts:1-1"


def test_changes_go_lists_test_packages(repo):
    repo.commit({"go.mod": "module m\n"})
    base = repo.git("rev-parse", "HEAD")
    repo.commit(
        {"p/p.go": "package p\n", "p/p_test.go": "package p\n", "q/q_test.go": "package q\n"}
    )
    result = plan.changes("go", base, POLICY)
    assert result["test-packages"] == "./p ./q"
    assert result["mutate"] == "./p"


def test_exempt_sources_are_not_mutated(repo):
    repo.commit({"README.md": "x"})
    base = repo.git("rev-parse", "HEAD")
    repo.commit({"gen/x.py": "def f():\n    return 1\n"})
    policy = config.from_mapping({"presence": {"exempt": ["gen/**"]}})
    assert plan.changes("python", base, policy)["sources-changed"] == "false"


def test_build_reports_label_and_repeats(repo):
    repo.commit({"README.md": "x"})
    base = repo.git("rev-parse", "HEAD")
    repo.commit({"README.md": "y"})
    outputs = plan.build(
        "python",
        {"skip-presence-label": "no-tests-needed"},
        pr_event(base, labels=["no-tests-needed"]),
        POLICY,
        repo.root,
    )
    assert outputs["presence-report-only"] == "true"
    assert outputs["repeats"] == "10"
    assert outputs["base"] == base
    assert outputs["event"] == "pull_request"
    assert outputs["slug"] == "root"


def test_build_without_label_input(repo):
    outputs = plan.build("go", {}, plan.Event("push", {}), POLICY, repo.root)
    assert outputs["presence-report-only"] == "false"
    assert outputs["base"] == ""


def test_write_outputs(tmp_path, monkeypatch):
    target = tmp_path / "out"
    monkeypatch.setattr(plan.secrets, "token_hex", lambda n: "f" * 2 * n)
    plan.write_outputs({"a": "1", "b": "x y", "c": "make deps\nmake test", "d": "x\ry"}, target)
    end = "GOLDEN_" + "f" * 32
    assert target.read_bytes().decode() == (
        f"a=1\nb=x y\nc<<{end}\nmake deps\nmake test\n{end}\nd<<{end}\nx\ry\n{end}\n"
    )


def test_write_outputs_cannot_be_injected(tmp_path):
    target = tmp_path / "out"
    plan.write_outputs({"cmd": "pytest\nbase=0000", "base": "real"}, target)
    lines = target.read_text().splitlines()
    assert lines[0].startswith("cmd<<GOLDEN_")
    assert lines[3] == lines[0].removeprefix("cmd<<")
    assert lines[4] == "base=real"


@pytest.mark.parametrize(
    "event",
    [
        plan.Event("pull_request", {}),
        plan.Event("pull_request", {"pull_request": {"base": None}}),
        plan.Event("merge_group", {"merge_group": {}}),
    ],
)
def test_event_without_base_fails_closed(event):
    with pytest.raises(ValueError, match="event payload has no base commit"):
        event.base()


def test_changes_select_only_runnable_tests(repo):
    repo.commit({"pkg/a.py": "def f():\n    return 1\n"})
    base = repo.git("rev-parse", "HEAD")
    repo.commit(
        {
            "tests/conftest.py": "import pytest\n",
            "tests/helpers.py": "X = 1\n",
            "tests/test_a.py": "from pkg.a import f\n",
            "shop/tests.py": "def test_x():\n    pass\n",
        }
    )
    result = plan.changes("python", base, POLICY)
    assert result["tests"] == "shop/tests.py tests/test_a.py"
    assert result["test-packages"] == "./shop ./tests"


def test_render_escapes_pipes():
    outputs = dict.fromkeys(
        (
            "base",
            "install-command",
            "format-command",
            "lint-command",
            "typecheck-command",
            "test-command",
            "tests",
            "mutate",
            "gating-changes",
        ),
        "",
    )
    outputs["format-command"] = "a || b"
    outputs["test-command"] = "make deps\nmake test"
    text = plan.render("go", outputs)
    assert "| format-command | `a \\|\\| b` |" in text
    assert "| test-command | `make deps ⏎ make test` |" in text
    assert "| base | _(none)_ |" in text


def test_cli_plan_prints_json_locally(repo, monkeypatch, capsys):
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    monkeypatch.setenv("GOLDEN_INPUTS", json.dumps({"test-command": "make test"}))
    assert cli.main(["plan", "--language", "python"]) == 0
    assert json.loads(capsys.readouterr().out)["test-command"] == "make test"


def test_cli_plan_writes_github_outputs(repo, monkeypatch, capsys):
    out = repo.root / "gh-output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    monkeypatch.delenv("GOLDEN_INPUTS", raising=False)
    assert cli.main(["plan", "--language", "node"]) == 0
    assert "install-command=npm ci --ignore-scripts\n" in out.read_text()
    assert "### Golden plan (node)" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("language", "files", "tests"),
    [
        (
            "python",
            ["tests/conftest.py", "tests/helpers.py", "conftest.py", "tests/test_a.py"],
            "tests/test_a.py",
        ),
        ("python", ["tests/conftest.py", "tests/fixtures/data.py"], ""),
        ("node", ["test/setup.ts", "tests/fixtures/x.json", "src/a.test.ts"], "src/a.test.ts"),
        ("node", ["src/__tests__/helpers.ts"], ""),
    ],
)
def test_flake_gate_only_reruns_runnable_tests(repo, language, files, tests):
    # Test support files count for presence but cannot be re-run on their own.
    repo.commit({"README.md": "x"})
    base = repo.git("rev-parse", "HEAD")
    repo.commit(dict.fromkeys(files, "x\n"))
    assert plan.changes(language, base, POLICY)["tests"] == tests


def test_mutation_targets_include_functions_a_test_only_change_touches(repo):
    mod = "def rate(q):\n    return 2 if q > 10 else 1\n\n\ndef fee():\n    return 1\n"
    test = "from pkg.mod import rate\n\n\ndef test_rate():\n    assert rate(11) == 2\n"
    repo.commit({"pkg/mod.py": mod, "tests/test_mod.py": test})
    base = repo.git("rev-parse", "HEAD")
    repo.commit({"tests/test_mod.py": test.replace("assert rate(11) == 2", "rate(11)")})
    assert plan.mutation_targets(plan.presets.get("python"), base, ()) == [
        "pkg.mod.x_rate__mutmut_*"
    ]
    assert plan.changes("python", base, POLICY)["mutate"] == "pkg.mod.x_rate__mutmut_*"
    assert plan.changes("python", base, POLICY)["sources-changed"] == "false"


def test_gating_changes(repo, monkeypatch):
    repo.commit({".github/workflows/ci.yml": "a", "svc/golden.toml": "", "svc/a.py": "x"})
    base = repo.git("rev-parse", "HEAD")
    assert plan.gating_changes(base, Path("golden.toml")) == []
    repo.commit(
        {".github/workflows/ci.yml": "b", "svc/golden.toml": "[coverage]", "golden.toml": ""}
    )
    monkeypatch.chdir(repo.root / "svc")
    assert plan.gating_changes(base, Path("golden.toml")) == [
        ".github/workflows/ci.yml",
        "svc/golden.toml",
    ]
    assert plan.gating_changes(base, Path("../golden.toml")) == [
        ".github/workflows/ci.yml",
        "golden.toml",
    ]
    assert plan.gating_changes("", Path("golden.toml")) == []
