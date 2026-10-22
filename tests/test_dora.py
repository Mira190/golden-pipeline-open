import json

import pytest

from golden import dora


def rec(committed, deployed, failed=False, restored=None):
    return {
        "sha": "x",
        "committed_at": committed,
        "deployed_at": deployed,
        "failed": failed,
        "restored_at": restored,
    }


RECORDS = [
    rec("2026-10-01T08:00:00+00:00", "2026-10-01T10:00:00+00:00"),
    rec(
        "2026-10-02T08:00:00+00:00", "2026-10-02T12:00:00+00:00", True, "2026-10-02T13:00:00+00:00"
    ),
    rec("2026-10-03T09:00:00+00:00", "2026-10-03T10:00:00+00:00"),
    rec("2026-10-05T09:00:00+00:00", "2026-10-05T10:00:00+00:00"),
]


def test_four_keys():
    assert dora.compute(RECORDS) == dora.Dora(
        deploys_per_day=1.0,
        lead_time_hours_p50=1.5,
        change_failure_rate=0.25,
        time_to_restore_hours_p50=1.0,
    )


def test_single_deploy():
    result = dora.compute(RECORDS[:1])
    assert result.deploys_per_day == 1.0
    assert result.time_to_restore_hours_p50 is None


def test_failure_without_restore_is_counted_but_not_timed():
    result = dora.compute([rec("2026-10-01T08:00:00+00:00", "2026-10-01T09:00:00+00:00", True)])
    assert result.change_failure_rate == 1.0
    assert result.time_to_restore_hours_p50 is None


def test_empty():
    with pytest.raises(ValueError, match="no deployments"):
        dora.compute([])


def test_rounding_precision():
    records = [
        rec(
            "2026-10-01T00:00:00+00:00",
            "2026-10-01T00:20:00+00:00",
            True,
            "2026-10-01T00:30:00+00:00",
        ),
        rec("2026-10-01T00:00:00+00:00", "2026-10-04T00:00:00+00:00"),
        rec("2026-10-01T00:00:00+00:00", "2026-10-04T00:00:00+00:00"),
    ]
    result = dora.compute(records)
    assert result.deploys_per_day == 1.0  # 3 deploys over 2.986 days
    assert result.lead_time_hours_p50 == 72.0
    assert result.change_failure_rate == 0.333
    assert result.time_to_restore_hours_p50 == 0.17


def line(**fields):
    base = {
        "sha": "x",
        "committed_at": "2026-10-01T08:00:00+00:00",
        "deployed_at": "2026-10-01T10:00:00+00:00",
        "failed": False,
        "restored_at": None,
    }
    return json.dumps({**base, **fields})


def test_parse_log_skips_blank_lines():
    records = dora.parse_log(line() + "\n\n" + line(sha="y") + "\n")
    assert [r["sha"] for r in records] == ["x", "y"]


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("not json", "line 1: not JSON"),
        ("[1]", "line 1: expected a JSON object"),
        ('{"committed_at": "2026-10-01T08:00:00+00:00"}', "line 1: missing deployed_at, failed"),
        (line(committed_at="yesterday"), "line 1: committed_at is not an ISO-8601"),
        (line(deployed_at=5), "line 1: deployed_at is not an ISO-8601"),
        (line(committed_at="2026-10-01T08:00:00"), "line 1: committed_at needs a UTC offset"),
        (line(deployed_at="2026-10-01T07:00:00+00:00"), "deployed_at is before committed_at"),
        (
            line(failed=True, restored_at="2026-10-01T09:00:00+00:00"),
            "restored_at is before deployed_at",
        ),
        ("\n" + line() + "\n" + line(failed=None) + "x", "line 3: not JSON"),
    ],
)
def test_parse_log_names_the_bad_line(text, message):
    with pytest.raises(ValueError, match=message):
        dora.parse_log(text)


def test_parse_log_accepts_equal_and_null_times():
    text = line(deployed_at="2026-10-01T08:00:00+00:00", failed=True, restored_at=None)
    assert dora.parse_log(text)[0]["failed"] is True
    restored = line(failed=True, restored_at="2026-10-01T10:00:00+00:00")
    assert dora.parse_log(restored)[0]["restored_at"] == "2026-10-01T10:00:00+00:00"


def test_rework_rate():
    records = [{**r, "rework": i == 2} for i, r in enumerate(RECORDS)]
    assert dora.compute(records).deployment_rework_rate == 0.25
    assert dora.compute(RECORDS).deployment_rework_rate is None
    partial = [RECORDS[0], {**RECORDS[1], "rework": 1}, {**RECORDS[2], "rework": 0}]
    assert dora.compute(partial).deployment_rework_rate == 0.5
    thirds = [{**r, "rework": i == 0} for i, r in enumerate(RECORDS[:3])]
    assert dora.compute(thirds).deployment_rework_rate == 0.333
