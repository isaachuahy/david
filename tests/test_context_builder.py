import pytest
from unittest.mock import patch, MagicMock

from orchestrator.context_builder import (
    CONTEXT_SECTION_ORDER,
    _format_calendar_events,
    _resolve_requested_sections,
    build_context,
)

@patch("orchestrator.context_builder.resolve_calendar_display_name")
@patch("orchestrator.context_builder.get_upcoming_events")
def test_format_calendar_events_cache_miss(mock_get_events, mock_resolve_calendar_display_name):
    """
    Tests the cache-miss scenario: on the first call, it should hit the API
    and populate the cache.
    """
    # Arrange
    # We only mock summary and start time for simplicity, but in reality these would be more complex event objects.
    mock_events = [{
        "summary": "API Event",
        "start": {"dateTime": "2023-01-01T10:00:00Z"},
        "calendar_id": "team_calendar@example.com",
    }]
    mock_get_events.return_value = mock_events
    mock_resolve_calendar_display_name.return_value = "Team Calendar"
    
    tg_context = MagicMock()
    tg_context.user_data = {} # Empty cache

    # Act
    result = _format_calendar_events(tg_context)

    # Assert
    mock_get_events.assert_called_once()
    assert tg_context.user_data['cached_events'] == mock_events
    assert "API Event" in result
    assert "Calendar: Team Calendar; calendar_id: team_calendar@example.com" in result

@patch("orchestrator.context_builder.resolve_calendar_display_name")
@patch("orchestrator.context_builder.get_upcoming_events")
def test_format_calendar_events_cache_hit(mock_get_events, mock_resolve_calendar_display_name):
    """
    Tests the cache-hit scenario: on subsequent calls, it should use the
    cached data and not hit the API.
    """
    # Arrange
    cached_events = [
        {
            "summary": "Later Event",
            "start": {"dateTime": "2023-01-01T11:00:00Z"},
            "calendar_id": "team_calendar@example.com",
        },
        {
            "summary": "Earlier Event",
            "start": {"dateTime": "2023-01-01T09:00:00Z"},
            "calendar_id": "team_calendar@example.com",
        },
    ]
    mock_resolve_calendar_display_name.return_value = "Team Calendar"
    
    tg_context = MagicMock()
    tg_context.user_data = {'cached_events': cached_events}

    # Act
    result = _format_calendar_events(tg_context)

    # Assert
    mock_get_events.assert_not_called()
    assert result.index("Earlier Event") < result.index("Later Event")
    assert "Calendar: Team Calendar; calendar_id: team_calendar@example.com" in result
    assert "Calendar coverage:" not in result
    assert tg_context.user_data["cached_events"][0]["summary"] == "Earlier Event"

def test_format_calendar_events_no_events():
    """Tests that the function returns the correct fallback string when there are no events."""
    # Arrange
    tg_context = MagicMock()
    tg_context.user_data = {'cached_events': []}

    # Act
    result = _format_calendar_events(tg_context)

    # Assert
    assert result == "No upcoming events scheduled."

@patch("orchestrator.context_builder.resolve_calendar_display_name")
def test_format_calendar_events_labels_local_day_cache_coverage(
    mock_resolve_calendar_display_name,
):
    """
    A morning cache should tell the model that later dates remain unqueried.
    """
    mock_resolve_calendar_display_name.return_value = "Primary"
    tg_context = MagicMock()
    tg_context.user_data = {
        "cached_events": [
            {
                "summary": "Morning Focus",
                "start": {"dateTime": "2026-06-18T09:00:00-04:00"},
                "calendar_id": "primary",
            }
        ],
        "calendar_cache_metadata": {
            "scope": "local_day",
            "start": "2026-06-18T00:00:00-04:00",
            "end": "2026-06-19T00:00:00-04:00",
            "timezone": "America/Toronto",
        },
    }

    result = _format_calendar_events(tg_context)

    assert (
        "Calendar coverage: events below are limited to Thursday, June 18, 2026 "
        "in America/Toronto. Events outside this local day have not been queried."
    ) in result
    assert "Morning Focus" in result


def test_format_calendar_events_labels_empty_local_day_cache_coverage():
    """An empty truncated cache must not imply that future dates are also free."""
    tg_context = MagicMock()
    tg_context.user_data = {
        "cached_events": [],
        "calendar_cache_metadata": {
            "scope": "local_day",
            "start": "2026-06-18T00:00:00-04:00",
            "end": "2026-06-19T00:00:00-04:00",
            "timezone": "America/Toronto",
        },
    }

    result = _format_calendar_events(tg_context)

    assert "Events outside this local day have not been queried." in result
    assert result.endswith("No upcoming events scheduled.")

@patch("orchestrator.context_builder._current_datetime_block", return_value="Today is Thursday, April 9, 2026.")
@patch("orchestrator.context_builder._format_calendar_events", return_value="<CALENDAR_EVENTS>")
@patch("orchestrator.context_builder._read_file_safely")
def test_build_context_structure(mock_read_file, mock_format_calendar, mock_current_datetime):
    """
    Tests that the final context string is assembled correctly with all its parts.
    """
    # Arrange
    def read_side_effect(filename, fallback):
        if filename == "goals.md": return "<GOALS_CONTENT>"
        if filename == "weekly_state.md": return "<WEEKLY_STATE_CONTENT>"
        if filename == "decision_log.md": return "<DECISION_LOG_CONTENT>"
        return fallback
    mock_read_file.side_effect = read_side_effect
    
    tg_context = MagicMock()

    # Act
    result = build_context(tg_context)

    # Assert
    assert "<CURRENT_DATETIME>\nToday is Thursday, April 9, 2026.\n</CURRENT_DATETIME>" in result
    assert "<GOALS>\n<GOALS_CONTENT>\n</GOALS>" in result
    assert "<WEEKLY_STATE>\n<WEEKLY_STATE_CONTENT>\n</WEEKLY_STATE>" in result
    assert "<DECISION_LOG>\n<DECISION_LOG_CONTENT>\n</DECISION_LOG>" in result
    assert "<UPCOMING_CALENDAR>\n<CALENDAR_EVENTS>\n</UPCOMING_CALENDAR>" in result
    mock_current_datetime.assert_called_once_with()
    mock_format_calendar.assert_called_once_with(tg_context)


def test_resolve_requested_sections_uses_renamed_calendar_context_profile():
    """
    Tests that the renamed calendar-focused profile resolves to the expected
    minimal section bundle in canonical order.
    """
    assert _resolve_requested_sections(profile="calendar_context") == (
        "CURRENT_DATETIME",
        "WEEKLY_STATE",
        "UPCOMING_CALENDAR",
    )


def test_resolve_requested_sections_uses_renamed_priority_strategy_profile():
    """
    Tests that the renamed strategy-focused profile resolves to the expected
    long-lived context sections in canonical order.
    """
    assert _resolve_requested_sections(profile="priority_strategy") == (
        "CURRENT_DATETIME",
        "GOALS",
        "WEEKLY_STATE",
        "DECISION_LOG",
    )


def test_resolve_requested_sections_preserves_canonical_order_for_explicit_sections():
    """
    Tests that callers can request explicit sections in any order while the
    builder still emits them in the repository's canonical prompt order.
    """
    assert _resolve_requested_sections(
        sections=("DECISION_LOG", "CURRENT_DATETIME", "GOALS"),
    ) == (
        "CURRENT_DATETIME",
        "GOALS",
        "DECISION_LOG",
    )


def test_resolve_requested_sections_defaults_to_full_context():
    """
    Tests that omitting both sections and profile preserves the original
    full-context behavior for backward compatibility.
    """
    assert _resolve_requested_sections() == CONTEXT_SECTION_ORDER


def test_resolve_requested_sections_rejects_unknown_profile():
    """Tests that invalid profile names fail loudly instead of silently falling back."""
    with pytest.raises(ValueError, match="Unknown context profile"):
        _resolve_requested_sections(profile="strategic")


def test_resolve_requested_sections_rejects_mixed_profile_and_sections():
    """Tests that callers must choose either a profile or explicit sections, not both."""
    with pytest.raises(ValueError, match="Pass either sections or profile, not both"):
        _resolve_requested_sections(
            sections=("CURRENT_DATETIME",),
            profile="lean",
        )


@patch("orchestrator.context_builder._current_datetime_block", return_value="Today is Thursday, April 9, 2026.")
@patch("orchestrator.context_builder._format_calendar_events", return_value="<CALENDAR_EVENTS>")
@patch("orchestrator.context_builder._read_file_safely")
def test_build_context_with_calendar_context_profile_omits_strategy_sections(
    mock_read_file,
    mock_format_calendar,
    mock_current_datetime,
):
    """
    Tests that the calendar-focused profile loads only the sections needed for
    time-aware answers and skips the heavier strategic files.
    """
    mock_read_file.return_value = "<WEEKLY_STATE_CONTENT>"

    tg_context = MagicMock()

    result = build_context(tg_context, profile="calendar_context")

    assert "<CURRENT_DATETIME>\nToday is Thursday, April 9, 2026.\n</CURRENT_DATETIME>" in result
    assert "<WEEKLY_STATE>\n<WEEKLY_STATE_CONTENT>\n</WEEKLY_STATE>" in result
    assert "<UPCOMING_CALENDAR>\n<CALENDAR_EVENTS>\n</UPCOMING_CALENDAR>" in result
    assert "<GOALS>" not in result
    assert "<DECISION_LOG>" not in result
    mock_read_file.assert_called_once_with("weekly_state.md", "No weekly state defined.")
    mock_current_datetime.assert_called_once_with()
    mock_format_calendar.assert_called_once_with(tg_context)


@patch("orchestrator.context_builder._current_datetime_block", return_value="Today is Thursday, April 9, 2026.")
@patch("orchestrator.context_builder._format_calendar_events", return_value="<CALENDAR_EVENTS>")
@patch("orchestrator.context_builder._read_file_safely")
def test_build_context_with_priority_strategy_profile_omits_calendar_section(
    mock_read_file,
    mock_format_calendar,
    mock_current_datetime,
):
    """
    Tests that the strategy-focused profile keeps goals, weekly state, and
    decision memory while skipping calendar work entirely.
    """
    def read_side_effect(filename, fallback):
        if filename == "goals.md":
            return "<GOALS_CONTENT>"
        if filename == "weekly_state.md":
            return "<WEEKLY_STATE_CONTENT>"
        if filename == "decision_log.md":
            return "<DECISION_LOG_CONTENT>"
        return fallback

    mock_read_file.side_effect = read_side_effect

    tg_context = MagicMock()

    result = build_context(tg_context, profile="priority_strategy")

    assert "<CURRENT_DATETIME>\nToday is Thursday, April 9, 2026.\n</CURRENT_DATETIME>" in result
    assert "<GOALS>\n<GOALS_CONTENT>\n</GOALS>" in result
    assert "<WEEKLY_STATE>\n<WEEKLY_STATE_CONTENT>\n</WEEKLY_STATE>" in result
    assert "<DECISION_LOG>\n<DECISION_LOG_CONTENT>\n</DECISION_LOG>" in result
    assert "<UPCOMING_CALENDAR>" not in result
    assert mock_read_file.call_count == 3
    mock_current_datetime.assert_called_once_with()
    mock_format_calendar.assert_not_called()
