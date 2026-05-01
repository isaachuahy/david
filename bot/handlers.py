import asyncio
from functools import wraps
from datetime import datetime, timezone, timedelta
from loguru import logger
from telegram import Update
from telegram.ext import ContextTypes

from orchestrator.router import process_message
from orchestrator.confirmation_queue import (
    accept_proposal_item,
    activate_next_proposal_item,
    add_proposal_item,
    confirm_write,
    create_proposal_thread,
    get_pending_write,
    get_proposal_item,
    mark_proposal_item_in_revision,
    mark_proposal_item_accepted,
    reject_proposal_item,
    reject_write,
    revise_proposal_item,
)
from orchestrator.trigger_scheduler import queue_trigger, consume_trigger
from integrations.calendar import resolve_calendar_reference, get_upcoming_events
from orchestrator.session_manager import (
    start_session, end_session, reset_session_timeout, cancel_session_timeout, get_session_state,
    is_session_active, track_confirmation_message, get_tracked_confirmation_messages, 
    untrack_confirmation_message, clear_tracked_confirmation_messages
)
from orchestrator.review_manager import (
    apply_bridge_event_feedback,
    apply_bridge_weekly_state_feedback,
    execute_weekly_state_update,
    start_weekly_review_workflow,
)
from orchestrator.time_utils import USER_TIMEZONE, calendar_event_sort_key, parse_iso, parse_user_datetime, format_user_datetime
from persistence.models import (
    CalendarWriteStatus,
    ProposalItemRecord,
    ProposalItemStatus,
    ReviewWorkflowStatus,
    SessionStatus,
)
from reasoning.flash_client import FlashResponse
from reasoning.schemas import ProposalThreadDraft, ProposedEvent
from bot.keyboards import build_proposal_item_keyboard, build_weekly_state_keyboard
from observability.sentry import capture_exception as capture_sentry_exception

ACTIVE_REVIEW_WORKFLOW_ID_KEY = "active_review_workflow_id"
UNAUTHORIZED_CALLBACK_TEXT = "This action is not available."
CALENDAR_AUTH_ERROR_TEXT = (
    "Google Calendar is currently unavailable because the saved Google authorization "
    "has expired or was revoked. Please refresh the server's calendar token and try again."
)


async def _is_authorized(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Drops any update that does not come from the configured Telegram user."""
    allowed_user_id = context.bot_data.get("allowed_user_id")
    user = update.effective_user

    if allowed_user_id is None:
        logger.error("Authorization is not configured on application bot_data. Dropping update.")
        if update.callback_query:
            await update.callback_query.answer(UNAUTHORIZED_CALLBACK_TEXT, show_alert=True)
        return False

    try:
        allowed_user_id = int(allowed_user_id)
    except (TypeError, ValueError):
        logger.error("Configured allowed_user_id is invalid. Dropping update.")
        if update.callback_query:
            await update.callback_query.answer(UNAUTHORIZED_CALLBACK_TEXT, show_alert=True)
        return False

    if user is None:
        logger.warning("Received update without an effective Telegram user. Dropping update.")
        return False

    if user.id == allowed_user_id:
        return True

    logger.warning(f"Dropped unauthorized update from Telegram user {user.id}.")
    if update.callback_query:
        await update.callback_query.answer(UNAUTHORIZED_CALLBACK_TEXT, show_alert=True)
    return False


def _is_calendar_auth_error(error: Exception) -> bool:
    """
    Detects calendar OAuth failures that should be surfaced to the user directly.

    The bigger picture here is graceful degradation: when Google Calendar auth
    breaks on a headless Lightsail instance, David should explain the operational
    issue instead of replying with a generic failure message.
    """
    auth_error_markers = (
        "invalid_grant",
        "token has been expired or revoked",
        "could not locate runnable browser",
        "oauth credentials not found",
        "failed to refresh token",
    )

    current_error: Exception | None = error
    while current_error is not None:
        message = str(current_error).lower()

        # We walk the exception chain because calendar auth failures may be
        # wrapped by higher-level orchestration errors before reaching the
        # Telegram handler boundary where we decide what the user should see.
        if any(marker in message for marker in auth_error_markers):
            return True

        current_error = current_error.__cause__ or current_error.__context__

    return False


def authorized_only(handler):
    """Decorator that enforces the single-user Telegram access policy."""
    # This is a critical security measure to ensure that only the intended user can interact with the bot, 
    # especially since it has powerful capabilities like reading calendar data, scheduling events and LLM calls.
    # wraps is a standard Python decorator that preserves the original function's metadata (like its name and docstring) 
    # when it's wrapped by another function.
    @wraps(handler)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        if not await _is_authorized(update, context):
            return
        return await handler(update, context, *args, **kwargs)

    return wrapper


def has_pending_weekly_review_event_feedback(context: ContextTypes.DEFAULT_TYPE) -> bool:
    """
    Returns whether the weekly review still has event proposals awaiting feedback.

    Weekly state confirmation is only one part of the review flow. The review
    should remain open if calendar proposals from that same Sunday review are
    still waiting for confirmation.
    """
    return any(
        pending_id.startswith("pi_")
        for pending_id, _message_id in get_tracked_confirmation_messages(context)
    )


def has_pending_weekly_state_feedback(context: ContextTypes.DEFAULT_TYPE) -> bool:
    """
    Returns whether the Sunday review still has a weekly-state confirmation open.

    This is intentionally separate from calendar-event feedback because the
    review may still be incomplete even after all event proposals are resolved.
    """
    proposed_state = context.user_data.get("proposed_weekly_state")
    return isinstance(proposed_state, dict)


async def _send_proposal_item_confirmation(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    item: ProposalItemRecord,
    *,
    prefix_text: str = "",
    calendar_display_name: str | None = None,
) -> str:
    """
    Presents one active proposal item with confirmation controls.

    The buttons point at the proposal item, not a calendar write. Calendar
    writes are created only after the user confirms the active item.
    """
    start_dt = parse_user_datetime(item.start_time)
    end_dt = parse_user_datetime(item.end_time)
    reply_markup = build_proposal_item_keyboard(item.id)

    display_name = calendar_display_name or item.calendar_id
    calendar_line = (
        f"Calendar: {display_name} (`{item.calendar_id}`)"
        if display_name != item.calendar_id
        else f"Calendar ID: `{item.calendar_id}`"
    )

    full_text = f"{prefix_text}\n\n" if prefix_text else ""
    if item.action_type == "cancel":
        full_text += (
            f"🗓️ *Proposed Cancellation:*\n*{item.summary}*\n_{item.description}_\n\n"
            f"{calendar_line}\n"
            f"Current Start: {format_user_datetime(start_dt)}\n"
            f"Current End: {format_user_datetime(end_dt)}"
        )
    elif item.action_type == "reschedule":
        full_text += (
            f"🗓️ *Proposed Reschedule:*\n*{item.summary}*\n_{item.description}_\n\n"
            f"{calendar_line}\n"
            f"New Start: {format_user_datetime(start_dt)}\n"
            f"New End: {format_user_datetime(end_dt)}"
        )
    else:
        full_text += (
            f"🗓️ *Proposed Event:*\n*{item.summary}*\n_{item.description}_\n\n"
            f"{calendar_line}\n"
            f"Start: {format_user_datetime(start_dt)}\n"
            f"End: {format_user_datetime(end_dt)}"
        )

    message = await context.bot.send_message(
        chat_id=chat_id,
        text=full_text.strip(),
        reply_markup=reply_markup,
        parse_mode="Markdown"
    )
    track_confirmation_message(context, item.id, message.message_id)
    return item.id


async def _normalize_calendar_action(
    context: ContextTypes.DEFAULT_TYPE,
    action: ProposedEvent,
) -> ProposedEvent:
    """
    Resolves and normalizes one proposed calendar action before persistence.

    This keeps initial proposals and revised proposals on the same path: both
    get canonical calendar metadata, target-event matching, and ISO datetime
    normalization before becoming durable ProposalItemRecord data.
    """
    resolved_calendar = await asyncio.to_thread(
        resolve_calendar_reference,
        action.requested_calendar_text,
    )
    action.calendar_id = resolved_calendar["calendar_id"]
    action.calendar_display_name = resolved_calendar["calendar_display_name"]

    if action.action_type in ("cancel", "reschedule"):
        if "cached_events" not in context.user_data:
            context.user_data["cached_events"] = await asyncio.to_thread(get_upcoming_events)
        matched_event = _match_existing_event(context.user_data["cached_events"], action)
        if not matched_event:
            raise ValueError(
                "I couldn't identify a single calendar event to "
                f"{action.action_type}. Please include the event title and/or start time."
            )
        action.target_event_id = matched_event.get("id")
        action.target_event_calendar_id = matched_event.get("calendar_id", action.calendar_id)
        if not action.target_event_id:
            raise ValueError("Matched event is missing an event ID and cannot be modified.")

    start_dt = parse_user_datetime(action.start_time)
    end_dt = parse_user_datetime(action.end_time)
    action.start_time = start_dt.isoformat()
    action.end_time = end_dt.isoformat()
    return action


def _extract_revised_calendar_action(response: FlashResponse) -> ProposedEvent | None:
    """
    Extracts the concrete revised event from a Flash response, if present.

    Proposal threads are the only calendar proposal contract; revision turns
    use the first proposed event as the replacement for the active item.
    """
    if response.proposal_thread and response.proposal_thread.proposed_events:
        return response.proposal_thread.proposed_events[0]
    return None


def _format_revision_request(item: ProposalItemRecord, feedback: str) -> str:
    """
    Builds an explicit revision turn for the router.

    The message includes the active draft because chat history alone is not a
    durable enough source of truth for multi-turn proposal-item revisions.
    """
    return (
        "Revise the active calendar proposal using this feedback. "
        "Return a concrete revised proposal only if it is ready for confirmation; "
        "otherwise ask a clarifying question.\n\n"
        "<ACTIVE_PROPOSAL_ITEM>\n"
        f"Action: {item.action_type}\n"
        f"Summary: {item.summary}\n"
        f"Start: {item.start_time}\n"
        f"End: {item.end_time}\n"
        f"Description: {item.description}\n"
        f"Calendar ID: {item.calendar_id}\n"
        "</ACTIVE_PROPOSAL_ITEM>\n\n"
        f"<USER_FEEDBACK>\n{feedback}\n</USER_FEEDBACK>"
    )


async def _revise_active_proposal_item(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    item_id: str,
    message_id: int,
    feedback: str,
) -> bool:
    """
    Revises one active proposal item in place from user feedback.

    The old confirmation message is retired before reasoning starts so stale
    buttons cannot confirm an outdated proposal while the revision is underway.
    """
    item = get_proposal_item(item_id)
    if not item or item.status not in {
        ProposalItemStatus.ACTIVE,
        ProposalItemStatus.IN_REVISION,
    }:
        return False

    try:
        await context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text="Revision requested. Retiring this proposal while I update it.",
        )
    except Exception as error:
        logger.error(f"Failed to retire proposal item UI for {item_id}: {error}")

    mark_proposal_item_in_revision(item_id, feedback=feedback)
    untrack_confirmation_message(context, item_id)

    revision_request = _format_revision_request(item, feedback)
    response = await process_message(revision_request, context)
    revised_action = _extract_revised_calendar_action(response)

    if revised_action is None:
        await context.bot.send_message(chat_id=chat_id, text=response.message)
        return True

    normalized_action = await _normalize_calendar_action(context, revised_action)
    revised_item = revise_proposal_item(
        item_id,
        normalized_action,
        feedback=feedback,
    )
    if revised_item is None:
        await context.bot.send_message(
            chat_id=chat_id,
            text="I couldn't update the active proposal. Please try again.",
        )
        return True

    await _send_proposal_item_confirmation(
        context,
        chat_id,
        revised_item,
        prefix_text=response.message,
        calendar_display_name=normalized_action.calendar_display_name,
    )
    return True


async def _advance_proposal_thread_after_item_resolution(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    item: ProposalItemRecord,
    *,
    outcome_text: str,
) -> bool:
    """
    Presents the next queued item after one proposal item is resolved.

    This advances exactly one step. The next item still needs its own
    confirm/reject/revision turn, so the queue depletes through user decisions
    rather than a single handler call.
    """
    next_item = activate_next_proposal_item(item.thread_id)
    if next_item is None:
        return False

    await context.bot.send_message(
        chat_id=chat_id,
        text=f"{outcome_text}. Sending the next related proposal now.",
    )
    await _send_proposal_item_confirmation(
        context,
        chat_id,
        next_item,
    )
    return True


async def send_calendar_proposal(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    action,
    prefix_text: str = "",
    *,
    source_type: str = "conversation",
    source_id: str | None = None,
    thread_title: str | None = None,
) -> str:
    """
    Compatibility wrapper for one-item calendar proposals.

    New code should prefer send_proposal_thread. This helper keeps older call
    sites working while routing through the same durable proposal-thread path.
    """
    proposal_thread = ProposalThreadDraft(
        title=thread_title or action.summary,
        rationale="Single calendar action proposed from the current conversation turn.",
        proposed_events=[action],
    )
    item_id = await send_proposal_thread(
        context,
        chat_id,
        proposal_thread,
        prefix_text=prefix_text,
        source_type=source_type,
        source_id=source_id,
    )
    if item_id is None:
        raise RuntimeError("Failed to create a proposal item for the calendar action.")
    return item_id


async def send_proposal_thread(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    proposal_thread,
    *,
    prefix_text: str = "",
    source_type: str = "conversation",
    source_id: str | None = None,
) -> str | None:
    """
    Persists a proposed thread and presents its first proposal item.

    calendar_planning_mode is a per-turn model decision; this durable thread is
    the longer-lived state that lets later turns discuss, revise, or confirm
    individual items without losing the shared proposal scope.
    """
    if not proposal_thread.proposed_events:
        return None

    thread = create_proposal_thread(
        source_type=source_type,
        source_id=source_id or context.user_data.get("current_session_id"),
        title=proposal_thread.title,
    )

    first_display_name: str | None = None
    for index, action in enumerate(proposal_thread.proposed_events):
        normalized_action = await _normalize_calendar_action(context, action)
        if index == 0:
            first_display_name = normalized_action.calendar_display_name
        add_proposal_item(
            thread.id,
            normalized_action,
            sequence_index=index,
            status=ProposalItemStatus.QUEUED,
        )

    item = activate_next_proposal_item(thread.id)
    if item is None:
        raise RuntimeError("Failed to activate the first item in the proposal thread.")

    return await _send_proposal_item_confirmation(
        context,
        chat_id,
        item,
        prefix_text=prefix_text,
        calendar_display_name=first_display_name,
    )


def _match_existing_event(cached_events: list[dict], action: ProposedEvent):
    """Attempts to match a cancel/reschedule action to one specific upcoming event."""
    target_summary = (action.target_event_summary or action.summary or "").strip().lower()
    target_start_dt = parse_user_datetime(action.target_event_start_time) if action.target_event_start_time else None

    best_event = None
    best_score = float("-inf")
    for event in cached_events:
        event_summary = event.get("summary", "").strip().lower()
        event_calendar_id = event.get("calendar_id", "primary")
        event_start_raw = event.get("start", {}).get("dateTime", event.get("start", {}).get("date"))
        if not event_start_raw:
            continue

        score = 0.0
        if target_summary:
            if event_summary == target_summary:
                score += 3.0
            elif target_summary in event_summary or event_summary in target_summary:
                score += 1.5
            else:
                continue

        if target_start_dt:
            try:
                event_start_dt = parse_user_datetime(event_start_raw)
                delta_minutes = abs((event_start_dt - target_start_dt).total_seconds()) / 60.0
                score += max(0.0, 2.0 - min(delta_minutes, 120.0) / 60.0)
            except Exception:
                continue

        if action.calendar_id and event_calendar_id == action.calendar_id:
            score += 1.0

        if score > best_score:
            best_score = score
            best_event = event

    return best_event


# Handlers for Telegram bot commands and messages.
# These are the entry points for all user interactions, and they delegate to the Router and other orchestrator components to handle the logic and state management. 
# The handlers also manage session state and ensure that the user experience is smooth and responsive, even when waiting for LLM responses or handling confirmations.

@authorized_only
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles the /start command."""
    user_id = update.effective_user.id
    logger.info(f"User {user_id} started the bot.")
    await update.message.reply_text("Hello! I am David.")

@authorized_only
async def test_trigger(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Temporary command to test the trigger queue."""
    trigger_type = context.args[0] if context.args else "daily_checkin"
    await queue_trigger(context, trigger_type, update.effective_chat.id)

@authorized_only
async def test_schedule(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Temporary command to test the confirmation UI."""
    now = datetime.now(USER_TIMEZONE)
    end = now + timedelta(minutes=15)
    await send_calendar_proposal(
        context=context,
        chat_id=update.effective_chat.id,
        action=ProposedEvent(
            summary="David UI Test Event",
            start_time=now.isoformat(),
            end_time=end.isoformat(),
            description="Testing the Telegram inline buttons."
        ),
        prefix_text="I propose scheduling 'David UI Test Event' for the next 15 minutes. Does this look good?"
    )

@authorized_only
async def handle_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles confirmation of a proposed calendar item or legacy calendar write."""
    query = update.callback_query
    await query.answer()

    if query.data.startswith("confirm_item_"):
        item_id = query.data.split("confirm_item_")[1]
        untrack_confirmation_message(context, item_id)

        item = get_proposal_item(item_id)
        if not item or item.status != ProposalItemStatus.ACTIVE:
            await query.edit_message_text(text="❌ *This proposal is no longer valid or has already been processed.*", parse_mode="Markdown")
            return

        write_id = accept_proposal_item(item_id)
        created_event = await asyncio.to_thread(confirm_write, write_id) if write_id else None
        if created_event:
            mark_proposal_item_accepted(item_id)
            action_label = item.action_type.capitalize()
            text = f"{query.message.text}\n\n✅ *{action_label} confirmed and executed.*"
            if 'cached_events' not in context.user_data:
                context.user_data['cached_events'] = []
            if item.action_type == "cancel":
                context.user_data['cached_events'] = [
                    event
                    for event in context.user_data['cached_events']
                    if event.get("id") != item.target_event_id
                ]
            elif item.action_type == "reschedule":
                existing = [
                    event for event in context.user_data['cached_events']
                    if event.get("id") != item.target_event_id
                ]
                existing.append(created_event)
                context.user_data['cached_events'] = existing
            else:
                context.user_data['cached_events'].append(created_event)
            context.user_data['cached_events'].sort(key=calendar_event_sort_key)
        else:
            text = f"{query.message.text}\n\n❌ *Failed to execute calendar action.*"

        await query.edit_message_text(text=text, parse_mode="Markdown")
        if created_event:
            advanced_thread = await _advance_proposal_thread_after_item_resolution(
                context=context,
                chat_id=query.message.chat_id,
                outcome_text="Confirmed",
                item=item,
            )
            if not advanced_thread:
                review_id = context.user_data.get(ACTIVE_REVIEW_WORKFLOW_ID_KEY)
                if review_id:
                    review_workflow = await apply_bridge_event_feedback(
                        review_id,
                        has_pending_weekly_state_feedback=has_pending_weekly_state_feedback(context),
                    )
                    if (
                        review_workflow is not None
                        and review_workflow.workflow_status == ReviewWorkflowStatus.COMPLETED
                    ):
                        context.user_data.pop(ACTIVE_REVIEW_WORKFLOW_ID_KEY, None)
        return

    write_id = query.data.split("confirm_")[1]
    untrack_confirmation_message(context, write_id)

    record = get_pending_write(write_id)
    if not record or record.status != CalendarWriteStatus.PENDING:
        await query.edit_message_text(text="❌ *This request is no longer valid or has already been processed.*", parse_mode="Markdown")
        return

    created_event = await asyncio.to_thread(confirm_write, write_id)
    if created_event:
        action_label = record.action_type.capitalize()
        text = f"{query.message.text}\n\n✅ *{action_label} confirmed and executed.*"
        # Immediately update the local cache so the LLM knows about this new event
        # Note: This cache is only for the current session and will not persist across sessions.
        # This is a workaround to ensure that if the user schedules an event and then immediately asks David about their schedule, 
        # the new event will be included in the context without needing to wait for the next API fetch cycle.

        # Initialize the in-memory calendar cache if it hasn't been populated yet,
        # then append the newly created event so subsequent context builds see it
        # without waiting for another Calendar API fetch.

        if 'cached_events' not in context.user_data:
            context.user_data['cached_events'] = []
        if record.action_type == "cancel":
            context.user_data['cached_events'] = [
                event
                for event in context.user_data['cached_events']
                if event.get("id") != record.target_event_id
            ]
        elif record.action_type == "reschedule":
            existing = [
                event for event in context.user_data['cached_events']
                if event.get("id") != record.target_event_id
            ]
            existing.append(created_event)
            context.user_data['cached_events'] = existing
        else:
            context.user_data['cached_events'].append(created_event)
        context.user_data['cached_events'].sort(key=calendar_event_sort_key)
    else:
        text = f"{query.message.text}\n\n❌ *Failed to execute calendar action.*"
        
    await query.edit_message_text(text=text, parse_mode="Markdown")

@authorized_only
async def handle_reject(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles rejection of a proposed calendar item or legacy calendar write."""
    query = update.callback_query
    await query.answer()

    if query.data.startswith("reject_item_"):
        item_id = query.data.split("reject_item_")[1]
        untrack_confirmation_message(context, item_id)

        item = get_proposal_item(item_id)
        if not item or item.status != ProposalItemStatus.ACTIVE:
            await query.edit_message_text(text="❌ *This proposal is no longer valid or has already been processed.*", parse_mode="Markdown")
            return

        rejected_item = reject_proposal_item(item_id)
        action_label = item.action_type.capitalize()
        text = (
            f"{query.message.text}\n\n🚫 *{action_label} rejected.*"
            if rejected_item
            else f"{query.message.text}\n\n❌ *Failed to reject calendar action.*"
        )
        await query.edit_message_text(text=text, parse_mode="Markdown")
        if rejected_item:
            advanced_thread = await _advance_proposal_thread_after_item_resolution(
                context=context,
                chat_id=query.message.chat_id,
                outcome_text="Rejected",
                item=item,
            )
            if not advanced_thread:
                review_id = context.user_data.get(ACTIVE_REVIEW_WORKFLOW_ID_KEY)
                if review_id:
                    review_workflow = await apply_bridge_event_feedback(
                        review_id,
                        has_pending_weekly_state_feedback=has_pending_weekly_state_feedback(context),
                    )
                    if (
                        review_workflow is not None
                        and review_workflow.workflow_status == ReviewWorkflowStatus.COMPLETED
                    ):
                        context.user_data.pop(ACTIVE_REVIEW_WORKFLOW_ID_KEY, None)
        return

    write_id = query.data.split("reject_")[1]
    untrack_confirmation_message(context, write_id)
        
    record = get_pending_write(write_id)
    if not record or record.status != CalendarWriteStatus.PENDING:
        await query.edit_message_text(text="❌ *This request is no longer valid or has already been processed.*", parse_mode="Markdown")
        return

    success = reject_write(write_id)
    action_label = record.action_type.capitalize()
    text = f"{query.message.text}\n\n🚫 *{action_label} rejected.*" if success else f"{query.message.text}\n\n❌ *Failed to reject calendar action.*"
    await query.edit_message_text(text=text, parse_mode="Markdown")

@authorized_only
async def handle_start_trigger(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles starting a scheduled trigger."""
    query = update.callback_query
    await query.answer()
    
    trigger_type = query.data.split("start_trigger_")[1]
    if trigger_type == "daily_checkin":
        consume_trigger(context, trigger_type)
        await query.edit_message_text("🌅 *Daily Check-in Started.* What are your top priorities for today?", parse_mode="Markdown")
    elif trigger_type == "weekly_review":
        await query.edit_message_text("📅 *Starting Sunday Review. Analysing your week...*", parse_mode="Markdown")
        try:
            review_workflow, review = await start_weekly_review_workflow(context)
            context.user_data[ACTIVE_REVIEW_WORKFLOW_ID_KEY] = review_workflow.id
            # Only consume the trigger after the review workflow is durable and has
            # actually started. This keeps the trigger retryable if startup fails.
            consume_trigger(context, trigger_type)
            
            # Send the synthesis message
            await context.bot.send_message(
                chat_id=update.effective_chat.id, 
                text=f"**Sunday Review Complete**\n\n{review.message}", 
                parse_mode="Markdown"
            )
            
            # Ask for confirmation before overwriting the weekly state
            context.user_data['proposed_weekly_state'] = {
                "content": review.weekly_state_content,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "review_id": review_workflow.id,
            }
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text=f"📝 *Proposed Weekly State Changes:*\n{review.state_change_summary}\n\nDo you want to apply these changes?",
                reply_markup=build_weekly_state_keyboard(),
                parse_mode="Markdown"
            )
            
            if review.proposed_events:
                sorted_events = sorted(
                    review.proposed_events,
                    key=lambda event: parse_user_datetime(event.start_time),
                )
                await send_proposal_thread(
                    context=context,
                    chat_id=update.effective_chat.id,
                    proposal_thread=ProposalThreadDraft(
                        title="Weekly review calendar proposals",
                        rationale="Calendar proposals generated from the Sunday review.",
                        proposed_events=sorted_events,
                    ),
                    prefix_text=(
                        "📅 *Weekly Review Proposal 1 of "
                        f"{len(sorted_events)}*\n"
                        "Please confirm, reject, or send feedback on this event before I move to the next one."
                    ),
                    source_type="weekly_review",
                    source_id=review_workflow.id,
                )
            else:
                await context.bot.send_message(
                    chat_id=update.effective_chat.id,
                    text="No calendar events were proposed in this weekly review.",
                )
        except Exception as e:
            logger.error(f"Error during Sunday Review: {e}")
            capture_sentry_exception(
                e,
                component="handlers",
                operation="handle_start_trigger_weekly_review",
                message="Failed to start or execute the Sunday review flow from the trigger handler.",
                tags={
                    "review_id": context.user_data.get(ACTIVE_REVIEW_WORKFLOW_ID_KEY, "unknown"),
                },
            )
            context.user_data.pop(ACTIVE_REVIEW_WORKFLOW_ID_KEY, None)
            await context.bot.send_message(chat_id=update.effective_chat.id, text="❌ An error occurred during the Sunday Review.")

@authorized_only
async def handle_delay_trigger(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles delaying a scheduled trigger."""
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("Got it - let's chat first. I'll hold onto this trigger until you're ready.", parse_mode="Markdown")

@authorized_only
async def handle_confirm_weekly_state(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles confirmation to overwrite the weekly state."""
    query = update.callback_query
    await query.answer()
    
    proposed_state = context.user_data.get('proposed_weekly_state')
    if not proposed_state or not isinstance(proposed_state, dict):
        await query.edit_message_text("❌ *No proposed weekly state found.*", parse_mode="Markdown")
        return
        
    review_id = proposed_state.get("review_id") or context.user_data.get(ACTIVE_REVIEW_WORKFLOW_ID_KEY)

    # Lazy Expiration: Check if the proposal is older than 2 hours
    proposal_time = parse_iso(proposed_state["timestamp"])
    if datetime.now(timezone.utc) - proposal_time > timedelta(hours=2):
        del context.user_data['proposed_weekly_state']
        if review_id:
            await apply_bridge_weekly_state_feedback(
                review_id,
                accepted=False,
                proposal_expired=True,
                has_pending_event_feedback=has_pending_weekly_review_event_feedback(context),
            )
        await query.edit_message_text("❌ *This weekly state proposal has expired (older than 2 hours).*", parse_mode="Markdown")
        return

    success = execute_weekly_state_update(proposed_state["content"])
    
    del context.user_data['proposed_weekly_state']
    
    if success:
        if review_id:
            review_workflow = await apply_bridge_weekly_state_feedback(
                review_id,
                accepted=True,
                has_pending_event_feedback=has_pending_weekly_review_event_feedback(context),
            )
            if (
                review_workflow is not None
                and review_workflow.workflow_status == ReviewWorkflowStatus.COMPLETED
            ):
                context.user_data.pop(ACTIVE_REVIEW_WORKFLOW_ID_KEY, None)
        await query.edit_message_text("✅ *Weekly State successfully updated and backed up.*", parse_mode="Markdown")
    else:
        await query.edit_message_text("❌ *Failed to update weekly state. Please check the logs.*", parse_mode="Markdown")

@authorized_only
async def handle_reject_weekly_state(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles rejection of the weekly state update."""
    query = update.callback_query
    await query.answer()

    proposed_state = context.user_data.get('proposed_weekly_state')
    review_id = (
        proposed_state.get("review_id")
        if isinstance(proposed_state, dict)
        else context.user_data.get(ACTIVE_REVIEW_WORKFLOW_ID_KEY)
    )

    if 'proposed_weekly_state' in context.user_data:
        del context.user_data['proposed_weekly_state']

    if review_id:
        await apply_bridge_weekly_state_feedback(
            review_id,
            accepted=False,
            has_pending_event_feedback=has_pending_weekly_review_event_feedback(context),
        )

    await query.edit_message_text(
        "🚫 *Weekly state update rejected. The Sunday review remains open for revision.*",
        parse_mode="Markdown",
    )

@authorized_only
async def done_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Closes the active session and checks for pending triggers."""
    if is_session_active(context):
        cancel_session_timeout(context, update.effective_user.id)
        await end_session(
            context,
            update.effective_chat.id,
            user_id=update.effective_user.id,
        )
    else:
        await update.message.reply_text("There is no active session to close.")

@authorized_only
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles ad-hoc messages by checking UI state and passing text to the Router."""
    # Block new messages if the session is currently synthesising
    if get_session_state(context) == SessionStatus.CLOSING:
        await update.message.reply_text("⏳ *I am currently synthesizing our last session. Please give me a moment...*", parse_mode="Markdown")
        return

    # Check if a text message was sent while a proposal is waiting for confirmation.
    pending_confirmations = get_tracked_confirmation_messages(context)
    if pending_confirmations:
        active_items = [
            (item_id, message_id)
            for item_id, message_id in pending_confirmations
            if item_id.startswith("pi_")
        ]
        if active_items:
            revised = await _revise_active_proposal_item(
                context,
                update.effective_chat.id,
                active_items[0][0],
                active_items[0][1],
                update.message.text,
            )
            if not revised:
                await update.message.reply_text("That proposal is no longer available for revision.")
            return

        for write_id, message_id in pending_confirmations:
            record = get_pending_write(write_id)
            if record and record.status == CalendarWriteStatus.PENDING:
                logger.info(f"New message received. Auto-rejecting interrupted write {write_id}.")
                reject_write(write_id)
                try:
                    await context.bot.edit_message_text(chat_id=update.effective_chat.id, message_id=message_id, text="🚫 *Event cancelled due to new incoming message.*", parse_mode="Markdown")
                except Exception as e:
                    logger.error(f"Failed to update interrupted message UI: {e}")
        clear_tracked_confirmation_messages(context)

    text = update.message.text
    logger.info(f"Received message: {text}")
    
    if not is_session_active(context):
        start_session(context)
    reset_session_timeout(context, update.effective_chat.id, update.effective_user.id)
    
    try:
        response = await process_message(text, context)

        if (
            response.calendar_planning_mode == "propose"
            and response.proposal_thread
            and response.proposal_thread.proposed_events
        ):
            await send_proposal_thread(
                context=context,
                chat_id=update.effective_chat.id,
                proposal_thread=response.proposal_thread,
                prefix_text=response.message,
            )
        else:
            await update.message.reply_text(response.message)
    except ValueError as e:
        logger.error(f"Calendar proposal validation error: {e}")
        await update.message.reply_text(str(e))
    except Exception as e:
        logger.error(f"Error handling message: {e}")
        if _is_calendar_auth_error(e):
            await update.message.reply_text(CALENDAR_AUTH_ERROR_TEXT)
            return

        await update.message.reply_text("Sorry, I encountered an error. Please check the logs.")
