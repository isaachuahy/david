import pytest
from unittest.mock import AsyncMock, MagicMock, patch, ANY
from bot.handlers import (
    done_command,
    handle_message,
    handle_confirm,
    handle_reject,
    handle_start_trigger,
    handle_delay_trigger,
    handle_confirm_weekly_state,
    handle_reject_weekly_state
)
from orchestrator.session_manager import (
    SESSION_INACTIVITY_TIMEOUT,
    get_session_timeout_job_name,
    timeout_inactive_session
)
from reasoning.flash_client import FlashResponse
from persistence.models import CalendarWriteStatus

@pytest.mark.asyncio
@patch('bot.handlers.process_message', new_callable=AsyncMock)
async def test_handle_message(mock_process_message):
    # 1. Arrange: Set up our mocks
    mock_flash_response = FlashResponse(
        message="This is a mocked response from David."
    )
    mock_process_message.return_value = mock_flash_response
    
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
    mock_process_message.assert_awaited_once_with("Hello David", context)
    update.message.reply_text.assert_called_once_with("This is a mocked response from David.")
    context.job_queue.run_once.assert_called_once_with(
        timeout_inactive_session,
        SESSION_INACTIVITY_TIMEOUT,
        data={"chat_id": 456},
        name=get_session_timeout_job_name(123),
        chat_id=456,
        user_id=123,
    )

@pytest.mark.asyncio
@patch('bot.handlers.end_session', new_callable=AsyncMock)
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

@pytest.mark.asyncio
@patch('bot.handlers.confirm_write')
@patch('bot.handlers.get_pending_write')
@patch('bot.handlers.untrack_confirmation_message')
async def test_handle_confirm_success(mock_remove_ui, mock_get_pending, mock_confirm_write):
    update = MagicMock()
    update.callback_query = MagicMock()
    update.callback_query.data = "confirm_cw_123"
    update.callback_query.answer = AsyncMock()
    update.callback_query.edit_message_text = AsyncMock()
    update.callback_query.message.text = "Original Proposal Text"
    
    context = MagicMock()
    context.user_data = {} # Ensure clean state for cache test
    
    # Mock the database record
    mock_record = MagicMock()
    mock_record.status = CalendarWriteStatus.PENDING
    mock_get_pending.return_value = mock_record
    
    # Mock successful execution
    mock_created_event = {"id": "evt_123", "summary": "Test Event"}
    mock_confirm_write.return_value = mock_created_event
    
    await handle_confirm(update, context)
    
    mock_remove_ui.assert_called_once_with(context, "cw_123")
    mock_confirm_write.assert_called_once_with("cw_123")
    update.callback_query.edit_message_text.assert_awaited_once_with(
        text="Original Proposal Text\n\n✅ *Event Confirmed and Scheduled.*", parse_mode="Markdown"
    )
    # Verify that the cache was updated
    assert context.user_data['cached_events'] == [mock_created_event]

@pytest.mark.asyncio
@patch('bot.handlers.reject_write')
@patch('bot.handlers.get_pending_write')
@patch('bot.handlers.untrack_confirmation_message')
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
@patch('bot.handlers.consume_trigger')
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
@patch('bot.handlers.execute_weekly_state_update', new_callable=AsyncMock)
async def test_handle_confirm_weekly_state(mock_execute):
    update = MagicMock()
    update.callback_query = MagicMock()
    update.callback_query.answer = AsyncMock()
    context = MagicMock()
    
    await handle_confirm_weekly_state(update, context)
    
    update.callback_query.answer.assert_awaited_once()
    mock_execute.assert_awaited_once_with(update, context)

@pytest.mark.asyncio
async def test_handle_reject_weekly_state():
    update = MagicMock()
    update.callback_query = MagicMock()
    update.callback_query.answer = AsyncMock()
    update.callback_query.edit_message_text = AsyncMock()
    
    context = MagicMock()
    context.user_data = {'proposed_weekly_state': {'content': 'test', 'timestamp': '2026-03-22T10:00:00Z'}}
    
    await handle_reject_weekly_state(update, context)
    
    update.callback_query.answer.assert_awaited_once()
    assert 'proposed_weekly_state' not in context.user_data
    update.callback_query.edit_message_text.assert_awaited_once_with(
        "🚫 *Weekly state update rejected.*", parse_mode="Markdown"
    )