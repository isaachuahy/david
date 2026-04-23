import pytest
from unittest.mock import MagicMock, patch

from orchestrator.router import (
    MessageIntent,
    RoutingDecision,
    _select_context_profile,
    classify_intent,
    process_message,
)
from reasoning.flash_client import FlashResponse


@pytest.mark.parametrize(
    "text, expected_decision",
    [
        (
            "reschedule my 2pm meeting",
            RoutingDecision(
                intent=MessageIntent.OPERATIONAL,
                needs_calendar_context=True,
                needs_strategy_context=False,
            ),
        ),
        (
            "let's brainstorm some ideas for the week",
            RoutingDecision(
                intent=MessageIntent.STRATEGIC,
                needs_calendar_context=False,
                needs_strategy_context=True,
            ),
        ),
        (
            "what are my priorities for the week?",
            RoutingDecision(
                intent=MessageIntent.STRATEGIC,
                needs_calendar_context=False,
                needs_strategy_context=True,
            ),
        ),
        (
            "am I free this afternoon to work on applications?",
            RoutingDecision(
                intent=MessageIntent.STRATEGIC,
                needs_calendar_context=True,
                needs_strategy_context=True,
            ),
        ),
        (
            "just a normal message",
            RoutingDecision(
                intent=MessageIntent.OPERATIONAL,
                needs_calendar_context=False,
                needs_strategy_context=False,
            ),
        ),
    ],
)
def test_classify_intent_returns_routing_decision(text, expected_decision):
    """
    Tests that keyword-based routing flags produce the expected reasoning mode
    and context requirements for representative user messages.
    """
    assert classify_intent(text) == expected_decision


@pytest.mark.parametrize(
    "routing_decision, expected_profile",
    [
        (
            RoutingDecision(
                intent=MessageIntent.OPERATIONAL,
                needs_calendar_context=False,
                needs_strategy_context=False,
            ),
            "lean",
        ),
        (
            RoutingDecision(
                intent=MessageIntent.OPERATIONAL,
                needs_calendar_context=True,
                needs_strategy_context=False,
            ),
            "calendar_context",
        ),
        (
            RoutingDecision(
                intent=MessageIntent.STRATEGIC,
                needs_calendar_context=False,
                needs_strategy_context=True,
            ),
            "priority_strategy",
        ),
        (
            RoutingDecision(
                intent=MessageIntent.STRATEGIC,
                needs_calendar_context=True,
                needs_strategy_context=True,
            ),
            "full",
        ),
    ],
)
def test_select_context_profile_maps_flags_to_smallest_profile(
    routing_decision,
    expected_profile,
):
    """
    Tests that routing flags map to the smallest context profile capable of
    answering the user's request accurately.
    """
    assert _select_context_profile(routing_decision) == expected_profile


@pytest.mark.asyncio
@patch("orchestrator.router.append_chat_history")
@patch("orchestrator.router.generate_flash_response")
@patch("orchestrator.router.get_chat_history")
@patch("orchestrator.router.build_context")
async def test_process_message_orchestration_uses_calendar_context_profile(
    mock_build_context,
    mock_get_chat_history,
    mock_generate_flash,
    mock_append_history,
):
    """
    Tests that a calendar-heavy operational message requests the calendar
    context profile and preserves the low-latency thinking level.
    """
    mock_build_context.return_value = "<CONTEXT>"
    mock_get_chat_history.return_value = [{"role": "user", "content": "prev"}]
    mock_response = FlashResponse(message="This is a mock response.")
    mock_generate_flash.return_value = mock_response
    context = MagicMock()

    result = await process_message("reschedule my 2pm meeting", context)

    assert result == mock_response
    mock_build_context.assert_called_once_with(context, profile="calendar_context")
    mock_get_chat_history.assert_called_once_with(context)
    mock_generate_flash.assert_called_once_with(
        user_message="reschedule my 2pm meeting",
        context_block="<CONTEXT>",
        chat_history=[{"role": "user", "content": "prev"}],
        thinking_level="low",
    )
    assert mock_append_history.call_count == 2
    mock_append_history.assert_any_call(context, "user", "reschedule my 2pm meeting")
    mock_append_history.assert_any_call(context, "assistant", "This is a mock response.")


@pytest.mark.asyncio
@patch("orchestrator.router.append_chat_history")
@patch("orchestrator.router.generate_flash_response")
@patch("orchestrator.router.get_chat_history")
@patch("orchestrator.router.build_context")
async def test_process_message_orchestration_uses_full_profile_for_mixed_message(
    mock_build_context,
    mock_get_chat_history,
    mock_generate_flash,
    mock_append_history,
):
    """
    Tests that a message combining scheduling and priorities requests the full
    context bundle and uses the strategic thinking level.
    """
    mock_build_context.return_value = "<CONTEXT>"
    mock_get_chat_history.return_value = []
    mock_generate_flash.return_value = FlashResponse(message="Response")
    context = MagicMock()

    await process_message("am I free this afternoon to work on applications?", context)

    mock_build_context.assert_called_once_with(context, profile="full")
    kwargs = mock_generate_flash.call_args.kwargs
    assert kwargs["thinking_level"] == "high"


@pytest.mark.asyncio
@patch("orchestrator.router.capture_sentry_exception")
@patch("orchestrator.router.build_context", side_effect=Exception("Mocked error"))
async def test_process_message_error(mock_build_context, mock_capture_exception):
    """Tests that build-context failures still get reported without intent tags."""
    context = MagicMock()

    with pytest.raises(Exception, match="Mocked error"):
        await process_message("Any user message", context)

    mock_build_context.assert_called_once_with(context, profile="lean")
    error = mock_capture_exception.call_args.args[0]
    assert "Mocked error" in str(error)
    mock_capture_exception.assert_called_once_with(
        error,
        component="router",
        operation="process_message",
        tags={"intent": "operational"},
    )


@pytest.mark.asyncio
@patch("orchestrator.router.capture_sentry_exception")
@patch(
    "orchestrator.router.classify_intent",
    return_value=RoutingDecision(
        intent=MessageIntent.STRATEGIC,
        needs_calendar_context=False,
        needs_strategy_context=True,
    ),
)
@patch("orchestrator.router.generate_flash_response", side_effect=RuntimeError("Gemini failed"))
@patch("orchestrator.router.get_chat_history", return_value=[])
@patch("orchestrator.router.build_context", return_value="<CONTEXT>")
async def test_process_message_error_reports_strategic_intent_tag(
    mock_build_context,
    mock_get_chat_history,
    mock_generate_flash_response,
    mock_classify_intent,
    mock_capture_exception,
):
    """
    Tests that downstream reasoning failures still report the merged strategic
    intent tag after the classifier refactor.
    """
    context = MagicMock()

    with pytest.raises(RuntimeError, match="Gemini failed"):
        await process_message("let's brainstorm", context)

    mock_build_context.assert_called_once_with(context, profile="priority_strategy")
    mock_capture_exception.assert_called_once_with(
        mock_generate_flash_response.side_effect,
        component="router",
        operation="process_message",
        tags={"intent": "strategic"},
    )
