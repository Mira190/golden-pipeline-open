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
    {"type": "pull_request", "parameters": {"required_approving_review_count": 1}},
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
    assert unknown == {"required-check", "reviews", "history"}
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
