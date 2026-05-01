import pytest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch, ANY
from bot.handlers import (
    done_command,
    handle_message,
    handle_confirm,
    handle_reject,
    handle_start_trigger,
    handle_delay_trigger,
    handle_confirm_weekly_state,
    handle_reject_weekly_state,
    send_calendar_proposal,
    test_schedule as handler_test_schedule,
)
from orchestrator.session_manager import (
    SESSION_INACTIVITY_TIMEOUT,
    get_session_timeout_job_name,
    timeout_inactive_session
)
from reasoning.flash_client import FlashResponse
from reasoning.pro_client import SundayReviewResponse
from reasoning.schemas import ProposalThreadDraft, ProposedEvent
from persistence.models import (
    CalendarWriteStatus,
    ProposalItemRecord,
    ProposalItemStatus,
    ProposalThreadRecord,
    ProposalThreadStatus,
    ReviewWorkflowRecord,
    SourceSnapshot,
)

@pytest.mark.asyncio
@patch('bot.handlers.start_session')
@patch('bot.handlers.process_message', new_callable=AsyncMock)
async def test_handle_message(mock_process_message, mock_start_session):
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
    context.bot_data = {"allowed_user_id": 123}
    context.job_queue.get_jobs_by_name.return_value = ()

    # 2. Act: Call our handler
    await handle_message(update, context)

    # 3. Assert: Verify the routing logic worked correctly
    mock_start_session.assert_called_once_with(context)
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
@patch('bot.handlers.process_message', new_callable=AsyncMock)
async def test_handle_message_drops_unauthorized_user(mock_process_message):
    update = MagicMock()
    update.effective_chat.id = 456
    update.effective_user.id = 999
    update.callback_query = None
    update.message.text = "Hello David"
    update.message.reply_text = AsyncMock()

    context = MagicMock()
    context.user_data = {}
    context.bot_data = {"allowed_user_id": 123}

    await handle_message(update, context)

    mock_process_message.assert_not_awaited()
    update.message.reply_text.assert_not_awaited()


@pytest.mark.asyncio
@patch('bot.handlers.send_calendar_proposal', new_callable=AsyncMock)
@patch('bot.handlers.send_proposal_thread', new_callable=AsyncMock)
@patch('bot.handlers.start_session')
@patch('bot.handlers.process_message', new_callable=AsyncMock)
async def test_handle_message_uses_proposal_thread_when_present(
    mock_process_message,
    mock_start_session,
    mock_send_proposal_thread,
    mock_send_calendar_proposal,
):
    proposal_thread = ProposalThreadDraft(
        title="Moving house schedule",
        rationale="The user asked for several separable moving tasks.",
        proposed_events=[
            ProposedEvent(
                summary="Pack kitchen boxes",
                start_time="2026-03-31T09:00:00-04:00",
                end_time="2026-03-31T11:00:00-04:00",
                description="Pack kitchen items before the move.",
            ),
        ],
    )
    mock_process_message.return_value = FlashResponse(
        message="I drafted a moving-house schedule.",
        calendar_planning_mode="propose",
        proposal_thread=proposal_thread,
    )

    update = MagicMock()
    update.effective_chat.id = 456
    update.effective_user.id = 123
    update.message.text = "Help me schedule moving prep"
    update.message.reply_text = AsyncMock()

    context = MagicMock()
    context.user_data = {}
    context.bot_data = {"allowed_user_id": 123}
    context.job_queue.get_jobs_by_name.return_value = ()

    await handle_message(update, context)

    mock_send_proposal_thread.assert_awaited_once_with(
        context=context,
        chat_id=456,
        proposal_thread=proposal_thread,
        prefix_text="I drafted a moving-house schedule.",
    )
    mock_send_calendar_proposal.assert_not_awaited()
    update.message.reply_text.assert_not_awaited()


@pytest.mark.asyncio
@patch('bot.handlers._send_proposal_item_confirmation', new_callable=AsyncMock)
@patch('bot.handlers.revise_proposal_item')
@patch('bot.handlers.mark_proposal_item_in_revision')
@patch('bot.handlers.get_proposal_item')
@patch('bot.handlers.process_message', new_callable=AsyncMock)
async def test_handle_message_revises_active_proposal_item_in_place(
    mock_process_message,
    mock_get_proposal_item,
    mock_mark_in_revision,
    mock_revise_proposal_item,
    mock_send_confirmation,
):
    update = MagicMock()
    update.effective_chat.id = 456
    update.effective_user.id = 123
    update.message.text = "Move it to 10 instead"
    update.message.reply_text = AsyncMock()

    context = MagicMock()
    context.user_data = {
        "pending_confirmations": [("pi_123", 999)],
        "session_state": "ACTIVE",
    }
    context.bot_data = {"allowed_user_id": 123}
    context.bot.edit_message_text = AsyncMock()

    original_item = make_proposal_item()
    revised_item = make_proposal_item()
    revised_item.start_time = "2026-03-31T10:00:00-04:00"
    revised_action = ProposedEvent(
        summary="Deep Work",
        start_time="2026-03-31T10:00:00-04:00",
        end_time="2026-03-31T12:00:00-04:00",
        description="Focus block.",
    )
    mock_get_proposal_item.return_value = original_item
    mock_process_message.return_value = FlashResponse(
        message="Updated the proposal to 10.",
        calendar_planning_mode="propose",
        proposal_thread=ProposalThreadDraft(
            title="Deep Work",
            rationale="The user asked to revise the active proposal.",
            proposed_events=[revised_action],
        ),
    )
    mock_revise_proposal_item.return_value = revised_item

    await handle_message(update, context)

    context.bot.edit_message_text.assert_awaited_once_with(
        chat_id=456,
        message_id=999,
        text="Revision requested. Retiring this proposal while I update it.",
    )
    mock_mark_in_revision.assert_called_once_with("pi_123", feedback="Move it to 10 instead")
    assert mock_process_message.await_args.args[0].startswith("Revise the active calendar proposal")
    mock_revise_proposal_item.assert_called_once()
    mock_send_confirmation.assert_awaited_once()
    update.message.reply_text.assert_not_awaited()


@pytest.mark.asyncio
@patch('bot.handlers.confirm_write')
async def test_handle_confirm_rejects_unauthorized_callback(mock_confirm_write):
    update = MagicMock()
    update.effective_user.id = 999
    update.callback_query = MagicMock()
    update.callback_query.answer = AsyncMock()
    update.callback_query.edit_message_text = AsyncMock()

    context = MagicMock()
    context.bot_data = {"allowed_user_id": 123}

    await handle_confirm(update, context)

    update.callback_query.answer.assert_awaited_once_with(
        "This action is not available.",
        show_alert=True,
    )
    update.callback_query.edit_message_text.assert_not_awaited()
    mock_confirm_write.assert_not_called()


def make_proposal_item(status=ProposalItemStatus.ACTIVE):
    return ProposalItemRecord(
        id="pi_123",
        thread_id="pt_123",
        status=status,
        sequence_index=0,
        action_type="schedule",
        summary="Deep Work",
        start_time="2026-03-31T09:00:00-04:00",
        end_time="2026-03-31T11:00:00-04:00",
        description="Focus block.",
        calendar_id="primary",
        created_at="2026-03-31T00:00:00+00:00",
        updated_at="2026-03-31T00:00:00+00:00",
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
    context.bot_data = {"allowed_user_id": 123}
    existing_job = MagicMock()
    context.job_queue.get_jobs_by_name.return_value = (existing_job,)

    await done_command(update, context)

    existing_job.schedule_removal.assert_called_once()
    mock_end_session.assert_awaited_once_with(context, 456, user_id=123)

@pytest.mark.asyncio
@patch('bot.handlers.confirm_write')
@patch('bot.handlers.accept_proposal_item')
@patch('bot.handlers.get_proposal_item')
@patch('bot.handlers.untrack_confirmation_message')
async def test_handle_confirm_item_success(
    mock_remove_ui,
    mock_get_item,
    mock_accept_proposal_item,
    mock_confirm_write,
):
    update = MagicMock()
    update.effective_user.id = 123
    update.callback_query = MagicMock()
    update.callback_query.data = "confirm_item_pi_123"
    update.callback_query.answer = AsyncMock()
    update.callback_query.edit_message_text = AsyncMock()
    update.callback_query.message.text = "Original Proposal Text"
    update.callback_query.message.chat_id = 456

    context = MagicMock()
    context.user_data = {}
    context.bot_data = {"allowed_user_id": 123}

    mock_get_item.return_value = make_proposal_item()
    mock_accept_proposal_item.return_value = "cw_123"
    mock_confirm_write.return_value = {
        "id": "evt_123",
        "summary": "Deep Work",
        "start": {"dateTime": "2026-03-31T13:00:00Z"},
    }

    await handle_confirm(update, context)

    mock_remove_ui.assert_called_once_with(context, "pi_123")
    mock_accept_proposal_item.assert_called_once_with("pi_123")
    mock_confirm_write.assert_called_once_with("cw_123")
    update.callback_query.edit_message_text.assert_awaited_once_with(
        text="Original Proposal Text\n\n✅ *Schedule confirmed and executed.*",
        parse_mode="Markdown",
    )
    assert context.user_data["cached_events"][0]["id"] == "evt_123"


@pytest.mark.asyncio
@patch('bot.handlers.confirm_write')
@patch('bot.handlers.get_pending_write')
@patch('bot.handlers.untrack_confirmation_message')
async def test_handle_confirm_legacy_write_success(mock_remove_ui, mock_get_pending, mock_confirm_write):
    update = MagicMock()
    update.effective_user.id = 123
    update.callback_query = MagicMock()
    update.callback_query.data = "confirm_cw_123"
    update.callback_query.answer = AsyncMock()
    update.callback_query.edit_message_text = AsyncMock()
    update.callback_query.message.text = "Original Proposal Text"
    
    context = MagicMock()
    context.user_data = {} # Ensure clean state for cache test
    context.bot_data = {"allowed_user_id": 123}
    
    # Mock the database record
    mock_record = MagicMock()
    mock_record.status = CalendarWriteStatus.PENDING
    mock_record.action_type = "schedule"
    mock_get_pending.return_value = mock_record
    
    # Mock successful execution
    mock_created_event = {
        "id": "evt_123",
        "summary": "Test Event",
        "start": {"dateTime": "2026-03-31T11:00:00Z"},
    }
    mock_confirm_write.return_value = mock_created_event
    
    await handle_confirm(update, context)
    
    mock_remove_ui.assert_called_once_with(context, "cw_123")
    mock_confirm_write.assert_called_once_with("cw_123")
    update.callback_query.edit_message_text.assert_awaited_once_with(
        text="Original Proposal Text\n\n✅ *Schedule confirmed and executed.*", parse_mode="Markdown"
    )
    # Verify that the cache was updated
    assert context.user_data['cached_events'] == [mock_created_event]

@pytest.mark.asyncio
@patch('bot.handlers.reject_proposal_item')
@patch('bot.handlers.get_proposal_item')
@patch('bot.handlers.untrack_confirmation_message')
async def test_handle_reject_item_success(mock_remove_ui, mock_get_item, mock_reject_proposal_item):
    update = MagicMock()
    update.effective_user.id = 123
    update.callback_query = MagicMock()
    update.callback_query.data = "reject_item_pi_123"
    update.callback_query.answer = AsyncMock()
    update.callback_query.edit_message_text = AsyncMock()
    update.callback_query.message.text = "Original Proposal Text"
    update.callback_query.message.chat_id = 456

    context = MagicMock()
    context.user_data = {}
    context.bot_data = {"allowed_user_id": 123}

    item = make_proposal_item()
    mock_get_item.return_value = item
    mock_reject_proposal_item.return_value = item

    await handle_reject(update, context)

    mock_remove_ui.assert_called_once_with(context, "pi_123")
    mock_reject_proposal_item.assert_called_once_with("pi_123")
    update.callback_query.edit_message_text.assert_awaited_once_with(
        text="Original Proposal Text\n\n🚫 *Schedule rejected.*",
        parse_mode="Markdown",
    )


@pytest.mark.asyncio
@patch('bot.handlers.reject_write')
@patch('bot.handlers.get_pending_write')
@patch('bot.handlers.untrack_confirmation_message')
async def test_handle_reject_legacy_write_success(mock_remove_ui, mock_get_pending, mock_reject_write):
    update = MagicMock()
    update.effective_user.id = 123
    update.callback_query = MagicMock()
    update.callback_query.data = "reject_cw_456"
    update.callback_query.answer = AsyncMock()
    update.callback_query.edit_message_text = AsyncMock()
    update.callback_query.message.text = "Original Proposal Text"
    
    context = MagicMock()
    context.bot_data = {"allowed_user_id": 123}
    
    # Mock the database record
    mock_record = MagicMock()
    mock_record.status = CalendarWriteStatus.PENDING
    mock_record.action_type = "schedule"
    mock_get_pending.return_value = mock_record
    
    # Mock successful rejection
    mock_reject_write.return_value = True
    
    await handle_reject(update, context)
    
    mock_remove_ui.assert_called_once_with(context, "cw_456")
    mock_reject_write.assert_called_once_with("cw_456")
    update.callback_query.edit_message_text.assert_awaited_once_with(
        text="Original Proposal Text\n\n🚫 *Schedule rejected.*", parse_mode="Markdown"
    )

@pytest.mark.asyncio
@patch('bot.handlers.consume_trigger')
async def test_handle_start_trigger_daily(mock_consume_trigger):
    update = MagicMock()
    update.effective_user.id = 123
    update.callback_query = MagicMock()
    update.callback_query.data = "start_trigger_daily_checkin"
    update.callback_query.answer = AsyncMock()
    update.callback_query.edit_message_text = AsyncMock()
    
    context = MagicMock()
    context.bot_data = {"allowed_user_id": 123}
    
    await handle_start_trigger(update, context)
    
    mock_consume_trigger.assert_called_once_with(context, "daily_checkin")
    update.callback_query.edit_message_text.assert_awaited_once_with(
        "🌅 *Daily Check-in Started.* What are your top priorities for today?", parse_mode="Markdown"
    )

@pytest.mark.asyncio
@patch('bot.handlers.send_proposal_thread', new_callable=AsyncMock)
@patch('bot.handlers.start_weekly_review_workflow', new_callable=AsyncMock)
@patch('bot.handlers.consume_trigger')
async def test_handle_start_trigger_weekly_queues_one_event_at_a_time(
    mock_consume_trigger,
    mock_start_weekly_review_workflow,
    mock_send_proposal_thread,
):
    update = MagicMock()
    update.effective_chat.id = 456
    update.effective_user.id = 123
    update.callback_query = MagicMock()
    update.callback_query.data = "start_trigger_weekly_review"
    update.callback_query.answer = AsyncMock()
    update.callback_query.edit_message_text = AsyncMock()

    context = MagicMock()
    context.user_data = {}
    context.bot_data = {"allowed_user_id": 123}
    context.bot.send_message = AsyncMock()

    first_event = ProposedEvent(
        summary="Deep Work Block",
        start_time="2026-03-31T09:00:00-04:00",
        end_time="2026-03-31T11:00:00-04:00",
        description="Focus on strategy.",
    )
    second_event = ProposedEvent(
        summary="Workout",
        start_time="2026-03-31T18:00:00-04:00",
        end_time="2026-03-31T19:00:00-04:00",
        description="Strength training.",
    )
    review_workflow = ReviewWorkflowRecord(
        id="review_test",
        created_at="2026-04-29T00:00:00+00:00",
        updated_at="2026-04-29T00:00:00+00:00",
        source_snapshot=SourceSnapshot(
            goals_markdown="# Goals",
            weekly_state_markdown="# Weekly State",
            decision_log_markdown="# Decision Log",
        ),
    )
    review = SundayReviewResponse(
        message="Strong week overall.",
        state_change_summary="Updated priorities for next week.",
        weekly_state_content="# Weekly State",
        proposed_events=[second_event, first_event],
    )
    mock_start_weekly_review_workflow.return_value = (review_workflow, review)
    mock_send_proposal_thread.return_value = "pi_first"

    await handle_start_trigger(update, context)

    mock_consume_trigger.assert_called_once_with(context, "weekly_review")
    mock_start_weekly_review_workflow.assert_awaited_once_with(context)
    assert context.user_data["active_review_workflow_id"] == "review_test"
    mock_send_proposal_thread.assert_awaited_once()
    kwargs = mock_send_proposal_thread.await_args.kwargs
    assert kwargs["context"] is context
    assert kwargs["chat_id"] == 456
    assert kwargs["source_type"] == "weekly_review"
    assert kwargs["source_id"] == "review_test"
    assert [event.summary for event in kwargs["proposal_thread"].proposed_events] == [
        "Deep Work Block",
        "Workout",
    ]
    assert kwargs["prefix_text"] == (
        "📅 *Weekly Review Proposal 1 of 2*\n"
        "Please confirm, reject, or send feedback on this event before I move to the next one."
    )


@pytest.mark.asyncio
@patch('bot.handlers._send_proposal_item_confirmation', new_callable=AsyncMock)
@patch('bot.handlers.activate_next_proposal_item')
@patch('bot.handlers.mark_proposal_item_accepted')
@patch('bot.handlers.confirm_write')
@patch('bot.handlers.accept_proposal_item')
@patch('bot.handlers.get_proposal_item')
@patch('bot.handlers.untrack_confirmation_message')
async def test_handle_confirm_advances_durable_proposal_thread(
    mock_remove_ui,
    mock_get_item,
    mock_accept_proposal_item,
    mock_confirm_write,
    mock_mark_proposal_item_accepted,
    mock_activate_next_proposal_item,
    mock_send_proposal_item_confirmation,
):
    update = MagicMock()
    update.effective_user.id = 123
    update.callback_query = MagicMock()
    update.callback_query.data = "confirm_item_pi_123"
    update.callback_query.answer = AsyncMock()
    update.callback_query.edit_message_text = AsyncMock()
    update.callback_query.message.text = "Original Proposal Text"
    update.callback_query.message.chat_id = 456

    current_item = make_proposal_item()
    next_item = make_proposal_item()
    next_item.id = "pi_456"
    next_item.sequence_index = 1
    next_item.summary = "Follow-up Block"

    context = MagicMock()
    context.user_data = {}
    context.bot_data = {"allowed_user_id": 123}
    context.bot.send_message = AsyncMock()

    mock_get_item.return_value = current_item
    mock_accept_proposal_item.return_value = "cw_123"
    mock_confirm_write.return_value = {
        "id": "evt_123",
        "summary": "Test Event",
        "start": {"dateTime": "2026-03-31T15:00:00Z"},
    }
    mock_activate_next_proposal_item.return_value = next_item

    await handle_confirm(update, context)

    mock_mark_proposal_item_accepted.assert_called_once_with("pi_123")
    mock_activate_next_proposal_item.assert_called_once_with("pt_123")
    context.bot.send_message.assert_awaited_once_with(
        chat_id=456,
        text="Confirmed. Sending the next related proposal now.",
    )
    mock_send_proposal_item_confirmation.assert_awaited_once_with(
        context,
        456,
        next_item,
    )

@pytest.mark.asyncio
@patch('bot.handlers.confirm_write')
@patch('bot.handlers.accept_proposal_item')
@patch('bot.handlers.get_proposal_item')
@patch('bot.handlers.untrack_confirmation_message')
async def test_handle_confirm_keeps_cached_events_in_chronological_order(
    mock_remove_ui,
    mock_get_item,
    mock_accept_proposal_item,
    mock_confirm_write,
):
    update = MagicMock()
    update.effective_user.id = 123
    update.callback_query = MagicMock()
    update.callback_query.data = "confirm_item_pi_123"
    update.callback_query.answer = AsyncMock()
    update.callback_query.edit_message_text = AsyncMock()
    update.callback_query.message.text = "Original Proposal Text"
    update.callback_query.message.chat_id = 456

    context = MagicMock()
    context.user_data = {
        "cached_events": [
            {"id": "evt_later", "summary": "Later Event", "start": {"dateTime": "2026-03-31T15:00:00Z"}},
        ]
    }
    context.bot_data = {"allowed_user_id": 123}

    mock_get_item.return_value = make_proposal_item()
    mock_accept_proposal_item.return_value = "cw_123"
    mock_confirm_write.return_value = {
        "id": "evt_earlier",
        "summary": "Earlier Event",
        "start": {"dateTime": "2026-03-31T13:00:00Z"},
    }

    await handle_confirm(update, context)

    assert [event["summary"] for event in context.user_data["cached_events"]] == [
        "Earlier Event",
        "Later Event",
    ]

@pytest.mark.asyncio
@patch('bot.handlers.apply_bridge_event_feedback', new_callable=AsyncMock)
@patch('bot.handlers.activate_next_proposal_item')
@patch('bot.handlers.reject_proposal_item')
@patch('bot.handlers.get_proposal_item')
@patch('bot.handlers.untrack_confirmation_message')
async def test_handle_reject_completes_weekly_review_proposal_thread(
    mock_remove_ui,
    mock_get_item,
    mock_reject_proposal_item,
    mock_activate_next_proposal_item,
    mock_apply_bridge_event_feedback,
):
    update = MagicMock()
    update.effective_user.id = 123
    update.callback_query = MagicMock()
    update.callback_query.data = "reject_item_pi_456"
    update.callback_query.answer = AsyncMock()
    update.callback_query.edit_message_text = AsyncMock()
    update.callback_query.message.text = "Original Proposal Text"
    update.callback_query.message.chat_id = 456

    context = MagicMock()
    context.user_data = {
        "active_review_workflow_id": "review_test",
    }
    context.bot_data = {"allowed_user_id": 123}
    context.bot.send_message = AsyncMock()

    item = make_proposal_item()
    item.id = "pi_456"
    mock_get_item.return_value = item
    mock_reject_proposal_item.return_value = item
    mock_activate_next_proposal_item.return_value = None

    await handle_reject(update, context)

    mock_activate_next_proposal_item.assert_called_once_with("pt_123")
    mock_apply_bridge_event_feedback.assert_awaited_once_with(
        "review_test",
        has_pending_weekly_state_feedback=False,
    )

@pytest.mark.asyncio
async def test_handle_delay_trigger():
    update = MagicMock()
    update.effective_user.id = 123
    update.callback_query = MagicMock()
    update.callback_query.answer = AsyncMock()
    update.callback_query.edit_message_text = AsyncMock()
    
    context = MagicMock()
    context.bot_data = {"allowed_user_id": 123}
    
    await handle_delay_trigger(update, context)
    
    update.callback_query.edit_message_text.assert_awaited_once_with(
        "Got it - let's chat first. I'll hold onto this trigger until you're ready.",
        parse_mode="Markdown"
    )

@pytest.mark.asyncio
@patch('bot.handlers.execute_weekly_state_update')
async def test_handle_confirm_weekly_state(mock_execute):
    update = MagicMock()
    update.effective_user.id = 123
    update.callback_query = MagicMock()
    update.callback_query.answer = AsyncMock()
    update.callback_query.edit_message_text = AsyncMock()
    context = MagicMock()
    context.user_data = {
        'proposed_weekly_state': {
            'content': '# Updated Weekly State',
            'timestamp': datetime.now(timezone.utc).isoformat(),
        }
    }
    context.bot_data = {"allowed_user_id": 123}
    mock_execute.return_value = True
    
    await handle_confirm_weekly_state(update, context)
    
    update.callback_query.answer.assert_awaited_once()
    mock_execute.assert_called_once_with('# Updated Weekly State')
    assert 'proposed_weekly_state' not in context.user_data
    update.callback_query.edit_message_text.assert_awaited_once_with(
        "✅ *Weekly State successfully updated and backed up.*",
        parse_mode="Markdown"
    )

@pytest.mark.asyncio
@patch('bot.handlers.send_calendar_proposal', new_callable=AsyncMock)
async def test_test_schedule_uses_calendar_proposal_helper(mock_send_calendar_proposal):
    update = MagicMock()
    update.effective_chat.id = 456
    update.effective_user.id = 123
    context = MagicMock()
    context.bot_data = {"allowed_user_id": 123}

    await handler_test_schedule(update, context)

    mock_send_calendar_proposal.assert_awaited_once()
    kwargs = mock_send_calendar_proposal.await_args.kwargs
    assert kwargs["context"] is context
    assert kwargs["chat_id"] == 456
    assert kwargs["action"].summary == "David UI Test Event"
    assert kwargs["action"].description == "Testing the Telegram inline buttons."
    assert kwargs["prefix_text"] == "I propose scheduling 'David UI Test Event' for the next 15 minutes. Does this look good?"

@pytest.mark.asyncio
@patch('bot.handlers.resolve_calendar_reference')
@patch('bot.handlers.track_confirmation_message')
@patch('bot.handlers.build_proposal_item_keyboard')
@patch('bot.handlers.activate_next_proposal_item')
@patch('bot.handlers.add_proposal_item')
@patch('bot.handlers.create_proposal_thread')
async def test_send_calendar_proposal_displays_toronto_time(
    mock_create_proposal_thread,
    mock_add_proposal_item,
    mock_activate_next_proposal_item,
    mock_build_keyboard,
    mock_track_confirmation_message,
    mock_resolve_calendar_reference,
):
    mock_create_proposal_thread.return_value = ProposalThreadRecord(
        id="pt_123",
        source_type="conversation",
        title="Coffee Chat",
        status=ProposalThreadStatus.ACTIVE,
        created_at="2026-03-31T00:00:00+00:00",
        updated_at="2026-03-31T00:00:00+00:00",
    )
    mock_activate_next_proposal_item.return_value = ProposalItemRecord(
        id="pi_123",
        thread_id="pt_123",
        status=ProposalItemStatus.ACTIVE,
        sequence_index=0,
        action_type="schedule",
        summary="Coffee Chat",
        start_time="2026-03-31T09:00:00-04:00",
        end_time="2026-03-31T09:30:00-04:00",
        description="Catch-up downtown.",
        calendar_id="team_calendar@example.com",
        created_at="2026-03-31T00:00:00+00:00",
        updated_at="2026-03-31T00:00:00+00:00",
    )
    mock_build_keyboard.return_value = "keyboard"
    mock_resolve_calendar_reference.return_value = {
        "calendar_id": "team_calendar@example.com",
        "calendar_display_name": "Team Calendar",
    }

    context = MagicMock()
    context.bot.send_message = AsyncMock(return_value=MagicMock(message_id=999))
    context.bot_data = {"allowed_user_id": 123}

    await send_calendar_proposal(
        context=context,
        chat_id=456,
        action=ProposedEvent(
            summary="Coffee Chat",
            start_time="2026-03-31T09:00:00-04:00",
            end_time="2026-03-31T09:30:00-04:00",
            description="Catch-up downtown.",
            requested_calendar_text="entertainment calendar",
        ),
        prefix_text="Want me to schedule this?"
    )

    context.bot.send_message.assert_awaited_once()
    mock_resolve_calendar_reference.assert_called_once_with("entertainment calendar")
    mock_create_proposal_thread.assert_called_once()
    mock_add_proposal_item.assert_called_once()
    mock_activate_next_proposal_item.assert_called_once_with("pt_123")
    kwargs = context.bot.send_message.await_args.kwargs
    assert kwargs["chat_id"] == 456
    assert "Calendar: Team Calendar (`team_calendar@example.com`)" in kwargs["text"]
    assert "Start: 2026-03-31 09:00 EDT" in kwargs["text"]
    assert "End: 2026-03-31 09:30 EDT" in kwargs["text"]
    assert "UTC" not in kwargs["text"]
    mock_track_confirmation_message.assert_called_once_with(context, "pi_123", 999)

@pytest.mark.asyncio
@patch('bot.handlers.apply_bridge_weekly_state_feedback', new_callable=AsyncMock)
async def test_handle_reject_weekly_state(mock_apply_bridge_weekly_state_feedback):
    update = MagicMock()
    update.effective_user.id = 123
    update.callback_query = MagicMock()
    update.callback_query.answer = AsyncMock()
    update.callback_query.edit_message_text = AsyncMock()
    
    context = MagicMock()
    context.user_data = {
        'proposed_weekly_state': {
            'content': 'test',
            'timestamp': '2026-03-22T10:00:00Z',
            'review_id': 'review_test',
        }
    }
    context.bot_data = {"allowed_user_id": 123}
    
    await handle_reject_weekly_state(update, context)
    
    update.callback_query.answer.assert_awaited_once()
    assert 'proposed_weekly_state' not in context.user_data
    mock_apply_bridge_weekly_state_feedback.assert_awaited_once_with(
        "review_test",
        accepted=False,
        has_pending_event_feedback=False,
    )
    update.callback_query.edit_message_text.assert_awaited_once_with(
        "🚫 *Weekly state update rejected. The Sunday review remains open for revision.*",
        parse_mode="Markdown",
    )
