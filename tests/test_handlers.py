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
    send_proposal_thread,
    send_review_stage_gate,
    test_schedule as handler_test_schedule,
)
from orchestrator.session_manager import (
    SESSION_INACTIVITY_TIMEOUT,
    get_session_timeout_job_name,
    timeout_inactive_session
)
from reasoning.flash_client import FlashResponse
from reasoning.schemas import ProposalThreadDraft, ProposedEvent
from persistence.models import (
    ArtifactChangeSummary,
    ArtifactType,
    ArtifactWriteRecord,
    ArtifactWriteSourceType,
    ArtifactWriteStatus,
    CalendarWriteStatus,
    ProposalItemRecord,
    ProposalItemStatus,
    ProposalThreadRecord,
    ProposalThreadStatus,
    ReviewStage,
    ReviewWorkflowRecord,
    ReviewWorkflowStatus,
    SchedulingPassArtifact,
    SourceSnapshot,
    StageCheckpoint,
    StageStatus,
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
@patch('bot.handlers.send_proposal_thread', new_callable=AsyncMock)
@patch('bot.handlers.process_message', new_callable=AsyncMock)
async def test_handle_message_rolls_back_failed_proposal_turn_from_chat_history(
    mock_process_message,
    mock_send_proposal_thread,
):
    proposal_thread = ProposalThreadDraft(
        title="Cancel ambiguous event",
        rationale="The user referred to an event without enough detail.",
        proposed_events=[
            ProposedEvent(
                action_type="cancel",
                summary="This event",
                start_time="2026-03-31T09:00:00-04:00",
                end_time="2026-03-31T10:00:00-04:00",
                description="Ambiguous cancellation request.",
            ),
        ],
    )
    mock_process_message.return_value = FlashResponse(
        message="I can cancel that.",
        calendar_planning_mode="propose",
        proposal_thread=proposal_thread,
    )
    mock_send_proposal_thread.side_effect = ValueError(
        "I couldn't identify a single calendar event to cancel."
    )

    update = MagicMock()
    update.effective_chat.id = 456
    update.effective_user.id = 123
    update.message.text = "Cancel this event"
    update.message.reply_text = AsyncMock()

    context = MagicMock()
    context.user_data = {
        "session_state": "ACTIVE",
        "chat_history": [
            {"role": "user", "content": "Earlier request"},
            {"role": "assistant", "content": "Earlier response"},
            {"role": "user", "content": "Cancel this event"},
            {"role": "assistant", "content": "I can cancel that."},
        ],
    }
    context.bot_data = {"allowed_user_id": 123}
    context.job_queue.get_jobs_by_name.return_value = ()

    await handle_message(update, context)

    assert context.user_data["chat_history"] == [
        {"role": "user", "content": "Earlier request"},
        {"role": "assistant", "content": "Earlier response"},
    ]
    update.message.reply_text.assert_awaited_once_with(
        "I couldn't identify a single calendar event to cancel."
    )


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
        "cached_events": [],
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
@patch('bot.handlers._send_proposal_item_clarification', new_callable=AsyncMock)
@patch('bot.handlers.activate_next_proposal_item')
@patch('bot.handlers.mark_proposal_item_in_revision')
@patch('bot.handlers._normalize_calendar_action', new_callable=AsyncMock)
@patch('bot.handlers.add_proposal_item')
@patch('bot.handlers.create_proposal_thread')
async def test_send_proposal_thread_keeps_invalid_item_recoverable(
    mock_create_proposal_thread,
    mock_add_proposal_item,
    mock_normalize_calendar_action,
    mock_mark_in_revision,
    mock_activate_next_proposal_item,
    mock_send_clarification,
):
    mock_normalize_calendar_action.side_effect = ValueError(
        "I couldn't identify a single calendar event to cancel."
    )
    mock_create_proposal_thread.return_value = ProposalThreadRecord(
        id="pt_123",
        source_type="conversation",
        title="Cancel ambiguous event",
        status=ProposalThreadStatus.ACTIVE,
        created_at="2026-03-31T00:00:00+00:00",
        updated_at="2026-03-31T00:00:00+00:00",
    )
    unresolved_item = make_proposal_item(status=ProposalItemStatus.IN_REVISION)
    unresolved_item.action_type = "cancel"
    unresolved_item.summary = "This event"
    mock_add_proposal_item.return_value = unresolved_item
    mock_mark_in_revision.return_value = unresolved_item
    mock_activate_next_proposal_item.return_value = unresolved_item
    mock_send_clarification.return_value = "pi_123"

    context = MagicMock()
    context.user_data = {"current_session_id": "sess_123"}

    proposal_thread = ProposalThreadDraft(
        title="Cancel ambiguous event",
        rationale="The user referred to an event without enough detail.",
        proposed_events=[
            ProposedEvent(
                action_type="cancel",
                summary="This event",
                start_time="2026-03-31T09:00:00-04:00",
                end_time="2026-03-31T10:00:00-04:00",
                description="Ambiguous cancellation request.",
            ),
        ],
    )

    item_id = await send_proposal_thread(
        context,
        456,
        proposal_thread,
        prefix_text="I found a possible cancellation.",
    )

    assert item_id == "pi_123"
    mock_normalize_calendar_action.assert_awaited_once()
    mock_create_proposal_thread.assert_called_once()
    mock_add_proposal_item.assert_called_once()
    assert mock_add_proposal_item.call_args.kwargs["status"] == ProposalItemStatus.IN_REVISION
    mock_mark_in_revision.assert_called_once_with(
        "pi_123",
        feedback="I couldn't identify a single calendar event to cancel.",
    )
    mock_activate_next_proposal_item.assert_called_once_with("pt_123")
    mock_send_clarification.assert_awaited_once_with(
        context,
        456,
        unresolved_item,
        prefix_text="I found a possible cancellation.",
    )


@pytest.mark.asyncio
@patch('bot.handlers._send_proposal_item_confirmation', new_callable=AsyncMock)
@patch('bot.handlers.activate_next_proposal_item')
@patch('bot.handlers.mark_proposal_item_in_revision')
@patch('bot.handlers._normalize_calendar_action', new_callable=AsyncMock)
@patch('bot.handlers.add_proposal_item')
@patch('bot.handlers.create_proposal_thread')
async def test_send_proposal_thread_persists_mixed_batch_in_original_order(
    mock_create_proposal_thread,
    mock_add_proposal_item,
    mock_normalize_calendar_action,
    mock_mark_in_revision,
    mock_activate_next_proposal_item,
    mock_send_confirmation,
):
    mock_create_proposal_thread.return_value = ProposalThreadRecord(
        id="pt_123",
        source_type="conversation",
        title="Mixed calendar changes",
        status=ProposalThreadStatus.ACTIVE,
        created_at="2026-03-31T00:00:00+00:00",
        updated_at="2026-03-31T00:00:00+00:00",
    )

    first_action = ProposedEvent(
        summary="Deep Work",
        start_time="2026-03-31T08:00:00-04:00",
        end_time="2026-03-31T09:30:00-04:00",
        description="Focus block.",
    )
    invalid_action = ProposedEvent(
        action_type="cancel",
        summary="This event",
        start_time="2026-03-31T10:00:00-04:00",
        end_time="2026-03-31T10:30:00-04:00",
        description="Ambiguous cancellation.",
    )
    third_action = ProposedEvent(
        summary="Writing Sprint",
        start_time="2026-03-31T11:00:00-04:00",
        end_time="2026-03-31T12:00:00-04:00",
        description="Draft notes.",
    )
    mock_normalize_calendar_action.side_effect = [
        first_action,
        ValueError("I couldn't identify a single calendar event to cancel."),
        third_action,
    ]

    first_item = make_proposal_item(status=ProposalItemStatus.QUEUED)
    first_item.id = "pi_first"
    unresolved_item = make_proposal_item(status=ProposalItemStatus.IN_REVISION)
    unresolved_item.id = "pi_second"
    third_item = make_proposal_item(status=ProposalItemStatus.QUEUED)
    third_item.id = "pi_third"
    active_first = make_proposal_item(status=ProposalItemStatus.ACTIVE)
    active_first.id = "pi_first"
    mock_add_proposal_item.side_effect = [first_item, unresolved_item, third_item]
    mock_mark_in_revision.return_value = unresolved_item
    mock_activate_next_proposal_item.return_value = active_first
    mock_send_confirmation.return_value = "pi_first"

    context = MagicMock()
    context.user_data = {"current_session_id": "sess_123"}

    result = await send_proposal_thread(
        context,
        456,
        ProposalThreadDraft(
            title="Mixed calendar changes",
            rationale="The user asked for several ordered calendar changes.",
            proposed_events=[first_action, invalid_action, third_action],
        ),
    )

    assert result == "pi_first"
    assert [call.args[1].summary for call in mock_add_proposal_item.call_args_list] == [
        "Deep Work",
        "This event",
        "Writing Sprint",
    ]
    assert [call.kwargs["status"] for call in mock_add_proposal_item.call_args_list] == [
        ProposalItemStatus.QUEUED,
        ProposalItemStatus.IN_REVISION,
        ProposalItemStatus.QUEUED,
    ]
    mock_mark_in_revision.assert_called_once_with(
        "pi_second",
        feedback="I couldn't identify a single calendar event to cancel.",
    )
    mock_activate_next_proposal_item.assert_called_once_with("pt_123")
    mock_send_confirmation.assert_awaited_once()


@pytest.mark.asyncio
@patch('bot.handlers.get_pending_write')
@patch('bot.handlers.get_proposal_item')
@patch('bot.handlers.start_session')
@patch('bot.handlers.process_message', new_callable=AsyncMock)
async def test_handle_message_untracks_stale_proposal_and_routes_normally(
    mock_process_message,
    mock_start_session,
    mock_get_proposal_item,
    mock_get_pending_write,
):
    update = MagicMock()
    update.effective_chat.id = 456
    update.effective_user.id = 123
    update.message.text = "Can you help me think through today instead?"
    update.message.reply_text = AsyncMock()

    context = MagicMock()
    context.user_data = {
        "pending_confirmations": [("pi_stale", 999)],
        "session_state": "IDLE",
    }
    context.bot_data = {"allowed_user_id": 123}
    context.job_queue.get_jobs_by_name.return_value = ()

    mock_get_proposal_item.return_value = None
    mock_get_pending_write.return_value = None
    mock_process_message.return_value = FlashResponse(message="Of course.")

    await handle_message(update, context)

    # A stale proposal item should be cleaned up and then the user's text should
    # continue into normal routing instead of being swallowed as proposal feedback.
    assert context.user_data["pending_confirmations"] == []
    mock_process_message.assert_awaited_once_with(
        "Can you help me think through today instead?",
        context,
    )
    update.message.reply_text.assert_awaited_once_with("Of course.")


@pytest.mark.asyncio
@patch('bot.handlers._send_proposal_item_clarification', new_callable=AsyncMock)
@patch('bot.handlers._normalize_calendar_action', new_callable=AsyncMock)
@patch('bot.handlers.revise_proposal_item')
@patch('bot.handlers.mark_proposal_item_in_revision')
@patch('bot.handlers.get_proposal_item')
@patch('bot.handlers.process_message', new_callable=AsyncMock)
async def test_handle_message_keeps_revised_item_unresolved_when_validation_fails(
    mock_process_message,
    mock_get_proposal_item,
    mock_mark_in_revision,
    mock_revise_proposal_item,
    mock_normalize_calendar_action,
    mock_send_clarification,
):
    update = MagicMock()
    update.effective_chat.id = 456
    update.effective_user.id = 123
    update.message.text = "Cancel this event instead"
    update.message.reply_text = AsyncMock()

    context = MagicMock()
    context.user_data = {
        "pending_confirmations": [("pi_123", 999)],
        "session_state": "ACTIVE",
        "cached_events": [],
    }
    context.bot_data = {"allowed_user_id": 123}
    context.bot.edit_message_text = AsyncMock()
    context.bot.send_message = AsyncMock()

    original_item = make_proposal_item()
    mock_get_proposal_item.return_value = original_item
    mock_process_message.return_value = FlashResponse(
        message="I found a cancellation to propose.",
        calendar_planning_mode="propose",
        proposal_thread=ProposalThreadDraft(
            title="Cancel event",
            rationale="The user asked to revise the active proposal.",
            proposed_events=[
                ProposedEvent(
                    action_type="cancel",
                    summary="This event",
                    start_time="2026-03-31T09:00:00-04:00",
                    end_time="2026-03-31T10:00:00-04:00",
                    description="Ambiguous cancellation request.",
                ),
            ],
        ),
    )
    mock_normalize_calendar_action.side_effect = ValueError(
        "I couldn't identify a single calendar event to cancel."
    )
    updated_item = make_proposal_item(status=ProposalItemStatus.ACTIVE)
    unresolved_item = make_proposal_item(status=ProposalItemStatus.IN_REVISION)
    mock_revise_proposal_item.return_value = updated_item
    mock_mark_in_revision.side_effect = [None, unresolved_item]

    await handle_message(update, context)

    assert mock_mark_in_revision.call_args_list[0].args == ("pi_123",)
    assert mock_mark_in_revision.call_args_list[0].kwargs == {
        "feedback": "Cancel this event instead",
    }
    assert mock_mark_in_revision.call_args_list[1].args == ("pi_123",)
    assert mock_mark_in_revision.call_args_list[1].kwargs == {
        "feedback": "I couldn't identify a single calendar event to cancel.",
    }
    mock_revise_proposal_item.assert_called_once()
    revised_action = mock_revise_proposal_item.call_args.args[1]
    assert revised_action.summary == "This event"
    mock_send_clarification.assert_awaited_once_with(
        context,
        456,
        unresolved_item,
        prefix_text="I still need more detail before I can confirm this calendar change.",
    )
    context.bot.send_message.assert_not_awaited()
    update.message.reply_text.assert_not_awaited()


@pytest.mark.asyncio
@patch('bot.handlers.track_confirmation_message')
@patch('bot.handlers.list_proposal_items')
@patch('bot.handlers.mark_proposal_item_in_revision')
@patch('bot.handlers.get_proposal_item')
@patch('bot.handlers.process_message', new_callable=AsyncMock)
async def test_handle_message_keeps_unresolved_item_tracked_across_clarifications(
    mock_process_message,
    mock_get_proposal_item,
    mock_mark_in_revision,
    mock_list_proposal_items,
    mock_track_confirmation_message,
):
    update = MagicMock()
    update.effective_chat.id = 456
    update.effective_user.id = 123
    update.message.text = "It is the sync with Alex"
    update.message.reply_text = AsyncMock()

    context = MagicMock()
    context.user_data = {
        "pending_confirmations": [("pi_456", 999)],
        "session_state": "ACTIVE",
        "cached_events": [
            {
                "id": "evt_existing",
                "summary": "Existing Meeting",
                "start": {"dateTime": "2026-03-31T10:00:00-04:00"},
                "end": {"dateTime": "2026-03-31T10:30:00-04:00"},
                "calendar_id": "primary",
            },
        ],
    }
    context.bot_data = {"allowed_user_id": 123}
    context.bot.edit_message_text = AsyncMock()
    context.bot.send_message = AsyncMock(return_value=MagicMock(message_id=777))

    prior_item = make_proposal_item(status=ProposalItemStatus.ACCEPTED)
    prior_item.sequence_index = 0
    prior_item.summary = "Deep Work"
    prior_item.start_time = "2026-03-31T08:00:00-04:00"
    prior_item.end_time = "2026-03-31T09:30:00-04:00"

    unresolved_item = make_proposal_item(status=ProposalItemStatus.IN_REVISION)
    unresolved_item.id = "pi_456"
    unresolved_item.sequence_index = 1
    unresolved_item.summary = "This event"
    unresolved_item.last_feedback = "I couldn't identify a single calendar event to cancel."

    mock_get_proposal_item.return_value = unresolved_item
    mock_list_proposal_items.return_value = [prior_item, unresolved_item]
    mock_process_message.return_value = FlashResponse(
        message="Which Alex sync should I use?"
    )

    await handle_message(update, context)

    revision_prompt = mock_process_message.await_args.args[0]
    assert "<CURRENT_CALENDAR_CONTEXT>" in revision_prompt
    assert "Existing Meeting" in revision_prompt
    assert "<PROPOSAL_THREAD_CONTEXT>" in revision_prompt
    assert "Item 1 (accepted)" in revision_prompt
    assert "Deep Work" in revision_prompt
    assert "Item 2 (in_revision)" in revision_prompt
    assert "I couldn't identify a single calendar event to cancel." in revision_prompt
    context.bot.send_message.assert_awaited_once_with(
        chat_id=456,
        text="Which Alex sync should I use?",
    )
    mock_track_confirmation_message.assert_called_once_with(context, "pi_456", 777)
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
async def test_handle_start_trigger_weekly_pauses_at_week_review_confirmation(
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

    review_workflow = ReviewWorkflowRecord(
        id="review_test",
        created_at="2026-04-29T00:00:00+00:00",
        updated_at="2026-04-29T00:00:00+00:00",
        source_snapshot=SourceSnapshot(
            goals_markdown="# Goals",
            weekly_state_markdown="# Weekly State",
            decision_log_markdown="# Decision Log",
        ),
        week_review=StageCheckpoint(
            summary="The week had useful progress.",
            key_findings=["Context routing advanced."],
            constraints=["Avoid late-evening event proposals."],
        ),
    )
    mock_start_weekly_review_workflow.return_value = review_workflow
    mock_send_proposal_thread.return_value = "pi_first"

    await handle_start_trigger(update, context)

    mock_consume_trigger.assert_called_once_with(context, "weekly_review")
    mock_start_weekly_review_workflow.assert_awaited_once_with()
    assert context.user_data["active_review_workflow_id"] == "review_test"
    mock_send_proposal_thread.assert_not_awaited()
    assert context.user_data["active_review_stage_confirmation"] == {
        "review_id": "review_test",
        "stage": "week_review",
    }
    sent_messages = context.bot.send_message.await_args_list
    assert "*Week Review Ready*" in sent_messages[0].kwargs["text"]
    assert "The week had useful progress." in sent_messages[0].kwargs["text"]
    assert "Context routing advanced." in sent_messages[0].kwargs["text"]
    assert sent_messages[0].kwargs["reply_markup"] is not None


@pytest.mark.asyncio
@patch('bot.handlers.send_review_stage_gate', new_callable=AsyncMock)
@patch('bot.handlers.advance_review_from_current_stage', new_callable=AsyncMock)
@patch('bot.handlers.transition_review_stage', new_callable=AsyncMock)
@patch('bot.handlers.load_review_workflow', new_callable=AsyncMock)
async def test_handle_confirm_review_stage_advances_to_next_stage_confirmation(
    mock_load_review_workflow,
    mock_transition_review_stage,
    mock_advance_review_from_current_stage,
    mock_send_review_stage_gate,
):
    update = MagicMock()
    update.effective_user.id = 123
    update.callback_query = MagicMock()
    update.callback_query.data = "confirm_review_stage_week_review"
    update.callback_query.answer = AsyncMock()
    update.callback_query.edit_message_text = AsyncMock()
    update.callback_query.message.chat_id = 456

    context = MagicMock()
    context.user_data = {
        "active_review_workflow_id": "review_test",
        "active_review_stage_confirmation": {
            "review_id": "review_test",
            "stage": "week_review",
        },
    }
    context.bot_data = {"allowed_user_id": 123}

    loaded_record = ReviewWorkflowRecord(
        id="review_test",
        created_at="2026-04-29T00:00:00+00:00",
        updated_at="2026-04-29T00:00:00+00:00",
        source_snapshot=SourceSnapshot(
            goals_markdown="# Goals",
            weekly_state_markdown="# Weekly State",
            decision_log_markdown="# Decision Log",
        ),
        week_review=StageCheckpoint(summary="The week had useful progress."),
    )
    completed_record = loaded_record.model_copy(
        update={
            "workflow_status": ReviewWorkflowStatus.ACTIVE,
            "current_stage": ReviewStage.WEEK_REVIEW,
            "stage_status": StageStatus.COMPLETED,
            "last_completed_stage": ReviewStage.WEEK_REVIEW,
        }
    )
    advanced_record = completed_record.model_copy(
        update={
            "workflow_status": ReviewWorkflowStatus.AWAITING_FEEDBACK,
            "current_stage": ReviewStage.GOALS_AUDIT,
            "stage_status": StageStatus.AWAITING_FEEDBACK,
            "goals_audit": StageCheckpoint(summary="Goals still look accurate."),
        }
    )
    mock_load_review_workflow.return_value = loaded_record
    mock_transition_review_stage.return_value = completed_record
    mock_advance_review_from_current_stage.return_value = advanced_record

    await handle_confirm(update, context)

    mock_load_review_workflow.assert_awaited_once_with("review_test")
    mock_transition_review_stage.assert_awaited_once_with(
        loaded_record,
        workflow_status=ReviewWorkflowStatus.ACTIVE,
        stage=ReviewStage.WEEK_REVIEW,
        stage_status=StageStatus.COMPLETED,
        last_completed_stage=ReviewStage.WEEK_REVIEW,
    )
    mock_advance_review_from_current_stage.assert_awaited_once_with(completed_record)
    mock_send_review_stage_gate.assert_awaited_once_with(
        context,
        456,
        advanced_record,
    )
    assert "active_review_stage_confirmation" not in context.user_data
    update.callback_query.edit_message_text.assert_awaited_once_with(
        "✅ *Week Review confirmed.*",
        parse_mode="Markdown",
    )


@pytest.mark.asyncio
@patch('bot.handlers.send_review_stage_gate', new_callable=AsyncMock)
@patch('bot.handlers.advance_review_from_current_stage', new_callable=AsyncMock)
@patch('bot.handlers.transition_review_stage', new_callable=AsyncMock)
@patch('bot.handlers.load_review_workflow', new_callable=AsyncMock)
@patch('bot.handlers.execute_artifact_write')
@patch('bot.handlers.create_artifact_write')
async def test_handle_confirm_memory_audit_executes_decision_log_artifact_write(
    mock_create_artifact_write,
    mock_execute_artifact_write,
    mock_load_review_workflow,
    mock_transition_review_stage,
    mock_advance_review_from_current_stage,
    mock_send_review_stage_gate,
):
    update = MagicMock()
    update.effective_user.id = 123
    update.callback_query = MagicMock()
    update.callback_query.data = "confirm_review_stage_memory_audit"
    update.callback_query.answer = AsyncMock()
    update.callback_query.edit_message_text = AsyncMock()
    update.callback_query.message.chat_id = 456

    context = MagicMock()
    context.user_data = {
        "active_review_workflow_id": "review_test",
        "active_review_stage_confirmation": {
            "review_id": "review_test",
            "stage": "memory_audit",
        },
    }
    context.bot_data = {"allowed_user_id": 123}

    loaded_record = ReviewWorkflowRecord(
        id="review_test",
        workflow_status=ReviewWorkflowStatus.AWAITING_FEEDBACK,
        current_stage=ReviewStage.MEMORY_AUDIT,
        stage_status=StageStatus.AWAITING_FEEDBACK,
        created_at="2026-04-29T00:00:00+00:00",
        updated_at="2026-04-29T00:00:00+00:00",
        source_snapshot=SourceSnapshot(
            goals_markdown="# Goals",
            weekly_state_markdown="# Weekly State",
            decision_log_markdown="# Decision Log",
        ),
        memory_audit=StageCheckpoint(summary="Memory audit is ready."),
        decision_log_changes=ArtifactChangeSummary(
            additions=["- Keep evenings light."],
            proposed_markdown="# Decision Log\n\n## Current Rolling Context\n- Keep evenings light.\n",
        ),
    )
    completed_record = loaded_record.model_copy(
        update={
            "workflow_status": ReviewWorkflowStatus.ACTIVE,
            "current_stage": ReviewStage.MEMORY_AUDIT,
            "stage_status": StageStatus.COMPLETED,
            "last_completed_stage": ReviewStage.MEMORY_AUDIT,
        }
    )
    advanced_record = completed_record.model_copy(
        update={
            "workflow_status": ReviewWorkflowStatus.AWAITING_FEEDBACK,
            "current_stage": ReviewStage.WEEKLY_PLAN,
            "stage_status": StageStatus.AWAITING_FEEDBACK,
        }
    )
    artifact_write = ArtifactWriteRecord(
        id="awrite_memory",
        artifact_type=ArtifactType.DECISION_LOG,
        content=loaded_record.decision_log_changes.proposed_markdown,
        source_type=ArtifactWriteSourceType.SUNDAY_REVIEW,
        source_id="review_test",
        source_stage=ReviewStage.MEMORY_AUDIT.value,
        created_at="2026-04-29T00:00:00+00:00",
        updated_at="2026-04-29T00:00:00+00:00",
    )
    mock_load_review_workflow.return_value = loaded_record
    mock_create_artifact_write.return_value = artifact_write
    mock_execute_artifact_write.return_value = artifact_write.model_copy(
        update={"status": ArtifactWriteStatus.EXECUTED}
    )
    mock_transition_review_stage.return_value = completed_record
    mock_advance_review_from_current_stage.return_value = advanced_record

    await handle_confirm(update, context)

    mock_create_artifact_write.assert_called_once_with(
        artifact_type=ArtifactType.DECISION_LOG,
        content=loaded_record.decision_log_changes.proposed_markdown,
        source_type=ArtifactWriteSourceType.SUNDAY_REVIEW,
        source_id="review_test",
        source_stage=ReviewStage.MEMORY_AUDIT.value,
    )
    mock_execute_artifact_write.assert_called_once_with(artifact_write)
    mock_transition_review_stage.assert_awaited_once_with(
        loaded_record,
        workflow_status=ReviewWorkflowStatus.ACTIVE,
        stage=ReviewStage.MEMORY_AUDIT,
        stage_status=StageStatus.COMPLETED,
        last_completed_stage=ReviewStage.MEMORY_AUDIT,
    )
    mock_advance_review_from_current_stage.assert_awaited_once_with(completed_record)
    mock_send_review_stage_gate.assert_awaited_once_with(context, 456, advanced_record)


@pytest.mark.asyncio
@patch('bot.handlers.advance_review_from_current_stage', new_callable=AsyncMock)
@patch('bot.handlers.transition_review_stage', new_callable=AsyncMock)
@patch('bot.handlers.load_review_workflow', new_callable=AsyncMock)
@patch('bot.handlers.execute_artifact_write')
@patch('bot.handlers.create_artifact_write')
async def test_handle_confirm_memory_audit_failed_artifact_write_shows_retry(
    mock_create_artifact_write,
    mock_execute_artifact_write,
    mock_load_review_workflow,
    mock_transition_review_stage,
    mock_advance_review_from_current_stage,
):
    update = MagicMock()
    update.effective_user.id = 123
    update.callback_query = MagicMock()
    update.callback_query.data = "confirm_review_stage_memory_audit"
    update.callback_query.answer = AsyncMock()
    update.callback_query.edit_message_text = AsyncMock()
    update.callback_query.message.chat_id = 456

    context = MagicMock()
    context.user_data = {
        "active_review_stage_confirmation": {
            "review_id": "review_test",
            "stage": "memory_audit",
        },
    }
    context.bot_data = {"allowed_user_id": 123}

    loaded_record = ReviewWorkflowRecord(
        id="review_test",
        current_stage=ReviewStage.MEMORY_AUDIT,
        stage_status=StageStatus.AWAITING_FEEDBACK,
        created_at="2026-04-29T00:00:00+00:00",
        updated_at="2026-04-29T00:00:00+00:00",
        source_snapshot=SourceSnapshot(
            goals_markdown="# Goals",
            weekly_state_markdown="# Weekly State",
            decision_log_markdown="# Decision Log",
        ),
        memory_audit=StageCheckpoint(summary="Memory audit is ready."),
        decision_log_changes=ArtifactChangeSummary(
            proposed_markdown="# Decision Log\n\n## Current Rolling Context\n- Keep evenings light.\n",
        ),
    )
    failed_write = ArtifactWriteRecord(
        id="awrite_failed",
        artifact_type=ArtifactType.DECISION_LOG,
        content=loaded_record.decision_log_changes.proposed_markdown,
        source_type=ArtifactWriteSourceType.SUNDAY_REVIEW,
        source_id="review_test",
        source_stage=ReviewStage.MEMORY_AUDIT.value,
        status=ArtifactWriteStatus.FAILED_RETRYABLE,
        created_at="2026-04-29T00:00:00+00:00",
        updated_at="2026-04-29T00:00:00+00:00",
    )
    mock_load_review_workflow.return_value = loaded_record
    mock_create_artifact_write.return_value = failed_write
    mock_execute_artifact_write.return_value = failed_write

    await handle_confirm(update, context)

    assert context.user_data["active_artifact_write_retry"] == {
        "write_id": "awrite_failed",
        "review_id": "review_test",
        "stage": "memory_audit",
    }
    mock_transition_review_stage.assert_not_awaited()
    mock_advance_review_from_current_stage.assert_not_awaited()
    update.callback_query.edit_message_text.assert_awaited_once()
    assert "could not apply" in update.callback_query.edit_message_text.await_args.args[0]
    assert update.callback_query.edit_message_text.await_args.kwargs["reply_markup"] is not None


@pytest.mark.asyncio
@patch('bot.handlers.send_review_stage_gate', new_callable=AsyncMock)
@patch('bot.handlers.advance_review_from_current_stage', new_callable=AsyncMock)
@patch('bot.handlers.transition_review_stage', new_callable=AsyncMock)
@patch('bot.handlers.load_review_workflow', new_callable=AsyncMock)
@patch('bot.handlers.retry_artifact_write')
async def test_handle_retry_artifact_write_success_advances_review(
    mock_retry_artifact_write,
    mock_load_review_workflow,
    mock_transition_review_stage,
    mock_advance_review_from_current_stage,
    mock_send_review_stage_gate,
):
    update = MagicMock()
    update.effective_user.id = 123
    update.callback_query = MagicMock()
    update.callback_query.data = "retry_artifact_write_awrite_failed"
    update.callback_query.answer = AsyncMock()
    update.callback_query.edit_message_text = AsyncMock()
    update.callback_query.message.chat_id = 456

    context = MagicMock()
    context.user_data = {
        "active_artifact_write_retry": {
            "write_id": "awrite_failed",
            "review_id": "review_test",
            "stage": "memory_audit",
        },
    }
    context.bot_data = {"allowed_user_id": 123}

    executed_write = ArtifactWriteRecord(
        id="awrite_failed",
        artifact_type=ArtifactType.DECISION_LOG,
        content="# Decision Log",
        source_type=ArtifactWriteSourceType.SUNDAY_REVIEW,
        source_id="review_test",
        source_stage=ReviewStage.MEMORY_AUDIT.value,
        status=ArtifactWriteStatus.EXECUTED,
        created_at="2026-04-29T00:00:00+00:00",
        updated_at="2026-04-29T00:00:00+00:00",
    )
    loaded_record = ReviewWorkflowRecord(
        id="review_test",
        current_stage=ReviewStage.MEMORY_AUDIT,
        stage_status=StageStatus.AWAITING_FEEDBACK,
        created_at="2026-04-29T00:00:00+00:00",
        updated_at="2026-04-29T00:00:00+00:00",
        source_snapshot=SourceSnapshot(
            goals_markdown="# Goals",
            weekly_state_markdown="# Weekly State",
            decision_log_markdown="# Decision Log",
        ),
    )
    completed_record = loaded_record.model_copy(
        update={
            "workflow_status": ReviewWorkflowStatus.ACTIVE,
            "current_stage": ReviewStage.MEMORY_AUDIT,
            "stage_status": StageStatus.COMPLETED,
            "last_completed_stage": ReviewStage.MEMORY_AUDIT,
        }
    )
    advanced_record = completed_record.model_copy(
        update={
            "workflow_status": ReviewWorkflowStatus.AWAITING_FEEDBACK,
            "current_stage": ReviewStage.WEEKLY_PLAN,
            "stage_status": StageStatus.AWAITING_FEEDBACK,
        }
    )
    mock_retry_artifact_write.return_value = executed_write
    mock_load_review_workflow.return_value = loaded_record
    mock_transition_review_stage.return_value = completed_record
    mock_advance_review_from_current_stage.return_value = advanced_record

    await handle_confirm(update, context)

    mock_retry_artifact_write.assert_called_once_with("awrite_failed")
    assert "active_artifact_write_retry" not in context.user_data
    mock_transition_review_stage.assert_awaited_once_with(
        loaded_record,
        workflow_status=ReviewWorkflowStatus.ACTIVE,
        stage=ReviewStage.MEMORY_AUDIT,
        stage_status=StageStatus.COMPLETED,
        last_completed_stage=ReviewStage.MEMORY_AUDIT,
    )
    mock_send_review_stage_gate.assert_awaited_once_with(context, 456, advanced_record)


@pytest.mark.asyncio
@patch('bot.handlers.retry_artifact_write')
async def test_handle_retry_artifact_write_failure_keeps_retry_button(
    mock_retry_artifact_write,
):
    update = MagicMock()
    update.effective_user.id = 123
    update.callback_query = MagicMock()
    update.callback_query.data = "retry_artifact_write_awrite_failed"
    update.callback_query.answer = AsyncMock()
    update.callback_query.edit_message_text = AsyncMock()

    context = MagicMock()
    context.user_data = {}
    context.bot_data = {"allowed_user_id": 123}

    failed_write = ArtifactWriteRecord(
        id="awrite_failed",
        artifact_type=ArtifactType.DECISION_LOG,
        content="# Decision Log",
        source_type=ArtifactWriteSourceType.SUNDAY_REVIEW,
        source_id="review_test",
        source_stage=ReviewStage.MEMORY_AUDIT.value,
        status=ArtifactWriteStatus.FAILED_RETRYABLE,
        created_at="2026-04-29T00:00:00+00:00",
        updated_at="2026-04-29T00:00:00+00:00",
    )
    mock_retry_artifact_write.return_value = failed_write

    await handle_confirm(update, context)

    assert context.user_data["active_artifact_write_retry"] == {
        "write_id": "awrite_failed",
        "review_id": "review_test",
        "stage": "memory_audit",
    }
    update.callback_query.edit_message_text.assert_awaited_once()
    assert "still could not be applied" in update.callback_query.edit_message_text.await_args.args[0]
    assert update.callback_query.edit_message_text.await_args.kwargs["reply_markup"] is not None


@pytest.mark.asyncio
async def test_send_review_stage_gate_presents_memory_audit_decision_log_changes():
    context = MagicMock()
    context.user_data = {}
    context.bot.send_message = AsyncMock()

    record = ReviewWorkflowRecord(
        id="review_test",
        workflow_status=ReviewWorkflowStatus.AWAITING_FEEDBACK,
        current_stage=ReviewStage.MEMORY_AUDIT,
        stage_status=StageStatus.AWAITING_FEEDBACK,
        created_at="2026-04-29T00:00:00+00:00",
        updated_at="2026-04-29T00:00:00+00:00",
        source_snapshot=SourceSnapshot(
            goals_markdown="# Goals",
            weekly_state_markdown="# Weekly State",
            decision_log_markdown="# Decision Log",
        ),
        memory_audit=StageCheckpoint(
            summary="Memory is useful but one durable preference should be added.",
            key_findings=["Evening constraints have repeated enough to preserve."],
            constraints=["Do not overfit one-off schedule details."],
        ),
        decision_log_changes=ArtifactChangeSummary(
            additions=["David works better when late-evening commitments are avoided."],
            deletions=["Remove duplicated implementation note."],
            modifications=["Compact the rolling-context energy preference."],
        ),
    )

    await send_review_stage_gate(context, 456, record)

    context.bot.send_message.assert_awaited_once()
    sent_text = context.bot.send_message.await_args.kwargs["text"]
    assert "*Memory Audit Ready*" in sent_text
    assert "*Proposed Decision Log Changes:*" in sent_text
    assert "David works better when late-evening commitments are avoided." in sent_text
    assert "Remove duplicated implementation note." in sent_text
    assert "Compact the rolling-context energy preference." in sent_text
    assert context.user_data["active_review_stage_confirmation"] == {
        "review_id": "review_test",
        "stage": "memory_audit",
    }


@pytest.mark.asyncio
@patch('bot.handlers.send_review_stage_gate', new_callable=AsyncMock)
@patch('bot.handlers.revise_review_stage', new_callable=AsyncMock)
@patch('bot.handlers.load_review_workflow', new_callable=AsyncMock)
async def test_handle_message_revises_active_review_stage(
    mock_load_review_workflow,
    mock_revise_review_stage,
    mock_send_review_stage_gate,
):
    update = MagicMock()
    update.effective_chat.id = 456
    update.effective_user.id = 123
    update.message.text = "The week review missed the dentist appointment."
    update.message.reply_text = AsyncMock()

    context = MagicMock()
    context.user_data = {
        "active_review_stage_confirmation": {
            "review_id": "review_test",
            "stage": "week_review",
        },
        "session_state": "ACTIVE",
    }
    context.bot_data = {"allowed_user_id": 123}

    record = ReviewWorkflowRecord(
        id="review_test",
        created_at="2026-04-29T00:00:00+00:00",
        updated_at="2026-04-29T00:00:00+00:00",
        source_snapshot=SourceSnapshot(
            goals_markdown="# Goals",
            weekly_state_markdown="# Weekly State",
            decision_log_markdown="# Decision Log",
        ),
    )
    revised_record = record.model_copy(
        update={
            "workflow_status": ReviewWorkflowStatus.AWAITING_FEEDBACK,
            "current_stage": ReviewStage.WEEK_REVIEW,
            "stage_status": StageStatus.AWAITING_FEEDBACK,
            "week_review": StageCheckpoint(summary="Revised week review."),
        }
    )
    mock_load_review_workflow.return_value = record
    mock_revise_review_stage.return_value = revised_record

    await handle_message(update, context)

    mock_revise_review_stage.assert_awaited_once_with(
        record,
        stage=ReviewStage.WEEK_REVIEW,
        feedback="The week review missed the dentist appointment.",
    )
    assert context.user_data["active_review_stage_confirmation"] == {
        "review_id": "review_test",
        "stage": "week_review",
    }
    update.message.reply_text.assert_awaited_once_with(
        "📝 *Revision applied.*",
        parse_mode="Markdown",
    )
    mock_send_review_stage_gate.assert_awaited_once_with(
        context,
        456,
        revised_record,
    )


@pytest.mark.asyncio
@patch('bot.handlers.send_proposal_thread', new_callable=AsyncMock)
@patch('bot.handlers.advance_review_from_current_stage', new_callable=AsyncMock)
@patch('bot.handlers.transition_review_stage', new_callable=AsyncMock)
@patch('bot.handlers.load_review_workflow', new_callable=AsyncMock)
@patch('bot.handlers.execute_artifact_write')
@patch('bot.handlers.create_artifact_write')
async def test_handle_confirm_weekly_state_advances_and_sends_scheduling_proposals(
    mock_create_artifact_write,
    mock_execute_artifact_write,
    mock_load_review_workflow,
    mock_transition_review_stage,
    mock_advance_review_from_current_stage,
    mock_send_proposal_thread,
):
    update = MagicMock()
    update.effective_chat.id = 456
    update.effective_user.id = 123
    update.callback_query = MagicMock()
    update.callback_query.answer = AsyncMock()
    update.callback_query.edit_message_text = AsyncMock()

    context = MagicMock()
    context.user_data = {
        'active_review_workflow_id': 'review_test',
        'proposed_weekly_state': {
            'content': '# Updated Weekly State',
            'timestamp': datetime.now(timezone.utc).isoformat(),
            'review_id': 'review_test',
        }
    }
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
    loaded_record = ReviewWorkflowRecord(
        id="review_test",
        created_at="2026-04-29T00:00:00+00:00",
        updated_at="2026-04-29T00:00:00+00:00",
        source_snapshot=SourceSnapshot(
            goals_markdown="# Goals",
            weekly_state_markdown="# Weekly State",
            decision_log_markdown="# Decision Log",
        ),
    )
    advanced_record = loaded_record.model_copy(
        update={
            "workflow_status": ReviewWorkflowStatus.AWAITING_FEEDBACK,
            "current_stage": ReviewStage.FINAL_REVIEW,
            "stage_status": StageStatus.AWAITING_FEEDBACK,
            "scheduling_proposals": SchedulingPassArtifact(
                proposed_events=[
                    second_event.model_dump(mode="json"),
                    first_event.model_dump(mode="json"),
                ],
                scheduling_rationale="Protect deep work first, then schedule recovery.",
            ),
        }
    )
    artifact_write = ArtifactWriteRecord(
        id="awrite_weekly",
        artifact_type=ArtifactType.WEEKLY_STATE,
        content="# Updated Weekly State",
        source_type=ArtifactWriteSourceType.SUNDAY_REVIEW,
        source_id="review_test",
        source_stage=ReviewStage.WEEKLY_PLAN.value,
        created_at="2026-04-29T00:00:00+00:00",
        updated_at="2026-04-29T00:00:00+00:00",
    )
    executed_write = artifact_write.model_copy(
        update={"status": ArtifactWriteStatus.EXECUTED}
    )
    mock_create_artifact_write.return_value = artifact_write
    mock_execute_artifact_write.return_value = executed_write
    mock_load_review_workflow.return_value = loaded_record
    mock_transition_review_stage.return_value = loaded_record
    mock_advance_review_from_current_stage.return_value = advanced_record

    await handle_confirm_weekly_state(update, context)

    update.callback_query.answer.assert_awaited_once()
    mock_create_artifact_write.assert_called_once_with(
        artifact_type=ArtifactType.WEEKLY_STATE,
        content="# Updated Weekly State",
        source_type=ArtifactWriteSourceType.SUNDAY_REVIEW,
        source_id="review_test",
        source_stage=ReviewStage.WEEKLY_PLAN.value,
    )
    mock_execute_artifact_write.assert_called_once_with(artifact_write)
    mock_load_review_workflow.assert_awaited_once_with("review_test")
    mock_transition_review_stage.assert_awaited_once_with(
        loaded_record,
        workflow_status=ReviewWorkflowStatus.ACTIVE,
        stage=ReviewStage.WEEKLY_PLAN,
        stage_status=StageStatus.COMPLETED,
        last_completed_stage=ReviewStage.WEEKLY_PLAN,
    )
    mock_advance_review_from_current_stage.assert_awaited_once_with(loaded_record)
    mock_send_proposal_thread.assert_awaited_once()
    kwargs = mock_send_proposal_thread.await_args.kwargs
    assert [event.summary for event in kwargs["proposal_thread"].proposed_events] == [
        "Deep Work Block",
        "Workout",
    ]
    assert kwargs["proposal_thread"].rationale == "Protect deep work first, then schedule recovery."
    assert 'proposed_weekly_state' not in context.user_data
    update.callback_query.edit_message_text.assert_awaited_once_with(
        "✅ *Weekly State successfully updated and backed up.*\n\n"
        "I’ll now prepare any calendar proposals from the accepted weekly plan.",
        parse_mode="Markdown",
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
@patch('bot.handlers._send_proposal_item_clarification', new_callable=AsyncMock)
@patch('bot.handlers.activate_next_proposal_item')
@patch('bot.handlers.mark_proposal_item_accepted')
@patch('bot.handlers.confirm_write')
@patch('bot.handlers.accept_proposal_item')
@patch('bot.handlers.get_proposal_item')
@patch('bot.handlers.untrack_confirmation_message')
async def test_handle_confirm_blocks_on_unresolved_next_item(
    mock_remove_ui,
    mock_get_item,
    mock_accept_proposal_item,
    mock_confirm_write,
    mock_mark_proposal_item_accepted,
    mock_activate_next_proposal_item,
    mock_send_clarification,
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
    unresolved_item = make_proposal_item(status=ProposalItemStatus.IN_REVISION)
    unresolved_item.id = "pi_456"
    unresolved_item.sequence_index = 1
    unresolved_item.summary = "This event"

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
    mock_activate_next_proposal_item.return_value = unresolved_item

    await handle_confirm(update, context)

    mock_mark_proposal_item_accepted.assert_called_once_with("pi_123")
    mock_activate_next_proposal_item.assert_called_once_with("pt_123")
    context.bot.send_message.assert_not_awaited()
    mock_send_clarification.assert_awaited_once_with(
        context,
        456,
        unresolved_item,
        prefix_text="Confirmed. I need one clarification before the next related proposal.",
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
@patch('bot.handlers.complete_review_after_event_feedback', new_callable=AsyncMock)
@patch('bot.handlers.activate_next_proposal_item')
@patch('bot.handlers.reject_proposal_item')
@patch('bot.handlers.get_proposal_item')
@patch('bot.handlers.untrack_confirmation_message')
async def test_handle_reject_completes_weekly_review_proposal_thread(
    mock_remove_ui,
    mock_get_item,
    mock_reject_proposal_item,
    mock_activate_next_proposal_item,
    mock_complete_review_after_event_feedback,
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
    mock_complete_review_after_event_feedback.assert_awaited_once_with(
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
@patch('bot.handlers.load_review_workflow', new_callable=AsyncMock)
@patch('bot.handlers.execute_artifact_write')
@patch('bot.handlers.create_artifact_write')
async def test_handle_confirm_weekly_state_without_review_id(
    mock_create_artifact_write,
    mock_execute_artifact_write,
    mock_load_review_workflow,
):
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
    artifact_write = ArtifactWriteRecord(
        id="awrite_weekly",
        artifact_type=ArtifactType.WEEKLY_STATE,
        content="# Updated Weekly State",
        source_type=ArtifactWriteSourceType.SUNDAY_REVIEW,
        created_at="2026-04-29T00:00:00+00:00",
        updated_at="2026-04-29T00:00:00+00:00",
    )
    mock_create_artifact_write.return_value = artifact_write
    mock_execute_artifact_write.return_value = artifact_write.model_copy(
        update={"status": ArtifactWriteStatus.EXECUTED}
    )
    
    await handle_confirm_weekly_state(update, context)
    
    update.callback_query.answer.assert_awaited_once()
    mock_create_artifact_write.assert_called_once_with(
        artifact_type=ArtifactType.WEEKLY_STATE,
        content="# Updated Weekly State",
        source_type=ArtifactWriteSourceType.SUNDAY_REVIEW,
        source_id=None,
        source_stage=ReviewStage.WEEKLY_PLAN.value,
    )
    mock_execute_artifact_write.assert_called_once_with(artifact_write)
    assert 'proposed_weekly_state' not in context.user_data
    mock_load_review_workflow.assert_not_awaited()
    update.callback_query.edit_message_text.assert_awaited_once_with(
        "✅ *Weekly State successfully updated and backed up.*\n\n"
        "I’ll now prepare any calendar proposals from the accepted weekly plan.",
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
@patch('bot.handlers.transition_review_stage', new_callable=AsyncMock)
@patch('bot.handlers.load_review_workflow', new_callable=AsyncMock)
async def test_handle_reject_weekly_state_marks_weekly_plan_in_revision(
    mock_load_review_workflow,
    mock_transition_review_stage,
):
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
    record = ReviewWorkflowRecord(
        id="review_test",
        created_at="2026-04-29T00:00:00+00:00",
        updated_at="2026-04-29T00:00:00+00:00",
        source_snapshot=SourceSnapshot(
            goals_markdown="# Goals",
            weekly_state_markdown="# Weekly State",
            decision_log_markdown="# Decision Log",
        ),
    )
    mock_load_review_workflow.return_value = record
    
    await handle_reject_weekly_state(update, context)
    
    update.callback_query.answer.assert_awaited_once()
    assert 'proposed_weekly_state' not in context.user_data
    mock_load_review_workflow.assert_awaited_once_with("review_test")
    mock_transition_review_stage.assert_awaited_once_with(
        record,
        stage=ReviewStage.WEEKLY_PLAN,
        stage_status=StageStatus.IN_REVISION,
    )
    update.callback_query.edit_message_text.assert_awaited_once_with(
        "🚫 *Weekly state update rejected. The Sunday review remains open for revision.*",
        parse_mode="Markdown",
    )
