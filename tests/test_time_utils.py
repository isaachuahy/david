import pytest

from orchestrator.time_utils import (
    parse_scheduling_datetime,
    validate_scheduling_window,
)


def test_parse_scheduling_datetime_accepts_valid_toronto_offset():
    """A Toronto wall-clock time should retain its valid seasonal offset."""
    parsed = parse_scheduling_datetime(
        "2026-06-18T09:00:00-04:00",
        "America/Toronto",
    )

    assert parsed.isoformat() == "2026-06-18T09:00:00-04:00"
    assert parsed.tzinfo.key == "America/Toronto"


def test_parse_scheduling_datetime_rejects_wrong_but_aware_offset():
    """A UTC timestamp must not silently replace an intended Toronto clock time."""
    with pytest.raises(ValueError, match="offset that is invalid"):
        parse_scheduling_datetime(
            "2026-06-18T09:00:00Z",
            "America/Toronto",
        )


def test_parse_scheduling_datetime_rejects_naive_timestamp():
    """Model proposals must declare an offset instead of relying on server locale."""
    with pytest.raises(ValueError, match="explicit UTC offset"):
        parse_scheduling_datetime(
            "2026-06-18T09:00:00",
            "America/Toronto",
        )


def test_parse_scheduling_datetime_accepts_explicit_utc_intent():
    """UTC remains valid when the proposal explicitly declares UTC as its timezone."""
    parsed = parse_scheduling_datetime(
        "2026-06-18T09:00:00Z",
        "UTC",
    )

    assert parsed.isoformat() == "2026-06-18T09:00:00+00:00"
    assert parsed.tzinfo.key == "UTC"


def test_parse_scheduling_datetime_rejects_unknown_timezone():
    """Invalid timezone names should fail before a proposal reaches persistence."""
    with pytest.raises(ValueError, match="Unknown calendar proposal timezone"):
        parse_scheduling_datetime(
            "2026-06-18T09:00:00-04:00",
            "Canada/Atlantis",
        )


def test_parse_scheduling_datetime_rejects_nonexistent_dst_time():
    """Toronto's spring-forward gap cannot represent a real local event time."""
    with pytest.raises(ValueError, match="offset that is invalid"):
        parse_scheduling_datetime(
            "2026-03-08T02:30:00-05:00",
            "America/Toronto",
        )


@pytest.mark.parametrize(
    "iso_value",
    [
        "2026-11-01T01:30:00-04:00",
        "2026-11-01T01:30:00-05:00",
    ],
)
def test_parse_scheduling_datetime_accepts_both_dst_fold_offsets(iso_value):
    """Both real occurrences of a repeated Toronto fall-back time are valid."""
    parsed = parse_scheduling_datetime(iso_value, "America/Toronto")

    assert parsed.isoformat() == iso_value


def test_validate_scheduling_window_rejects_non_positive_duration():
    """An event window must move forward after timezone validation succeeds."""
    with pytest.raises(ValueError, match="later than start_time"):
        validate_scheduling_window(
            "2026-06-18T10:00:00-04:00",
            "2026-06-18T09:00:00-04:00",
            "America/Toronto",
        )
