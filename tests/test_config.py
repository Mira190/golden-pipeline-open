import datetime as dt

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
