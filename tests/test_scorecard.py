import datetime as dt
import json
import urllib.error

import pytest

from golden import config
from golden import scorecard as sc

PIN = "a" * 40
CI = f"""name: ci
on:
  pull_request:
permissions:
  contents: read
jobs:
  golden:
    uses: Mira190/golden-pipeline-open/.github/workflows/golden.yml@{PIN} # v1.0.0
  container:
    uses: Mira190/golden-pipeline-open/.github/workflows/container.yml@{PIN} # v1.0.0
"""
OSSF = f"""permissions: read-all
jobs:
  s:
    steps:
      - uses: ossf/scorecard-action@{PIN} # v2.5.0
"""
RULES = [
    {
        "type": "required_status_checks",
        "parameters": {"required_status_checks": [{"context": "ci / golden"}]},
    },
    {
        "type": "pull_request",
        "parameters": {"required_approving_review_count": 1, "require_code_owner_review": True},
    },
    {"type": "non_fast_forward"},
    {"type": "deletion"},
]


def make(tmp_path, files):
    for name, content in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    return tmp_path


ELITE = {
    ".github/workflows/ci.yml": CI,
    ".github/workflows/scorecard.yml": OSSF,
    ".github/dependabot.yml": "version: 2\nupdates: []\ncooldown:\n  default-days: 7\n",
    ".github/CODEOWNERS": "* @me\n",
    "SECURITY.md": "report to me",
    "service/uv.lock": "",
}


def repo(tmp_path, files, rules=RULES, policy=None):
    return sc.Repo(make(tmp_path, files), policy or config.from_mapping({}), rules)


def test_elite_repository(tmp_path):
    results = sc.evaluate(repo(tmp_path, ELITE))
    assert [r.id for r in results if r.passed is not True] == []
    assert sc.level(results) == 4


def test_empty_repository_is_level_zero(tmp_path):
    results = sc.evaluate(repo(tmp_path, {}, rules=None))
    assert sc.level(results) == 0


def test_unknown_rules_never_count_as_pass(tmp_path):
    results = sc.evaluate(repo(tmp_path, ELITE, rules=None))
    unknown = {r.id for r in results if r.passed is None}
    assert unknown == {"required-check", "reviews", "owner-review", "history"}
    assert sc.level(results) == 2


def test_unpinned_action_blocks_level_three(tmp_path):
    files = {
        **ELITE,
        ".github/workflows/x.yml": "permissions: {}\nsteps:\n  - uses: actions/checkout@v7\n",
    }
    results = {r.id: r.passed for r in sc.evaluate(repo(tmp_path, files))}
    assert results["pinned"] is False


def test_local_and_self_references_need_no_pin(tmp_path):
    files = {
        **ELITE,
        ".github/workflows/x.yml": "permissions: {}\njobs:\n"
        "  a:\n    uses: ./.github/workflows/golden.yml\n"
        "  b:\n    steps:\n      - uses: $/actions/x\n      - uses: docker://alpine:3\n",
    }
    assert sc.actions_pinned(sc.Repo(make(tmp_path, files), config.from_mapping({})))


def test_missing_permissions(tmp_path):
    files = {**ELITE, ".github/workflows/x.yml": "on: push\n"}
    assert sc.least_privilege(sc.Repo(make(tmp_path, files), config.from_mapping({}))) is False


def test_rules_must_name_golden_check(tmp_path):
    rules = [
        {**RULES[0], "parameters": {"required_status_checks": [{"context": "lint"}]}},
        *RULES[1:],
    ]
    results = {r.id: r.passed for r in sc.evaluate(repo(tmp_path, ELITE, rules=rules))}
    assert results["required-check"] is False


def test_reviews_need_an_approval(tmp_path):
    rules = [
        RULES[0],
        {"type": "pull_request", "parameters": {"required_approving_review_count": 0}},
    ]
    results = {r.id: r.passed for r in sc.evaluate(repo(tmp_path, ELITE, rules=rules))}
    assert results["reviews"] is False
    assert results["history"] is False


def test_active_waiver_blocks_level_four(tmp_path):
    expires = dt.datetime.now(dt.UTC).date() + dt.timedelta(days=10)
    policy = config.from_mapping({"waivers": {"mutation": {"reason": "x", "expires": expires}}})
    results = {r.id: r.passed for r in sc.evaluate(repo(tmp_path, ELITE, policy=policy))}
    assert results["waivers"] is False


def test_renovate_minimum_release_age_counts_as_cooldown(tmp_path):
    files = {k: v for k, v in ELITE.items() if "dependabot" not in k}
    files["renovate.json"] = json.dumps({"minimumReleaseAge": "7 days"})
    results = {r.id: r.passed for r in sc.evaluate(repo(tmp_path, files))}
    assert results["deps-bot"] is True
    assert results["cooldown"] is True


def test_render(tmp_path):
    text = sc.render(sc.evaluate(repo(tmp_path, ELITE, rules=None)))
    assert "level 2 / 4" in text
    assert "❔ unknown" in text


class FakeResponse:
    def __init__(self, body):
        self.body = body

    def read(self):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_fetch_rules(monkeypatch):
    seen = {}

    def fake_urlopen(request, timeout):
        seen["url"] = request.full_url
        seen["auth"] = request.get_header("Authorization")
        return FakeResponse(json.dumps(RULES).encode())

    monkeypatch.setattr(sc.urllib.request, "urlopen", fake_urlopen)
    assert sc.fetch_rules("o/r", "main", "tok") == RULES
    assert seen == {
        "url": "https://api.github.com/repos/o/r/rules/branches/main",
        "auth": "Bearer tok",
    }


@pytest.mark.parametrize("failure", [urllib.error.URLError("down"), TimeoutError()])
def test_fetch_rules_unavailable(monkeypatch, failure):
    def boom(request, timeout):
        raise failure

    monkeypatch.setattr(sc.urllib.request, "urlopen", boom)
    assert sc.fetch_rules("o/r", "main", None) is None


def test_fetch_rules_rejects_non_list(monkeypatch):
    monkeypatch.setattr(sc.urllib.request, "urlopen", lambda r, timeout: FakeResponse(b"{}"))
    assert sc.fetch_rules("o/r", "main", None) is None


def test_rules_from_env(monkeypatch):
    monkeypatch.delenv("GOLDEN_DEFAULT_BRANCH", raising=False)
    assert sc.rules_from_env() is None
    monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")
    monkeypatch.setenv("GOLDEN_DEFAULT_BRANCH", "main")
    monkeypatch.setattr(sc, "fetch_rules", lambda repo, branch, token: [{"type": repo + branch}])
    assert sc.rules_from_env() == [{"type": "o/rmain"}]


def check(tmp_path, control, files, rules=RULES, policy_path="golden.toml"):
    policy = config.from_mapping({})
    return control(sc.Repo(make(tmp_path, files), policy, rules, policy_path))


@pytest.mark.parametrize(
    ("ci", "expected"),
    [
        ("on:\n  pull_request:\n", True),
        ("on:\n  pull_request:\n    types: [opened]\n", True),
        ("on: [push, pull_request]\n", True),
        ("on: pull_request\n", True),
        ("on:\n  - push\n  - pull_request\n", True),
        ("on:\n  pull_request_target:\n", False),
        ("on: push\nconcurrency: ${{ github.event.pull_request.number }}\n", False),
        ("on: push\njobs:\n  a:\n    if: github.event_name == 'pull_request'\n", False),
        ("on: push  # pull_request later\n", False),
        ("on: push\n# on: pull_request\n", False),
    ],
)
def test_runs_on_prs(tmp_path, ci, expected):
    assert check(tmp_path, sc.runs_on_prs, {".github/workflows/ci.yml": ci}) is expected


@pytest.mark.parametrize(
    ("top", "expected"),
    [
        ("permissions: {}\n", True),
        ("permissions: read-all\n", True),
        ("permissions:\n  contents: read\n  pull-requests: read\n", True),
        (
            "permissions:\n  contents: read\njobs:\n  r:\n"
            "    permissions:\n      contents: write\n",
            True,
        ),
        ("permissions: write-all\n", False),
        ("permissions:\n  contents: write\n", False),
        ("permissions:\n  contents: read\n\n  packages: write\njobs: {}\n", False),
        ("permissions: { contents: write }\n", False),
        ("permissions:\njobs: {}\n", False),
        ("jobs:\n  a:\n    permissions: {}\n", False),
        ("# permissions: {}\non: push\n", False),
    ],
)
def test_least_privilege(tmp_path, top, expected):
    assert check(tmp_path, sc.least_privilege, {".github/workflows/ci.yml": top}) is expected


def test_top_level_permissions():
    assert sc.top_level_permissions("on: push\npermissions: read-all\n") == "read-all"
    block = "permissions:\n  contents: read\n\n  checks: write\njobs: {}\n"
    assert sc.top_level_permissions(block) == "contents: read\nchecks: write"
    assert sc.top_level_permissions("permissions:\n") == ""
    assert sc.top_level_permissions("jobs:\n  permissions: {}\n") == ""


GOLDEN_CALL = f"jobs:\n  ci:\n    uses: o/golden-pipeline/.github/workflows/golden.yml@{PIN}\n"


@pytest.mark.parametrize(
    ("ci", "expected"),
    [
        (GOLDEN_CALL + "    with:\n      language: go\n", True),
        (GOLDEN_CALL + "    with:\n      mutation: false\n", False),
        (GOLDEN_CALL + "    with:\n      security:  false\n", False),
        (GOLDEN_CALL + "    with:\n      mutation: true\n", True),
        (GOLDEN_CALL + "    with:\n      # mutation: false\n", True),
        ("jobs:\n  other:\n    with:\n      mutation: false\n", True),
    ],
)
def test_gates_enabled(tmp_path, ci, expected):
    assert check(tmp_path, sc.gates_enabled, {".github/workflows/ci.yml": ci}) is expected


@pytest.mark.parametrize(
    ("ci", "provenance", "ossf"),
    [
        (f"steps:\n  - uses: actions/attest-build-provenance@{PIN}\n", True, False),
        (
            f"jobs:\n  c:\n    uses: o/golden-pipeline/.github/workflows/container.yml@{PIN}\n",
            True,
            False,
        ),
        ("jobs:\n  c:\n    uses: ./.github/workflows/container.yml\n", True, False),
        (f"steps:\n  - uses: ossf/scorecard-action@{PIN}\n", False, True),
        ("# uses: actions/attest@x and ossf/scorecard-action@y\n", False, False),
        ("steps:\n  - run: echo container.yml ossf/scorecard-action\n", False, False),
        (
            f"jobs:\n  c:\n    uses: o/r/.github/workflows/my-container.yml.bak@{PIN}\n",
            False,
            False,
        ),
    ],
)
def test_provenance_and_ossf_need_a_real_step(tmp_path, ci, provenance, ossf):
    files = {".github/workflows/ci.yml": ci}
    assert check(tmp_path / "p", sc.provenance, files) is provenance
    assert check(tmp_path / "o", sc.ossf_scorecard, files) is ossf


@pytest.mark.parametrize(
    ("files", "bot", "cooldown"),
    [
        (
            {".github/dependabot.yml": "updates:\n  - cooldown:\n      default-days: 7\n"},
            True,
            True,
        ),
        ({".github/dependabot.yaml": "updates:\n  - x: 1\n    # cooldown: 7\n"}, True, False),
        ({".github/dependabot.yml": "# see cooldown docs\nupdates: []\n"}, True, False),
        ({".renovaterc.json": '{"minimumReleaseAge": "7 days"}'}, True, True),
        ({".github/renovate.json5": "{\n  minimumReleaseAge: '7 days',\n}"}, True, True),
        ({"renovate.json5": "{\n  // minimumReleaseAge: '7 days',\n}"}, True, False),
        ({".gitlab/renovate.json": '{"extends": []}'}, True, False),
        ({"README.md": "cooldown minimumReleaseAge"}, False, False),
    ],
)
def test_dependency_bots_and_cooldowns(tmp_path, files, bot, cooldown):
    assert check(tmp_path / "b", sc.deps_bot, files) is bot
    assert check(tmp_path / "c", sc.dependency_cooldown, files) is cooldown


@pytest.mark.parametrize(
    ("files", "expected"),
    [
        ({"svc/api/uv.lock": ""}, True),
        ({"go.sum": ""}, True),
        ({"node_modules/x/package-lock.json": ""}, False),
        ({"vendor/m/go.sum": "", ".venv/lib/uv.lock": "", ".git/x/yarn.lock": ""}, False),
        ({"package.json": "{}"}, False),
    ],
)
def test_has_lockfile_ignores_foreign_directories(tmp_path, files, expected):
    assert check(tmp_path, sc.has_lockfile, files) is expected


@pytest.mark.parametrize(
    ("codeowners", "path", "expected"),
    [
        ("* @team\n", "golden.toml", True),
        ("/golden.toml @team\n", "golden.toml", True),
        ("/golden.toml @team\n", "svc/golden.toml", False),
        ("golden.toml @team\n", "svc/golden.toml", True),
        ("/.github/ @team\n", ".github/workflows/ci.yml", True),
        ("* @team\n/.github/\n", ".github/workflows/ci.yml", False),
        ("docs/* @team\n", ".github/workflows/ci.yml", False),
        ("# * @team\n", "golden.toml", False),
        ("* @team # comment\n\n", "golden.toml", True),
    ],
)
def test_codeowners_last_match_wins(tmp_path, codeowners, path, expected):
    files = {".github/CODEOWNERS": codeowners}
    assert sc.owned(sc.Repo(make(tmp_path, files), config.from_mapping({})), path) is expected


def test_owned_without_codeowners(tmp_path):
    assert sc.owned(sc.Repo(tmp_path, config.from_mapping({})), "golden.toml") is False


@pytest.mark.parametrize(
    ("params", "codeowners", "policy_path", "expected"),
    [
        ({"require_code_owner_review": True}, "* @team\n", "golden.toml", True),
        ({"require_code_owner_review": False}, "* @team\n", "golden.toml", False),
        ({}, "* @team\n", "golden.toml", False),
        ({"require_code_owner_review": True}, "/.github/ @team\n", "golden.toml", False),
        ({"require_code_owner_review": True}, "/golden.toml @team\n", "golden.toml", False),
        (
            {"require_code_owner_review": True},
            "/.github/ @t\n/svc/golden.toml @t\n",
            "svc/golden.toml",
            True,
        ),
    ],
)
def test_owner_review(tmp_path, params, codeowners, policy_path, expected):
    rules = [{"type": "pull_request", "parameters": params}]
    files = {"CODEOWNERS": codeowners}
    assert check(tmp_path, sc.owner_review, files, rules, policy_path) is expected


def test_owner_review_unknown_without_rules(tmp_path):
    assert check(tmp_path, sc.owner_review, {}, rules=None) is None
    assert check(tmp_path, sc.owner_review, {}, rules=[]) is False


def test_workflow_comments_are_ignored(tmp_path):
    files = {".github/workflows/ci.yml": "on: push # pull_request\nx: 'a#b'\n# whole line\n"}
    repo_ = sc.Repo(make(tmp_path, files), config.from_mapping({}))
    assert repo_.workflows() == {"ci.yml": "on: push \nx: 'a#b'\n\n"}
