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
