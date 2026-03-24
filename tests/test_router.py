import pytest
from unittest.mock import patch, MagicMock

from orchestrator.router import process_message
from reasoning.flash_client import FlashResponse

@pytest.mark.asyncio
@patch("orchestrator.router.append_chat_history")
@patch("orchestrator.router.generate_flash_response")
@patch("orchestrator.router.get_chat_history")
@patch("orchestrator.router.build_context")
async def test_process_message_success(mock_build_context, mock_get_chat_history, mock_generate_flash, mock_append_history):
    # Arrange
    mock_build_context.return_value = "<CONTEXT>"
    mock_get_chat_history.return_value = [{"role": "user", "content": "previous message"}]
    
    mock_response = FlashResponse(
        message="This is a mock response.",
        should_escalate=False,
        escalation_reason=None
    )
    mock_generate_flash.return_value = mock_response
    
    context = MagicMock()
    
    # Act
    result = await process_message("Test user message", context)
    
    # Assert
    assert result == mock_response
    mock_build_context.assert_called_once()
    mock_get_chat_history.assert_called_once_with(context)
    mock_generate_flash.assert_called_once_with(
        user_message="Test user message",
        context_block="<CONTEXT>",
        chat_history=[{"role": "user", "content": "previous message"}]
    )
    
    assert mock_append_history.call_count == 2
    mock_append_history.assert_any_call(context, "user", "Test user message")
    mock_append_history.assert_any_call(context, "assistant", "This is a mock response.")

@pytest.mark.asyncio
@patch("orchestrator.router.build_context", side_effect=Exception("Mocked error"))
async def test_process_message_error(mock_build_context):
    context = MagicMock()
    with pytest.raises(Exception, match="Mocked error"):
        await process_message("Test user message", context)
