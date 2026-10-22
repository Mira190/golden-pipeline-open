import datetime as dt
from pathlib import Path

import pytest

from golden import config

TODAY = dt.date(2026, 10, 21)


def load(data):
    return config.from_mapping(data, TODAY)


def test_defaults():
    policy = load({})
    assert policy.get("coverage.diff_min") == 90
    assert policy.get("mutation.min_score") == 80
    assert policy.get("flaky.repeats") == 10
    assert policy.exempt == ()
    assert policy.waivers == {}


def test_stricter_values_are_accepted():
    policy = load(
        {"coverage": {"diff_min": 95, "ratchet_tolerance": 0}, "mutation": {"min_score": 90}}
    )
    assert policy.get("coverage.diff_min") == 95
    assert policy.get("coverage.ratchet_tolerance") == 0


@pytest.mark.parametrize(
    ("data", "message"),
    [
        ({"coverage": {"diff_min": 79}}, r"coverage.diff_min = 79 is weaker .*\(>= 80\)"),
        ({"coverage": {"ratchet_tolerance": 2.5}}, r"ratchet_tolerance = 2.5 is weaker .*\(<= 2\)"),
        ({"mutation": {"min_score": 59.9}}, "mutation.min_score"),
        ({"flaky": {"repeats": 2}}, "flaky.repeats"),
        ({"scorecard": {"min_level": 0}}, "scorecard.min_level"),
    ],
)
def test_floor_cannot_be_lowered(data, message):
    with pytest.raises(config.PolicyError, match=message):
        load(data)


def test_floor_itself_is_allowed():
    assert (
        load({"coverage": {"diff_min": 80, "ratchet_tolerance": 2}}).get("coverage.diff_min") == 80
    )


@pytest.mark.parametrize("value", ["90", True, None])
def test_values_must_be_numbers(value):
    with pytest.raises(config.PolicyError, match="must be a number"):
        load({"coverage": {"diff_min": value}})


def test_unknown_section_is_rejected():
    with pytest.raises(config.PolicyError, match="unknown section"):
        load({"covrage": {}})


def test_exempt_globs():
    assert load({"presence": {"exempt": ["**/generated/**"]}}).exempt == ("**/generated/**",)


@pytest.mark.parametrize("exempt", ["x", [1]])
def test_exempt_must_be_list_of_strings(exempt):
    with pytest.raises(config.PolicyError, match="exempt"):
        load({"presence": {"exempt": exempt}})


def waiver(**spec):
    return {"waivers": {"mutation": {"reason": "legacy", "expires": dt.date(2026, 12, 1), **spec}}}


def test_waiver_active_until_expiry():
    policy = load(waiver())
    assert policy.waiver("mutation", TODAY).reason == "legacy"
    assert policy.waiver("mutation", dt.date(2026, 12, 1)) is not None
    assert policy.waiver("mutation", dt.date(2026, 12, 2)) is None
    assert policy.waiver("diff-coverage", TODAY) is None


@pytest.mark.parametrize(
    ("spec", "message"),
    [
        ({"reason": " "}, "needs a reason"),
        ({"expires": "2026-12-01"}, "needs `expires"),
        ({"expires": dt.date(2027, 3, 1)}, "at most 90 days"),
    ],
)
def test_invalid_waivers(spec, message):
    with pytest.raises(config.PolicyError, match=message):
        load(waiver(**spec))


def test_waiver_for_unknown_gate():
    with pytest.raises(config.PolicyError, match="unknown gate"):
        load({"waivers": {"lint": {"reason": "x", "expires": TODAY}}})


def test_load_file(tmp_path):
    path = tmp_path / "golden.toml"
    path.write_text("[mutation]\nmin_score = 85\n")
    assert config.load(path, TODAY).get("mutation.min_score") == 85


def test_load_missing_file_uses_defaults(tmp_path):
    assert config.load(tmp_path / "nope.toml", TODAY).get("coverage.diff_min") == 90


def test_load_invalid_toml(tmp_path):
    path = tmp_path / "golden.toml"
    path.write_text("[mutation\n")
    with pytest.raises(config.PolicyError, match=r"golden\.toml"):
        config.load(path, TODAY)


@pytest.mark.parametrize(
    ("data", "message"),
    [
        ({"coverage": {"diff_minimum": 99}}, r"in \[coverage\]: diff_minimum; known: diff_min"),
        ({"waivers": []}, r"\[waivers\] must be a table"),
        (
            {"waivers": {"mutation": {"reason": "x", "expires": TODAY, "owner": "me"}}},
            r"waiver for mutation: unknown key\(s\) owner",
        ),
        ({"flaky": {"repeats": 4.5}}, "flaky.repeats must be a whole number"),
        ({"scorecard": {"min_level": 2.5}}, "scorecard.min_level must be a whole number"),
        ({"scorecard": {"min_level": 5}}, r"min_level = 5 can never pass \(<= 4\)"),
        ({"coverage": {"diff_min": 100.5}}, r"diff_min = 100.5 can never pass \(<= 100\)"),
        ({"mutation": {"min_score": 101}}, "mutation.min_score = 101 can never pass"),
        ({"flaky": {"repeats": 101}}, "flaky.repeats = 101 can never pass"),
        ({"coverage": {"ratchet_tolerance": -0.1}}, r"-0.1 can never pass \(>= 0\)"),
    ],
)
def test_policy_fails_closed_on_mistakes(data, message):
    with pytest.raises(config.PolicyError, match=message):
        load(data)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_values_must_be_finite(value):
    with pytest.raises(config.PolicyError, match="finite"):
        load({"mutation": {"min_score": value}})


@pytest.mark.parametrize(
    ("data", "message"),
    [
        ({"coverage": 5}, r"\[coverage\] must be a table"),
        ({"presence": ["x"]}, r"\[presence\] must be a table"),
        ({"waivers": "x"}, r"\[waivers\] must be a table"),
        ({"waivers": {"mutation": "x"}}, r"\[waivers.mutation\] must be a table"),
        ({"mutation": {"min_scor": 95}}, r"unknown key\(s\) in \[mutation\]: min_scor"),
        ({"presence": {"exclude": []}}, r"unknown key\(s\) in \[presence\]: exclude"),
    ],
)
def test_malformed_sections_are_rejected(data, message):
    with pytest.raises(config.PolicyError, match=message):
        load(data)


def test_strictest_values_are_allowed():
    policy = load(
        {
            "coverage": {"diff_min": 100, "ratchet_tolerance": 0},
            "mutation": {"min_score": 100},
            "flaky": {"repeats": 100},
            "scorecard": {"min_level": 4},
        }
    )
    assert policy.get("flaky.repeats") == 100
    assert policy.get("scorecard.min_level") == 4


@pytest.mark.parametrize(
    ("key", "a", "b", "stricter"),
    [
        ("coverage.diff_min", 85, 95, 95),
        ("coverage.diff_min", 95, 85, 95),
        ("coverage.ratchet_tolerance", 0.2, 2, 0.2),
        ("coverage.ratchet_tolerance", 2, 0.2, 0.2),
    ],
)
def test_bound_stricter(key, a, b, stricter):
    assert config.BOUNDS[key].stricter(a, b) == stricter


def test_merge_tightens_but_never_loosens():
    base = load(
        {
            "coverage": {"diff_min": 95, "ratchet_tolerance": 0.2},
            "presence": {"exempt": ["gen/**", "old/**"]},
            "waivers": {
                "mutation": {"reason": "merged", "expires": dt.date(2026, 11, 10)},
                "scorecard": {"reason": "merged", "expires": dt.date(2026, 11, 30)},
                "diff-coverage": {"reason": "merged", "expires": dt.date(2026, 11, 30)},
            },
        }
    )
    head = load(
        {
            "coverage": {"diff_min": 85, "ratchet_tolerance": 0.1},
            "mutation": {"min_score": 90},
            "presence": {"exempt": ["gen/**", "new/**"]},
            "waivers": {
                "mutation": {"reason": "extended", "expires": dt.date(2026, 12, 20)},
                "scorecard": {"reason": "shortened", "expires": dt.date(2026, 11, 1)},
                "test-presence": {"reason": "new", "expires": dt.date(2026, 11, 1)},
            },
        }
    )
    policy = config.merge(base, head)
    assert policy.values == {
        "coverage.diff_min": 95,
        "coverage.ratchet_tolerance": 0.1,
        "mutation.min_score": 90,
        "flaky.repeats": 10,
        "scorecard.min_level": 2,
    }
    assert policy.exempt == ("gen/**",)
    # Extending keeps the merged expiry; shortening applies now; removing applies now.
    assert policy.waivers == {
        "mutation": config.Waiver("mutation", "merged", dt.date(2026, 11, 10)),
        "scorecard": config.Waiver("scorecard", "shortened", dt.date(2026, 11, 1)),
    }
    assert policy.deferred == (
        "coverage.diff_min = 85 applies after merge (now 95)",
        "presence.exempt 'new/**' applies after merge",
        "waiver for test-presence applies after merge",
    )


def test_merge_of_identical_policies_defers_nothing():
    policy = load({"coverage": {"diff_min": 95}})
    assert config.merge(policy, policy) == policy


@pytest.fixture
def history(repo):
    """A repository whose base commit holds golden.toml (diff_min = 95)."""
    repo.commit({"golden.toml": "[coverage]\ndiff_min = 95\n"})
    base = repo.git("rev-parse", "HEAD")
    repo.git("checkout", "-qb", "pr")
    return base


def test_load_anchored_to_base(repo, history):
    (repo.root / "golden.toml").write_text("[coverage]\ndiff_min = 85\n")
    policy = config.load(Path("golden.toml"), TODAY, base=history)
    assert policy.get("coverage.diff_min") == 95
    assert policy.deferred == ("coverage.diff_min = 85 applies after merge (now 95)",)
    assert config.load(Path("golden.toml"), TODAY).get("coverage.diff_min") == 85


def test_deleting_the_policy_does_not_loosen_it(repo, history):
    (repo.root / "golden.toml").unlink()
    assert config.load(Path("golden.toml"), TODAY, base=history).get("coverage.diff_min") == 95


def test_policy_added_by_the_change_can_only_tighten(repo):
    repo.commit({"README.md": "x"})
    base = repo.git("rev-parse", "HEAD")
    (repo.root / "golden.toml").write_text(
        "[coverage]\ndiff_min = 85\n[mutation]\nmin_score = 90\n"
    )
    policy = config.load(Path("golden.toml"), TODAY, base=base)
    assert policy.get("coverage.diff_min") == 90
    assert policy.get("mutation.min_score") == 90


def test_read_at(repo, history):
    assert config.read_at(history, Path("golden.toml")) == "[coverage]\ndiff_min = 95\n"
    assert config.read_at(history, Path("nope.toml")) is None
    assert config.read_at(history, (repo.root / "golden.toml").resolve()) is not None
    with pytest.raises(config.PolicyError, match="cannot read the merged policy"):
        config.read_at("0" * 40, Path("golden.toml"))


def test_invalid_merged_policy_falls_back_to_the_change(repo):
    # A floor raised by a platform upgrade makes the merged file invalid; the PR
    # that repairs it must be judged by its own (valid) policy.
    repo.commit({"golden.toml": "[coverage]\ndiff_min = 50\n"})
    base = repo.git("rev-parse", "HEAD")
    (repo.root / "golden.toml").write_text("[coverage]\ndiff_min = 92\n")
    policy = config.load(Path("golden.toml"), TODAY, base=base)
    assert policy.get("coverage.diff_min") == 92
    assert len(policy.deferred) == 1
    assert policy.deferred[0].startswith("merged policy ignored: coverage.diff_min = 50")


def test_invalid_change_policy_is_an_error_even_with_a_base(repo, history):
    (repo.root / "golden.toml").write_text("[coverage]\ndiff_min = 50\n")
    with pytest.raises(config.PolicyError, match="weaker than the platform floor"):
        config.load(Path("golden.toml"), TODAY, base=history)


def test_waiver_expiry_must_be_a_date_not_a_datetime():
    with pytest.raises(config.PolicyError, match="needs `expires"):
        load(waiver(expires=dt.datetime(2026, 11, 1, tzinfo=dt.UTC)))


def test_waiver_may_last_exactly_90_days():
    assert load(waiver(expires=TODAY + dt.timedelta(days=90))).waivers["mutation"]
    with pytest.raises(config.PolicyError, match="at most 90 days"):
        load(waiver(expires=TODAY + dt.timedelta(days=91)))


@pytest.mark.parametrize("glob", ["**", "*", "**/*", "*.py", "**/*.ts", "/**/*.go", " * "])
def test_blanket_exemptions_are_rejected(glob):
    with pytest.raises(config.PolicyError, match="would exempt every file"):
        load({"presence": {"exempt": [glob]}})


@pytest.mark.parametrize("glob", ["src/generated/**", "*_pb2.py", "**/legacy_adapter.py", "gen/*"])
def test_targeted_exemptions_are_allowed(glob):
    assert load({"presence": {"exempt": [glob]}}).exempt == (glob,)
