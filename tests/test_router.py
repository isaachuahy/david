import pytest
from unittest.mock import patch, MagicMock

from orchestrator.router import process_message, MessageIntent, classify_intent
from reasoning.flash_client import FlashResponse

@pytest.mark.parametrize(
    "text, expected_intent",
    [
        ("reschedule my 2pm", MessageIntent.OPERATIONAL),
        ("let's brainstorm some ideas", MessageIntent.BRAINSTORM),
        ("what are my priorities for the week?", MessageIntent.GOAL_REVIEW),
        ("just a normal message", MessageIntent.OPERATIONAL),  # Default fallback
    ],
)
def test_classify_intent(text, expected_intent):
    """Tests that the heuristic keyword classification correctly identifies message intent."""
    assert classify_intent(text) == expected_intent

@pytest.mark.asyncio
@patch("orchestrator.router.append_chat_history")
@patch("orchestrator.router.generate_flash_response")
@patch("orchestrator.router.get_chat_history")
@patch("orchestrator.router.build_context")
async def test_process_message_orchestration(
    mock_build_context,
    mock_get_chat_history,
    mock_generate_flash,
    mock_append_history,
):
    """Tests that process_message calls all external dependencies in the correct order."""
    mock_build_context.return_value = "<CONTEXT>"
    mock_get_chat_history.return_value = [{"role": "user", "content": "prev"}]
    mock_response = FlashResponse(message="This is a mock response.")
    mock_generate_flash.return_value = mock_response
    context = MagicMock()
    
    result = await process_message("reschedule my 2pm", context)
    
    assert result == mock_response
    mock_build_context.assert_called_once_with(context)
    mock_get_chat_history.assert_called_once_with(context)
    
    mock_generate_flash.assert_called_once_with(
        user_message="reschedule my 2pm",
        context_block="<CONTEXT>",
        chat_history=[{"role": "user", "content": "prev"}],
        thinking_level="low",  # Operational level
    )
    
    assert mock_append_history.call_count == 2
    mock_append_history.assert_any_call(context, "user", "reschedule my 2pm")
    mock_append_history.assert_any_call(context, "assistant", "This is a mock response.")

@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mocked_intent, expected_level",
    [
        (MessageIntent.OPERATIONAL, "low"),
        (MessageIntent.BRAINSTORM, "high"),
        (MessageIntent.GOAL_REVIEW, "high"),
    ],
)
@patch("orchestrator.router.classify_intent")
@patch("orchestrator.router.generate_flash_response")
@patch("orchestrator.router.get_chat_history")
@patch("orchestrator.router.build_context")
@patch("orchestrator.router.append_chat_history")
async def test_process_message_thinking_levels(
    mock_append_history,
    mock_build_context,
    mock_get_chat_history,
    mock_generate_flash,
    mock_classify_intent,
    mocked_intent,
    expected_level,
):
    """Tests that the mapped intent accurately translates to the correct Gemini thinking level."""
    # Verify that classify_intent is correctly mocked to return the desired intent for each test case, and that generate_flash_response is called with the expected thinking level based on that intent.
    mock_classify_intent.return_value = mocked_intent
    mock_generate_flash.return_value = FlashResponse(message="Response")
    mock_build_context.return_value = "<CONTEXT>"
    
    await process_message("any text", MagicMock())
    
    kwargs = mock_generate_flash.call_args.kwargs
    assert kwargs["thinking_level"] == expected_level

@pytest.mark.asyncio
@patch("orchestrator.router.build_context", side_effect=Exception("Mocked error"))
async def test_process_message_error(mock_build_context):
    context = MagicMock()
    with pytest.raises(Exception, match="Mocked error"):
        await process_message("Any user message", context)
    mock_build_context.assert_called_once_with(context)
