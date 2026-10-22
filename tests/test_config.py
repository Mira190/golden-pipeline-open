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
