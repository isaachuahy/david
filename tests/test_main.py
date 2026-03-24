import pytest
from unittest.mock import AsyncMock, MagicMock, patch, ANY
from main import (
    done_command,
    handle_message,
    handle_confirm,
    handle_reject,
    handle_start_trigger,
    handle_delay_trigger,
    handle_confirm_weekly_state
)
from orchestrator.session_manager import (
    SESSION_INACTIVITY_TIMEOUT,
    get_session_timeout_job_name,
    timeout_inactive_session,
    reset_session_timeout
)
from reasoning.flash_client import FlashResponse
from persistence.models import CalendarWriteStatus

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
@patch('orchestrator.session_manager.end_session', new_callable=AsyncMock)
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

@pytest.mark.asyncio
@patch('main.confirm_write')
@patch('main.get_pending_write')
@patch('main.remove_pending_write_ui_state')
async def test_handle_confirm_success(mock_remove_ui, mock_get_pending, mock_confirm_write):
    update = MagicMock()
    update.callback_query = MagicMock()
    update.callback_query.data = "confirm_cw_123"
    update.callback_query.answer = AsyncMock()
    update.callback_query.edit_message_text = AsyncMock()
    update.callback_query.message.text = "Original Proposal Text"
    
    context = MagicMock()
    
    # Mock the database record
    mock_record = MagicMock()
    mock_record.status = CalendarWriteStatus.PENDING
    mock_get_pending.return_value = mock_record
    
    # Mock successful execution
    mock_confirm_write.return_value = True
    
    await handle_confirm(update, context)
    
    mock_remove_ui.assert_called_once_with(context, "cw_123")
    mock_confirm_write.assert_called_once_with("cw_123")
    update.callback_query.edit_message_text.assert_awaited_once_with(
        text="Original Proposal Text\n\n✅ *Event Confirmed and Scheduled.*", parse_mode="Markdown"
    )

@pytest.mark.asyncio
@patch('main.reject_write')
@patch('main.get_pending_write')
@patch('main.remove_pending_write_ui_state')
async def test_handle_reject_success(mock_remove_ui, mock_get_pending, mock_reject_write):
    update = MagicMock()
    update.callback_query = MagicMock()
    update.callback_query.data = "reject_cw_456"
    update.callback_query.answer = AsyncMock()
    update.callback_query.edit_message_text = AsyncMock()
    update.callback_query.message.text = "Original Proposal Text"
    
    context = MagicMock()
    
    # Mock the database record
    mock_record = MagicMock()
    mock_record.status = CalendarWriteStatus.PENDING
    mock_get_pending.return_value = mock_record
    
    # Mock successful rejection
    mock_reject_write.return_value = True
    
    await handle_reject(update, context)
    
    mock_remove_ui.assert_called_once_with(context, "cw_456")
    mock_reject_write.assert_called_once_with("cw_456")
    update.callback_query.edit_message_text.assert_awaited_once_with(
        text="Original Proposal Text\n\n🚫 *Event Rejected.*", parse_mode="Markdown"
    )

@pytest.mark.asyncio
@patch('main.consume_trigger')
async def test_handle_start_trigger_daily(mock_consume_trigger):
    update = MagicMock()
    update.callback_query = MagicMock()
    update.callback_query.data = "start_trigger_daily_checkin"
    update.callback_query.answer = AsyncMock()
    update.callback_query.edit_message_text = AsyncMock()
    
    context = MagicMock()
    
    await handle_start_trigger(update, context)
    
    mock_consume_trigger.assert_called_once_with(context, "daily_checkin")
    update.callback_query.edit_message_text.assert_awaited_once_with(
        "🌅 *Daily Check-in Started.* What are your top priorities for today?", parse_mode="Markdown"
    )

@pytest.mark.asyncio
async def test_handle_delay_trigger():
    update = MagicMock()
    update.callback_query = MagicMock()
    update.callback_query.answer = AsyncMock()
    update.callback_query.edit_message_text = AsyncMock()
    
    context = MagicMock()
    
    await handle_delay_trigger(update, context)
    
    update.callback_query.edit_message_text.assert_awaited_once_with("Got it. We will chat first. I'll hold onto this trigger until you're ready.", parse_mode="Markdown")

@pytest.mark.asyncio
@patch('main.execute_weekly_state_update', new_callable=AsyncMock)
async def test_handle_confirm_weekly_state(mock_execute):
    update = MagicMock()
    update.callback_query = MagicMock()
    update.callback_query.answer = AsyncMock()
    context = MagicMock()
    
    await handle_confirm_weekly_state(update, context)
    
    update.callback_query.answer.assert_awaited_once()
    mock_execute.assert_awaited_once_with(update, context)
