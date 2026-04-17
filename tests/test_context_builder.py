import pytest
from unittest.mock import patch, MagicMock

from orchestrator.context_builder import build_context, _format_calendar_events

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
