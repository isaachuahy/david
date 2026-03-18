import pytest
from unittest.mock import AsyncMock, MagicMock, patch, ANY
from main import (
    SESSION_INACTIVITY_TIMEOUT,
    done_command,
    get_session_timeout_job_name,
    handle_message,
    reset_session_timeout,
    timeout_inactive_session,
)
from reasoning.flash_client import FlashResponse

@pytest.mark.asyncio
@patch('main.build_context')
@patch('main.generate_flash_response')
async def test_handle_message(mock_generate_flash, mock_build_context):
    # 1. Arrange: Set up our mocks
    # Mock the context builder output
    mock_build_context.return_value = "<CONTEXT>Mocked context</CONTEXT>"
    
    # Mock the Gemini Flash response
    mock_flash_response = FlashResponse(
        message="This is a mocked response from David.",
        should_escalate=False,
        escalation_reason=None
    )
    mock_generate_flash.return_value = mock_flash_response
    
    # Mock the Telegram Update and Context objects
    update = MagicMock()
    update.effective_chat.id = 456
    update.effective_user.id = 123
    update.message.text = "Hello David"
    update.message.reply_text = AsyncMock() # Must be AsyncMock because we 'await' it
    
    context = MagicMock()
    context.user_data = {}
    context.job_queue.get_jobs_by_name.return_value = ()

    # 2. Act: Call our handler
    await handle_message(update, context)

    # 3. Assert: Verify the routing logic worked correctly
    mock_build_context.assert_called_once()
    mock_generate_flash.assert_called_once_with(
        user_message="Hello David", 
        context_block="<CONTEXT>Mocked context</CONTEXT>",
        chat_history=ANY
    )
    update.message.reply_text.assert_called_once_with("This is a mocked response from David.")
    context.job_queue.run_once.assert_called_once_with(
        timeout_inactive_session,
        SESSION_INACTIVITY_TIMEOUT,
        data={"chat_id": 456},
        name=get_session_timeout_job_name(123),
        chat_id=456,
        user_id=123,
    )
    
    assert len(context.user_data['chat_history']) == 2
    assert context.user_data['chat_history'][0] == {"role": "user", "content": "Hello David"}
    assert context.user_data['chat_history'][1] == {"role": "assistant", "content": "This is a mocked response from David."}

def test_reset_session_timeout_replaces_existing_job():
    context = MagicMock()
    existing_job = MagicMock()
    context.job_queue.get_jobs_by_name.return_value = (existing_job,)

    reset_session_timeout(context, chat_id=456, user_id=123)

    existing_job.schedule_removal.assert_called_once()
    context.job_queue.run_once.assert_called_once_with(
        timeout_inactive_session,
        SESSION_INACTIVITY_TIMEOUT,
        data={"chat_id": 456},
        name=get_session_timeout_job_name(123),
        chat_id=456,
        user_id=123,
    )

@pytest.mark.asyncio
@patch('main.end_session', new_callable=AsyncMock)
async def test_timeout_inactive_session_closes_active_session(mock_end_session):
    context = MagicMock()
    context.user_data = {"session_state": "ACTIVE"}
    context.job.data = {"chat_id": 456}
    context.bot.send_message = AsyncMock()

    await timeout_inactive_session(context)

    mock_end_session.assert_awaited_once_with(context, 456)
    context.bot.send_message.assert_awaited_once_with(
        chat_id=456,
        text="Session closed after 30 minutes of inactivity. Transcript ready for synthesis."
    )

@pytest.mark.asyncio
@patch('main.end_session', new_callable=AsyncMock)
async def test_done_command_cancels_session_timeout_before_closing(mock_end_session):
    update = MagicMock()
    update.effective_user.id = 123
    update.effective_chat.id = 456
    update.message.reply_text = AsyncMock()

    context = MagicMock()
    context.user_data = {"session_state": "ACTIVE"}
    existing_job = MagicMock()
    context.job_queue.get_jobs_by_name.return_value = (existing_job,)

    await done_command(update, context)

    existing_job.schedule_removal.assert_called_once()
    mock_end_session.assert_awaited_once_with(context, 456)
    update.message.reply_text.assert_awaited_once_with("Session closed. Transcript ready for synthesis.")
