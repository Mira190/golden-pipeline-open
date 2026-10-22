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
CODEQL = f"""name: codeql
on:
  pull_request:
permissions:
  contents: read
jobs:
  analyze:
    permissions:
      security-events: write
    steps:
      - uses: github/codeql-action/init@{PIN} # v4.35.1
      - uses: github/codeql-action/analyze@{PIN} # v4.35.1
"""
RULES = [
    {
        "type": "required_status_checks",
        "parameters": {"required_status_checks": [{"context": "ci / golden"}]},
    },
    {
        "type": "pull_request",
        "parameters": {
            "required_approving_review_count": 1,
            "require_code_owner_review": True,
            "dismiss_stale_reviews_on_push": True,
            "require_last_push_approval": True,
        },
    },
    {"type": "non_fast_forward"},
    {"type": "deletion"},
    {
        "type": "merge_queue",
        "parameters": {"merge_method": "SQUASH", "grouping_strategy": "ALLGREEN"},
    },
]
TAG_RULESET = {
    "id": 7,
    "name": "release tags",
    "target": "tag",
    "enforcement": "active",
    "conditions": {"ref_name": {"include": ["refs/tags/v*"], "exclude": []}},
    "rules": [{"type": "deletion"}, {"type": "non_fast_forward"}],
}
TAG_RULESETS = [TAG_RULESET]


def make(tmp_path, files):
    for name, content in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    return tmp_path


ELITE = {
    ".github/workflows/ci.yml": CI,
    ".github/workflows/scorecard.yml": OSSF,
    ".github/workflows/codeql.yml": CODEQL,
    ".github/dependabot.yml": "version: 2\nupdates: []\ncooldown:\n  default-days: 7\n",
    ".github/CODEOWNERS": "* @me\n",
    "SECURITY.md": "report to me",
    "service/uv.lock": "",
}


def repo(tmp_path, files, rules=RULES, policy=None, tag_rulesets=TAG_RULESETS):
    root = make(tmp_path, files)
    return sc.Repo(root, policy or config.from_mapping({}), rules, tag_rulesets=tag_rulesets)


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
    assert unknown == {
        "required-check",
        "reviews",
        "owner-review",
        "history",
        "fresh-review",
        "merge-queue",
    }
    assert sc.level(results) == 2


def test_unknown_tag_rulesets_never_count_as_pass(tmp_path):
    results = sc.evaluate(repo(tmp_path, ELITE, tag_rulesets=None))
    assert [r.id for r in results if r.passed is not True] == ["release-tags"]
    assert next(r for r in results if r.id == "release-tags").passed is None
    assert sc.level(results) == 3


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


def spy_request_headers(monkeypatch):
    """Record the headers handed to urllib.request.Request, spelled as GitHub documents them
    (urllib re-capitalises them, so only the constructor call shows the spelling)."""
    sent = {}
    request = sc.urllib.request.Request

    def spy(url, headers):
        sent["headers"] = headers
        return request(url, headers=headers)

    monkeypatch.setattr(sc.urllib.request, "Request", spy)
    return sent


def test_fetch_rules(monkeypatch):
    seen = {}

    def fake_urlopen(request, timeout):
        seen["url"] = request.full_url
        seen["auth"] = request.get_header("Authorization")
        seen["timeout"] = timeout
        return FakeResponse(json.dumps(RULES).encode())

    sent = spy_request_headers(monkeypatch)
    monkeypatch.setattr(sc.urllib.request, "urlopen", fake_urlopen)
    assert sc.fetch_rules("o/r", "main", "tok") == RULES
    assert seen == {
        "url": "https://api.github.com/repos/o/r/rules/branches/main",
        "auth": "Bearer tok",
        "timeout": 10,
    }
    assert sent["headers"] == {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "Authorization": "Bearer tok",
    }


def test_fetch_rules_without_a_token_sends_no_authorization(monkeypatch):
    sent = spy_request_headers(monkeypatch)
    monkeypatch.setattr(sc.urllib.request, "urlopen", lambda r, timeout: FakeResponse(b"[]"))
    assert sc.fetch_rules("o/r", "main", None) == []
    assert sent["headers"] == {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
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


def check(tmp_path, control, files, rules=RULES, policy_path="golden.toml", tag_rulesets=None):
    policy = config.from_mapping({})
    return control(sc.Repo(make(tmp_path, files), policy, rules, policy_path, tag_rulesets))


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
        (GOLDEN_CALL + "    with:\n      workflow-lint: false\n", False),
        (GOLDEN_CALL + "    with:\n      mutation: true\n", True),
        (GOLDEN_CALL + "    with:\n      # mutation: false\n", True),
        ("jobs:\n  other:\n    with:\n      mutation: false\n", True),
        # Repository-wide scans need one caller only; mutation is needed in every project.
        (
            GOLDEN_CALL
            + GOLDEN_CALL.replace("jobs:\n  ci", "  b")
            + "    with:\n      security: false\n",
            True,
        ),
        (
            GOLDEN_CALL
            + GOLDEN_CALL.replace("jobs:\n  ci", "  b")
            + "    with:\n      workflow-lint: false\n",
            True,
        ),
        (
            GOLDEN_CALL
            + GOLDEN_CALL.replace("jobs:\n  ci", "  b")
            + "    with:\n      mutation: false\n",
            False,
        ),
        (
            GOLDEN_CALL
            + "    with:\n      security: false\n"
            + GOLDEN_CALL.replace("jobs:\n  ci", "  b")
            + "    with:\n      security: false\n",
            False,
        ),
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


@pytest.mark.parametrize(
    ("on", "expected"),
    [
        ("on:\n  pull_request:\n", True),
        ("on: [push, pull_request]\n", True),
        ("on:\n  - pull_request\n", True),
        ("on:\n  pull_request_target:\n", False),
        ("on: push  # pull_request later\n", False),
        ("on: push\nif: github.event_name == 'pull_request'\n", False),
    ],
)
def test_runs_on_prs_needs_a_real_trigger(tmp_path, on, expected):
    workflow = f"{on}jobs:\n  g:\n    uses: o/golden-pipeline/.github/workflows/golden.yml@{PIN}\n"
    r = repo(tmp_path, {".github/workflows/ci.yml": workflow})
    assert sc.runs_on_prs(r) is expected


def test_mentions_in_comments_satisfy_no_control(tmp_path):
    workflow = (
        "# uses o/golden-pipeline/.github/workflows/golden.yml and container.yml\n"
        "# - uses: actions/attest-build-provenance@v4\n"
        "on: pull_request\npermissions: {}\njobs: {}\n"
    )
    r = repo(tmp_path, {".github/workflows/ci.yml": workflow})
    assert not sc.uses_golden(r)
    assert not sc.provenance(r)


@pytest.mark.parametrize(
    "step",
    [
        f"      - uses: actions/attest-build-provenance@{PIN}\n",
        f"      - uses: actions/attest@{PIN}\n",
        f"    uses: o/golden-pipeline-open/.github/workflows/container.yml@{PIN}\n",
    ],
)
def test_provenance_from_a_real_step(tmp_path, step):
    r = repo(tmp_path, {".github/workflows/release.yml": "jobs:\n  r:\n    steps:\n" + step})
    assert sc.provenance(r)


FRESH = {"dismiss_stale_reviews_on_push": True, "require_last_push_approval": True}
FRESH_TITLE = "Approvals are dismissed on new pushes and the last pusher cannot self-approve"


def pull_request(**params):
    return [{"type": "pull_request", "parameters": params}]


@pytest.mark.parametrize(
    ("rules", "expected"),
    [
        (RULES, True),
        (pull_request(**FRESH), True),
        # Unrelated parameters neither help nor hurt.
        (pull_request(**FRESH, required_approving_review_count=0), True),
        # The pull_request rule is found among other rules.
        ([{"type": "deletion"}, *pull_request(**FRESH), {"type": "non_fast_forward"}], True),
        (pull_request(**{**FRESH, "dismiss_stale_reviews_on_push": False}), False),
        (pull_request(**{**FRESH, "require_last_push_approval": False}), False),
        (
            pull_request(dismiss_stale_reviews_on_push=False, require_last_push_approval=False),
            False,
        ),
        # A missing parameter is as bad as a false one (fail closed).
        (pull_request(dismiss_stale_reviews_on_push=True), False),
        (pull_request(require_last_push_approval=True), False),
        (pull_request(), False),
        # Only the literal `true` counts, not a truthy string or number.
        (
            pull_request(dismiss_stale_reviews_on_push="true", require_last_push_approval=True),
            False,
        ),
        (pull_request(dismiss_stale_reviews_on_push=True, require_last_push_approval=1), False),
        # The parameters of a rule with another type are not the pull_request rule's.
        ([{"type": "required_status_checks", "parameters": FRESH}], False),
        # No pull_request rule at all, or a rule without `parameters`.
        ([{"type": "deletion"}, {"type": "non_fast_forward"}], False),
        ([{"type": "pull_request"}], False),
        ([{"type": "pull_request", "parameters": None}], False),
        ([], False),
        # Rules unavailable: unknown, never a pass.
        (None, None),
    ],
)
def test_fresh_review(tmp_path, rules, expected):
    assert check(tmp_path, sc.fresh_review, {}, rules) is expected


def test_fresh_review_is_a_level_four_control():
    control = next(c for c in sc.CONTROLS if c.id == "fresh-review")
    assert (control.level, control.title, control.check) == (4, FRESH_TITLE, sc.fresh_review)
    assert len(sc.CONTROLS) == 21


@pytest.mark.parametrize("parameter", sorted(FRESH))
def test_a_stale_approval_rule_caps_the_level_at_three(tmp_path, parameter):
    rules = [RULES[0], *pull_request(**{**RULES[1]["parameters"], parameter: False}), *RULES[2:]]
    results = sc.evaluate(repo(tmp_path, ELITE, rules=rules))
    assert [r.id for r in results if r.passed is not True] == ["fresh-review"]
    assert sc.level(results) == 3
    assert f"| L4 | {FRESH_TITLE} | ❌ |" in sc.render(results)


# ------------------------------------------------------- merge queue, SAST, release tags
@pytest.mark.parametrize(
    ("rules", "expected"),
    [
        (RULES, True),
        ([{"type": "merge_queue"}], True),
        ([{"type": "deletion"}, {"type": "merge_queue", "parameters": {}}], True),
        ([{"type": "deletion"}, {"type": "pull_request", "parameters": {}}], False),
        ([{"type": "merge_queue_extra"}, {"type": "required_linear_history"}], False),
        ([], False),
        (None, None),
    ],
)
def test_merge_queue(tmp_path, rules, expected):
    assert check(tmp_path, sc.merge_queue, {}, rules) is expected


PR = "on:\n  pull_request:\n"
SCAN = {
    "type": "code_scanning",
    "parameters": {
        "code_scanning_tools": [{"tool": "CodeQL", "security_alerts_threshold": "high_or_higher"}]
    },
}


def steps(*items, on=PR):
    """A workflow whose single job runs the given steps (each may span several lines)."""
    return on + "jobs:\n  s:\n    steps:\n" + "".join(f"      - {item}\n" for item in items)


ANALYZE = "uses: github/codeql-action/analyze@v4"
GOLDEN_SAST_REF = "Mira190/golden-pipeline-open/.github/workflows/sast.yml"
SEMGREP_ACTION = "uses: semgrep/semgrep-action@v1"
SCHEDULE = "on:\n  schedule:\n    - cron: '0 0 * * 1'\n"
SCRIPT_OF_ANOTHER_KEY = "name: x\n        with:\n          s: |\n            semgrep ci"


@pytest.mark.parametrize(
    ("workflow", "expected"),
    [
        (steps(f"uses: github/codeql-action/analyze@{PIN} # v4.35.1"), True),
        (steps(ANALYZE), True),
        (steps("uses: returntocorp/semgrep-action@v1"), True),
        (steps(SEMGREP_ACTION), True),
        (steps(SEMGREP_ACTION, on="on: [push, pull_request]\n"), True),
        (PR + "jobs:\n  s:\n    uses: github/codeql-action/analyze@v4\n", True),
        # A call of golden's sast.yml runs CodeQL and gates it.
        (PR + f"jobs:\n  s:\n    uses: {GOLDEN_SAST_REF}@{PIN}\n", True),
        (PR + "jobs:\n  s:\n    uses: acme/golden-pipeline/.github/workflows/sast.yml@v1\n", True),
        (PR + "jobs:\n  s:\n    uses: $/.github/workflows/sast.yml\n", True),
        (PR + "jobs:\n  s:\n    uses: ./.github/workflows/sast.yml\n", True),
        (PR + "jobs:\n  s:\n    uses: o/other/.github/workflows/sast.yml@v1\n", False),
        (PR + "jobs:\n  s:\n    uses: x/o/golden-pipeline/.github/workflows/sast.yml@v1\n", False),
        (
            PR + "jobs:\n  s:\n    uses: o/golden-pipeline/.github/workflows/sast.yml.bak@v1\n",
            False,
        ),
        (PR + "jobs:\n  s:\n    uses: $/.github/workflows/golden.yml\n", False),
        ("on: push\njobs:\n  s:\n    uses: $/.github/workflows/sast.yml\n", False),
        # Only the analyze step reports findings: init alone, another repo or path don't.
        (steps(f"uses: github/codeql-action/init@{PIN}"), False),
        (steps("uses: github/codeql-action/upload-sarif@v4"), False),
        (steps("uses: github/codeql-action/analyze-extra@v4"), False),
        (steps("uses: github/codeql-action/analyze"), False),
        (steps("uses: o/codeql-action/analyze@v4"), False),
        (steps("uses: semgrep/semgrep@v1"), False),
        (steps("uses: other/semgrep-action@v1"), False),
        # The analysis must run at diff time, on the `pull_request` trigger.
        (steps(ANALYZE, on="on: push\n"), False),
        (steps(SEMGREP_ACTION, on=SCHEDULE), False),
        (steps(SEMGREP_ACTION, on="on:\n  pull_request_target:\n"), False),
        (steps("run: semgrep ci", on="on: push\nx: ${{ github.event.pull_request.id }}\n"), False),
        # Semgrep as a command in a run script.
        (steps("run: semgrep ci"), True),
        (steps("run: semgrep scan --config auto"), True),
        (steps("run: sudo semgrep ci"), True),
        (steps("run: uvx semgrep scan"), True),
        (steps("run: pipx run semgrep ci"), True),
        (steps("run: uv run semgrep ci"), True),
        (steps('run: "semgrep ci"'), True),
        (steps("run: 'semgrep ci'"), True),
        (steps("run: pip install semgrep && semgrep ci"), True),
        (steps("run: pip install semgrep; semgrep ci"), True),
        (steps("run: cat files | semgrep scan -"), True),
        (steps("run: (semgrep ci)"), True),
        (steps("run: out=$(semgrep ci)"), True),
        (steps("run: |\n          pip install semgrep\n          semgrep ci"), True),
        (steps("name: scan\n        run: |\n          semgrep ci"), True),
        (steps("run: |\n          echo start\n\n          semgrep ci"), True),
        (steps("run: pip install semgrep"), False),
        (steps("run: echo semgrep ci"), False),
        (steps('run: echo "semgrep ci"'), False),
        (steps("run: semgrep-ci x"), False),
        (steps("run: ./semgrep ci"), False),
        (steps("run: docker run returntocorp/semgrep semgrep ci"), False),
        (steps("name: semgrep ci\n        run: echo hi"), False),
        (steps("uses: o/x@v1\n        with:\n          args: semgrep ci"), False),
        # A command that another key of the step (or the next step) holds is not run.
        (steps("run: echo hi", SCRIPT_OF_ANOTHER_KEY), False),
        (steps("run: echo hi\n        with:\n          s: |\n            semgrep ci"), False),
        (
            steps(
                "run: |\n          echo hi\n        env:\n          A: |\n            semgrep ci"
            ),
            False,
        ),
        (PR, False),
        ("", False),
    ],
)
def test_runs_sast_on_prs(workflow, expected):
    assert sc.runs_sast_on_prs(workflow) is expected


@pytest.mark.parametrize(
    ("text", "scripts"),
    [
        ("", []),
        ("on: push\njobs: {}\n", []),
        ("steps:\n  - run: echo 1\n", ["echo 1"]),
        ("steps:\n  - run:\n", [""]),
        ("steps:\n  - run: echo 1\n  - run: echo 2\n", ["echo 1", "echo 2"]),
        (
            "steps:\n  - run: |\n      x\n\n      y\n    env:\n      Z: 1\n  - name: n\n",
            ["|\n      x\n\n      y"],
        ),
        ("steps:\n  - name: a\n    run: |\n      x\n  - uses: o/r@v1\n", ["|\n      x"]),
        ("steps:\n  - name: a\n    run: x\n    shell: bash\n", ["x"]),
        ("steps:\n  - name: a\n    run: |\n      x\n", ["|\n      x"]),
        ("run: a\nother:\n  run: b\n", ["a", "b"]),
        ("steps:\n  - name: run: x\n  - norun: y\n  - runs: z\n", []),
    ],
)
def test_run_scripts(text, scripts):
    assert sc.run_scripts(text) == scripts


@pytest.mark.parametrize(
    ("workflow", "rules", "expected"),
    [
        # Workflow evidence is enough on its own, with the API reachable or not.
        (steps("run: semgrep ci"), None, True),
        (steps("run: semgrep ci"), [], True),
        (CODEQL, None, True),
        # A comment is no evidence.
        (steps("# uses: github/codeql-action/analyze@v4"), None, None),
        (steps("# uses: github/codeql-action/analyze@v4"), [], False),
        (steps("run: echo hi # semgrep ci"), [], False),
        ("# on: pull_request\n" + steps("run: semgrep ci", on=""), [], False),
        # Without workflow evidence the rules decide, or nobody can tell.
        ("on: push\n", None, None),
        ("on: push\n", [], False),
        ("on: push\n", [SCAN], True),
        ("on: push\n", [{"type": "deletion"}, SCAN], True),
        ("on: push\n", RULES, False),
        # A code scanning rule needs at least one required tool.
        ("on: push\n", [{"type": "code_scanning"}], False),
        ("on: push\n", [{"type": "code_scanning", "parameters": None}], False),
        ("on: push\n", [{"type": "code_scanning", "parameters": {}}], False),
        (
            "on: push\n",
            [{"type": "code_scanning", "parameters": {"code_scanning_tools": []}}],
            False,
        ),
        (
            "on: push\n",
            [{"type": "code_scanning", "parameters": {"code_scanning_tools": None}}],
            False,
        ),
        (
            "on: push\n",
            [{"type": "code_scanning", "parameters": {"code_scanning_tools": "CodeQL"}}],
            False,
        ),
        (
            "on: push\n",
            [{"type": "code_scanning", "parameters": {"code_scanning_tools": [{"tool": "x"}]}}],
            True,
        ),
        ("on: push\n", [{"type": "pull_request", "parameters": SCAN["parameters"]}], False),
    ],
)
def test_code_scanning(tmp_path, workflow, rules, expected):
    files = {".github/workflows/ci.yml": workflow}
    assert check(tmp_path, sc.code_scanning, files, rules) is expected


def test_code_scanning_needs_the_trigger_and_the_tool_in_the_same_workflow(tmp_path):
    files = {
        ".github/workflows/ci.yml": steps("run: echo hi"),
        ".github/workflows/nightly.yml": steps("run: semgrep ci", on="on: push\n"),
    }
    assert check(tmp_path / "a", sc.code_scanning, files, []) is False
    assert check(tmp_path / "b", sc.code_scanning, files, None) is None
    files[".github/workflows/sast.yml"] = steps("run: semgrep ci")
    assert check(tmp_path / "c", sc.code_scanning, files, None) is True


def test_code_scanning_without_workflows(tmp_path):
    assert check(tmp_path / "a", sc.code_scanning, {}, None) is None
    assert check(tmp_path / "b", sc.code_scanning, {}, []) is False


def tag_ruleset(
    include=("refs/tags/v*",), exclude=(), rules=("deletion", "non_fast_forward"), **top
):
    conditions = {"ref_name": {"include": list(include), "exclude": list(exclude)}}
    return {**TAG_RULESET, "conditions": conditions, "rules": [{"type": t} for t in rules], **top}


def without(key):
    return {k: v for k, v in TAG_RULESET.items() if k != key}


def with_ref_name(ref_name):
    return {**TAG_RULESET, "conditions": {"ref_name": ref_name}}


@pytest.mark.parametrize(
    ("ruleset", "expected"),
    [
        (TAG_RULESET, True),
        (tag_ruleset(rules=["deletion", "update"]), True),
        (tag_ruleset(rules=["update", "creation", "deletion", "non_fast_forward"]), True),
        (tag_ruleset(rules=["deletion"]), False),
        (tag_ruleset(rules=["non_fast_forward"]), False),
        (tag_ruleset(rules=["update"]), False),
        (tag_ruleset(rules=["deletion", "creation"]), False),
        (tag_ruleset(rules=["deletion", "required_signatures"]), False),
        (tag_ruleset(rules=[]), False),
        ({**TAG_RULESET, "rules": None}, False),
        (without("rules"), False),
        # Active tag rulesets only: not a branch ruleset, not evaluate/disabled.
        (tag_ruleset(target="branch"), False),
        (tag_ruleset(target="push"), False),
        (without("target"), False),
        (tag_ruleset(enforcement="evaluate"), False),
        (tag_ruleset(enforcement="disabled"), False),
        (tag_ruleset(enforcement="enabled"), False),
        (without("enforcement"), False),
        # The ref conditions must cover version tags.
        (tag_ruleset(include=["~ALL"]), True),
        (tag_ruleset(include=["refs/tags/*"]), True),
        (tag_ruleset(include=["refs/tags/**"]), True),
        (tag_ruleset(include=["refs/tags/rc-*", "refs/tags/v*"]), True),
        (tag_ruleset(include=["~DEFAULT_BRANCH"]), False),
        (tag_ruleset(include=["refs/heads/main"]), False),
        (tag_ruleset(include=["refs/tags/release-*"]), False),
        (tag_ruleset(include=["refs/tags/v1.0.0"]), False),
        (tag_ruleset(include=["refs/tags/V*"]), False),
        (tag_ruleset(include=[]), False),
        (with_ref_name({"include": None}), False),
        (with_ref_name({}), False),
        (with_ref_name(None), False),
        ({**TAG_RULESET, "conditions": {}}, False),
        ({**TAG_RULESET, "conditions": None}, False),
        (without("conditions"), False),
        # Exclusions must leave version tags alone.
        (tag_ruleset(exclude=["refs/tags/v1.0.0"]), False),
        (tag_ruleset(exclude=["refs/tags/v*"]), False),
        (tag_ruleset(exclude=["refs/tags/**"]), False),
        (tag_ruleset(exclude=["refs/tags/*"]), False),
        (tag_ruleset(include=["~ALL"], exclude=["~ALL"]), False),
        (tag_ruleset(include=["~ALL"], exclude=["refs/tags/nightly*", "refs/tags/v*"]), False),
        (tag_ruleset(include=["~ALL"], exclude=["refs/tags/nightly*"]), True),
        (tag_ruleset(include=["~ALL"], exclude=["refs/tags/v2.*", "~DEFAULT_BRANCH"]), True),
        (with_ref_name({"include": ["~ALL"]}), True),
        (with_ref_name({"include": ["~ALL"], "exclude": None}), True),
    ],
)
def test_locks_release_tags(ruleset, expected):
    assert sc.locks_release_tags(ruleset) is expected


def test_release_tags(tmp_path):
    other = tag_ruleset(rules=["creation"], id=8)
    halves = [tag_ruleset(rules=["deletion"]), tag_ruleset(rules=["update"])]
    cases = [
        (None, None),
        ([], False),
        ([TAG_RULESET], True),
        ([other], False),
        ([other, TAG_RULESET], True),
        ([TAG_RULESET, other], True),
        # Each ruleset stands on its own: two half-protections don't add up.
        (halves, False),
        ([tag_ruleset(enforcement="evaluate")], False),
    ]
    for i, (rulesets, expected) in enumerate(cases):
        assert check(tmp_path / str(i), sc.release_tags, {}, tag_rulesets=rulesets) is expected


def test_new_l4_controls_are_registered():
    controls = {c.id: c for c in sc.CONTROLS}
    assert [
        (c.level, c.title, c.check)
        for c in (controls["merge-queue"], controls["code-scanning"], controls["release-tags"])
    ] == [
        (4, "Merge queue keeps the default branch green", sc.merge_queue),
        (4, "Static analysis runs on pull requests", sc.code_scanning),
        (4, "Release tags can't be moved or deleted", sc.release_tags),
    ]


def drop(rules, kind):
    return [r for r in rules if r["type"] != kind]


@pytest.mark.parametrize(
    ("control", "title", "kwargs"),
    [
        (
            "merge-queue",
            "Merge queue keeps the default branch green",
            {"rules": drop(RULES, "merge_queue")},
        ),
        ("release-tags", "Release tags can't be moved or deleted", {"tag_rulesets": []}),
        (
            "release-tags",
            "Release tags can't be moved or deleted",
            {"tag_rulesets": [tag_ruleset(rules=["deletion"])]},
        ),
    ],
)
def test_each_new_control_caps_the_level_at_three(tmp_path, control, title, kwargs):
    results = sc.evaluate(repo(tmp_path, ELITE, **kwargs))
    assert [r.id for r in results if r.passed is not True] == [control]
    assert sc.level(results) == 3
    assert f"| L4 | {title} | ❌ |" in sc.render(results)


def test_without_static_analysis_the_level_is_three(tmp_path):
    files = {k: v for k, v in ELITE.items() if "codeql" not in k}
    results = sc.evaluate(repo(tmp_path, files))
    assert [r.id for r in results if r.passed is not True] == ["code-scanning"]
    assert sc.level(results) == 3
    # ...unless the ruleset requires code scanning results instead of a workflow.
    results = sc.evaluate(repo(tmp_path / "rule", files, rules=[*RULES, SCAN]))
    assert sc.level(results) == 4


API = "https://api.github.com/repos/o/r/rulesets"
LIST_URL = f"{API}?includes_parents=true&targets=tag&per_page=100"


def detail_url(ruleset_id):
    return f"{API}/{ruleset_id}?includes_parents=true"


class FakeApi:
    """urlopen stand-in: serves JSON by URL (an Exception value is raised) and logs the calls."""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def __call__(self, request, timeout):
        self.calls.append((request.full_url, request.get_header("Authorization"), timeout))
        body = self.routes[request.full_url]
        if isinstance(body, Exception):
            raise body
        return FakeResponse(body if isinstance(body, bytes) else json.dumps(body).encode())


def fake_api(monkeypatch, routes):
    api = FakeApi(routes)
    monkeypatch.setattr(sc.urllib.request, "urlopen", api)
    return api


def test_fetch_tag_rulesets_gets_each_ruleset_in_full(monkeypatch):
    second = {**TAG_RULESET, "id": 9, "enforcement": "disabled"}
    summaries = [
        {"id": 7, "name": "release tags", "enforcement": "active"},
        {"id": 9, "name": "old", "enforcement": "disabled", "source_type": "Organization"},
    ]
    routes = {LIST_URL: summaries, detail_url(7): TAG_RULESET, detail_url(9): second}
    api = fake_api(monkeypatch, routes)
    assert sc.fetch_tag_rulesets("o/r", "tok") == [TAG_RULESET, second]
    assert api.calls == [
        (LIST_URL, "Bearer tok", 10),
        (detail_url(7), "Bearer tok", 10),
        (detail_url(9), "Bearer tok", 10),
    ]


def test_fetch_tag_rulesets_without_rulesets(monkeypatch):
    api = fake_api(monkeypatch, {LIST_URL: []})
    assert sc.fetch_tag_rulesets("o/r", None) == []
    assert api.calls == [(LIST_URL, None, 10)]


@pytest.mark.parametrize("failure", [urllib.error.URLError("down"), TimeoutError()])
def test_fetch_tag_rulesets_unavailable(monkeypatch, failure):
    fake_api(monkeypatch, {LIST_URL: failure})
    assert sc.fetch_tag_rulesets("o/r", "tok") is None


@pytest.mark.parametrize("body", [{}, b"{}", b"not json", b"", b"null", b'"x"', b"7"])
def test_fetch_tag_rulesets_rejects_a_list_that_is_not_a_list(monkeypatch, body):
    fake_api(monkeypatch, {LIST_URL: body})
    assert sc.fetch_tag_rulesets("o/r", "tok") is None


@pytest.mark.parametrize(
    "failure", [urllib.error.URLError("403"), TimeoutError(), b"nope", [], b"null"]
)
def test_a_ruleset_that_cannot_be_read_makes_all_of_them_unknown(monkeypatch, failure):
    routes = {LIST_URL: [{"id": 7}, {"id": 8}], detail_url(7): TAG_RULESET, detail_url(8): failure}
    api = fake_api(monkeypatch, routes)
    assert sc.fetch_tag_rulesets("o/r", "tok") is None
    assert [url for url, _, _ in api.calls][-1] == detail_url(8)


@pytest.mark.parametrize(
    "summary", [{"name": "no id"}, {"id": "7"}, {"id": None}, {"id": 7.5}, "7", 7, None, [7]]
)
def test_a_ruleset_without_an_id_makes_all_of_them_unknown(monkeypatch, summary):
    api = fake_api(monkeypatch, {LIST_URL: [{"id": 7}, summary], detail_url(7): TAG_RULESET})
    assert sc.fetch_tag_rulesets("o/r", "tok") is None
    assert [url for url, _, _ in api.calls] == [LIST_URL, detail_url(7)]


def test_tag_rulesets_from_env(monkeypatch):
    monkeypatch.delenv("GITHUB_REPOSITORY", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setattr(sc, "fetch_tag_rulesets", lambda repo, token: [{"id": f"{repo}|{token}"}])
    assert sc.tag_rulesets_from_env() is None
    monkeypatch.setenv("GITHUB_REPOSITORY", "")
    assert sc.tag_rulesets_from_env() is None
    monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")
    assert sc.tag_rulesets_from_env() == [{"id": "o/r|None"}]
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    assert sc.tag_rulesets_from_env() == [{"id": "o/r|tok"}]
