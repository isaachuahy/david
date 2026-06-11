import asyncio

from loguru import logger
from telegram.ext import ContextTypes

from bot.keyboards import build_proposal_item_keyboard
from integrations.calendar import get_upcoming_events, resolve_calendar_reference
from orchestrator.confirmation_queue import (
    activate_next_proposal_item,
    add_proposal_item,
    create_proposal_thread,
    get_proposal_item,
    list_proposal_items,
    mark_proposal_item_in_revision,
    revise_proposal_item,
)
from orchestrator.router import process_message
from orchestrator.session_manager import (
    track_confirmation_message,
    untrack_confirmation_message,
)
from orchestrator.time_utils import format_user_datetime, parse_user_datetime
from persistence.models import ProposalItemRecord, ProposalItemStatus
from reasoning.flash_client import FlashResponse
from reasoning.schemas import ProposalThreadDraft, ProposedEvent


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
    reply_markup = build_proposal_item_keyboard(item.id)
    full_text = _format_proposal_item_confirmation_text(
        item,
        prefix_text=prefix_text,
        calendar_display_name=calendar_display_name,
    )

    message = await context.bot.send_message(
        chat_id=chat_id,
        text=full_text.strip(),
        reply_markup=reply_markup,
        parse_mode="Markdown",
    )
    track_confirmation_message(context, item.id, message.message_id)
    return item.id


def _format_proposal_item_confirmation_text(
    item: ProposalItemRecord,
    *,
    prefix_text: str = "",
    calendar_display_name: str | None = None,
) -> str:
    """
    Formats the user-visible proposal text from durable item state.

    Reusing this for both initial presentation and revision-in-progress updates
    keeps the Telegram thread readable even though proposal messages are not
    copied into David's chat history.
    """
    start_dt = parse_user_datetime(item.start_time)
    end_dt = parse_user_datetime(item.end_time)
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

    return full_text.strip()


async def _normalize_calendar_action(
    context: ContextTypes.DEFAULT_TYPE,
    action: ProposedEvent,
) -> ProposedEvent:
    """
    Resolves and normalizes one proposed calendar action before persistence.

    Initial proposals and revised proposals share this path so durable proposal
    items always store canonical calendar metadata and normalized datetimes.
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

    Proposal threads are the only calendar proposal contract; revision turns use
    the first proposed event as the replacement for the active item.
    """
    if response.proposal_thread and response.proposal_thread.proposed_events:
        return response.proposal_thread.proposed_events[0]
    return None


def _format_calendar_cache_for_revision(context: ContextTypes.DEFAULT_TYPE) -> str:
    """
    Formats cached calendar context for proposal-item revision turns.

    The cache mirrors the Google Calendar snapshot used by normal routing so a
    revision can compare draft changes against the user's real calendar.
    """
    events = context.user_data.get("cached_events", [])
    if not events:
        return "No cached calendar events."

    lines = []
    for event in events:
        start = event.get("start", {}).get("dateTime", event.get("start", {}).get("date", "Unknown start"))
        end = event.get("end", {}).get("dateTime", event.get("end", {}).get("date", "Unknown end"))
        summary = event.get("summary", "Busy / No Title")
        calendar_id = event.get("calendar_id", "primary")
        event_id = event.get("id", "unknown")
        lines.append(
            f"- {summary} | {start} to {end} | calendar_id={calendar_id} | event_id={event_id}"
        )

    return "\n".join(lines)


def _format_proposal_thread_context(item: ProposalItemRecord) -> str:
    """
    Formats durable proposal-thread state for a revision turn.

    Each feedback turn includes the latest stored thread state so the active
    proposal can be revised without losing related queued/resolved items.
    """
    try:
        thread_items = list_proposal_items(item.thread_id)
    except Exception as error:
        logger.error(f"Failed to load proposal thread context for {item.thread_id}: {error}")
        thread_items = [item]

    lines = []
    for thread_item in thread_items:
        validation_note = (
            f"\n  Validation/feedback note: {thread_item.last_feedback}"
            if thread_item.last_feedback
            else ""
        )
        target_line = (
            f"\n  Target event ID: {thread_item.target_event_id}"
            if thread_item.target_event_id
            else ""
        )
        lines.append(
            f"Item {thread_item.sequence_index + 1} ({thread_item.status.value})\n"
            f"  Action: {thread_item.action_type}\n"
            f"  Summary: {thread_item.summary}\n"
            f"  Start: {thread_item.start_time}\n"
            f"  End: {thread_item.end_time}\n"
            f"  Description: {thread_item.description}\n"
            f"  Calendar ID: {thread_item.calendar_id}"
            f"{target_line}"
            f"{validation_note}"
        )

    return "\n\n".join(lines)


def _format_revision_request(
    context: ContextTypes.DEFAULT_TYPE,
    item: ProposalItemRecord,
    feedback: str,
) -> str:
    """
    Builds an explicit revision turn for the router.

    Chat history alone is not durable enough for multi-turn proposal revision,
    so the prompt carries current calendar context and proposal-thread state.
    """
    return (
        "Revise the active calendar proposal using this feedback. "
        "Return a concrete revised proposal only if it is ready for confirmation; "
        "otherwise ask a clarifying question.\n\n"
        "<CURRENT_CALENDAR_CONTEXT>\n"
        f"{_format_calendar_cache_for_revision(context)}\n"
        "</CURRENT_CALENDAR_CONTEXT>\n\n"
        "<PROPOSAL_THREAD_CONTEXT>\n"
        f"{_format_proposal_thread_context(item)}\n"
        "</PROPOSAL_THREAD_CONTEXT>\n\n"
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


def _rollback_latest_chat_turn(context: ContextTypes.DEFAULT_TYPE, user_text: str) -> None:
    """
    Removes the most recent model turn when downstream validation rejects it.

    Router history is updated before Telegram validation. Rolling back the
    rejected turn prevents undelivered proposals from becoming short-term memory.
    """
    chat_history = context.user_data.get("chat_history")
    if not isinstance(chat_history, list) or len(chat_history) < 2:
        return

    latest_user_turn = chat_history[-2]
    latest_assistant_turn = chat_history[-1]
    if (
        latest_user_turn.get("role") == "user"
        and latest_user_turn.get("content") == user_text
        and latest_assistant_turn.get("role") == "assistant"
    ):
        # Remove only this failed user/assistant pair; older session context
        # remains available for the next normal message.
        chat_history.pop()
        chat_history.pop()


def _format_unresolved_proposal_prompt(item: ProposalItemRecord, prefix_text: str = "") -> str:
    """
    Builds the user-facing prompt for a recoverable but not-yet-confirmable item.

    These items stay in the proposal thread as IN_REVISION drafts until the user
    supplies enough detail for safe confirm/reject calendar controls.
    """
    start_text = item.start_time
    end_text = item.end_time
    try:
        start_text = format_user_datetime(parse_user_datetime(item.start_time))
        end_text = format_user_datetime(parse_user_datetime(item.end_time))
    except Exception:
        logger.debug("Using raw proposal times for unresolved item {}.", item.id)

    validation_note = (
        f"\n\nIssue: {item.last_feedback}"
        if item.last_feedback
        else ""
    )
    prompt = (
        f"📝 *Needs clarification:*\n"
        f"*{item.summary}*\n"
        f"_{item.description}_\n\n"
        f"Action: {item.action_type}\n"
        f"Calendar ID: `{item.calendar_id}`\n"
        f"Draft Start: {start_text}\n"
        f"Draft End: {end_text}"
        f"{validation_note}\n\n"
        "Please send the event title and/or start time so I can update this proposal."
    )
    return f"{prefix_text}\n\n{prompt}".strip() if prefix_text else prompt


async def _send_proposal_item_clarification(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    item: ProposalItemRecord,
    *,
    prefix_text: str = "",
) -> str:
    """
    Presents an unresolved proposal item and tracks it for the next text reply.

    Unlike confirmation UI, this has no inline buttons because the item is not
    safe to accept until calendar validation succeeds.
    """
    message = await context.bot.send_message(
        chat_id=chat_id,
        text=_format_unresolved_proposal_prompt(item, prefix_text=prefix_text),
        parse_mode="Markdown",
    )
    track_confirmation_message(context, item.id, message.message_id)
    return item.id


async def revise_active_proposal_item(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    item_id: str,
    message_id: int,
    feedback: str,
) -> bool:
    """
    Revises one active proposal item in place from user feedback.

    The old confirmation message is retired before reasoning starts so stale
    buttons cannot confirm an outdated proposal while revision is underway.
    """
    item = get_proposal_item(item_id)
    if not item or item.status not in {
        ProposalItemStatus.ACTIVE,
        ProposalItemStatus.IN_REVISION,
    }:
        return False

    try:
        existing_text = _format_proposal_item_confirmation_text(item)
        await context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=(
                f"{existing_text}\n\n📝 *Revision in progress...*"
                if existing_text
                else "📝 *Revision in progress...*"
            ),
            parse_mode="Markdown",
        )
    except Exception as error:
        logger.error(f"Failed to retire proposal item UI for {item_id}: {error}")

    mark_proposal_item_in_revision(item_id, feedback=feedback)
    untrack_confirmation_message(context, item_id)

    if "cached_events" not in context.user_data:
        context.user_data["cached_events"] = await asyncio.to_thread(get_upcoming_events)

    revision_request = _format_revision_request(context, item, feedback)
    response = await process_message(revision_request, context)
    revised_action = _extract_revised_calendar_action(response)

    if revised_action is None:
        message = await context.bot.send_message(chat_id=chat_id, text=response.message)
        track_confirmation_message(context, item_id, message.message_id)
        return True

    try:
        normalized_action = await _normalize_calendar_action(context, revised_action)
    except ValueError as error:
        _rollback_latest_chat_turn(context, revision_request)
        updated_item = revise_proposal_item(
            item_id,
            revised_action,
            feedback=feedback,
        )
        unresolved_item = (
            mark_proposal_item_in_revision(item_id, feedback=str(error))
            if updated_item is not None
            else None
        )
        if unresolved_item is not None:
            await _send_proposal_item_clarification(
                context,
                chat_id,
                unresolved_item,
                prefix_text="I still need more detail before I can confirm this calendar change.",
            )
        else:
            await context.bot.send_message(chat_id=chat_id, text=str(error))
        return True

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


async def advance_proposal_thread_after_item_resolution(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    item: ProposalItemRecord,
    *,
    outcome_text: str,
) -> bool:
    """
    Presents the next queued item after one proposal item is resolved.

    The queue advances exactly one step per user decision; the next item still
    needs its own confirm/reject/revision turn.
    """
    next_item = activate_next_proposal_item(item.thread_id)
    if next_item is None:
        return False

    if next_item.status == ProposalItemStatus.IN_REVISION:
        await _send_proposal_item_clarification(
            context,
            chat_id,
            next_item,
            prefix_text=f"{outcome_text}. I need one clarification before the next related proposal.",
        )
        return True

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
    action: ProposedEvent,
    prefix_text: str = "",
    *,
    source_type: str = "conversation",
    source_id: str | None = None,
    thread_title: str | None = None,
) -> str:
    """
    Compatibility wrapper for one-item calendar proposals.

    New call sites should prefer send_proposal_thread. This keeps existing paths
    on the same durable proposal-thread confirmation flow.
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
    proposal_thread: ProposalThreadDraft,
    *,
    prefix_text: str = "",
    source_type: str = "conversation",
    source_id: str | None = None,
) -> str | None:
    """
    Persists a proposed thread and presents its first proposal item.

    `calendar_planning_mode` is a per-turn model decision; this durable thread
    is the longer-lived state that supports later revision or confirmation.
    """
    if not proposal_thread.proposed_events:
        return None

    proposal_actions: list[tuple[ProposedEvent, ProposalItemStatus, str | None]] = []
    first_display_name: str | None = None
    for index, action in enumerate(proposal_thread.proposed_events):
        # Validate every proposed calendar action before creating durable thread
        # state, while preserving failed actions as recoverable drafts.
        try:
            normalized_action = await _normalize_calendar_action(context, action)
        except ValueError as error:
            proposal_actions.append((action, ProposalItemStatus.IN_REVISION, str(error)))
            continue
        if first_display_name is None:
            first_display_name = normalized_action.calendar_display_name
        proposal_actions.append((normalized_action, ProposalItemStatus.QUEUED, None))

    thread = create_proposal_thread(
        source_type=source_type,
        source_id=source_id or context.user_data.get("current_session_id"),
        title=proposal_thread.title,
    )

    for index, (action, status, validation_error) in enumerate(proposal_actions):
        item = add_proposal_item(
            thread.id,
            action,
            sequence_index=index,
            status=status,
        )
        if validation_error:
            mark_proposal_item_in_revision(item.id, feedback=validation_error)

    item = activate_next_proposal_item(thread.id)
    if item is None:
        raise RuntimeError("Failed to activate the first item in the proposal thread.")

    if item.status == ProposalItemStatus.IN_REVISION:
        return await _send_proposal_item_clarification(
            context,
            chat_id,
            item,
            prefix_text=prefix_text,
        )

    return await _send_proposal_item_confirmation(
        context,
        chat_id,
        item,
        prefix_text=prefix_text,
        calendar_display_name=first_display_name,
    )


async def send_weekly_review_scheduling_proposals(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    review_workflow,
) -> bool:
    """
    Presents scheduling-pass proposals after the weekly plan has been accepted.

    Scheduling proposals are stage artifacts until this point. Once the weekly
    plan is confirmed, they become a normal proposal thread with item-by-item
    confirmation and revision behavior.
    """
    scheduling_proposals = review_workflow.scheduling_proposals
    if not scheduling_proposals or not scheduling_proposals.proposed_events:
        return False

    proposed_events = [
        ProposedEvent.model_validate(event)
        for event in scheduling_proposals.proposed_events
    ]
    sorted_events = sorted(
        proposed_events,
        key=lambda event: parse_user_datetime(event.start_time),
    )
    await send_proposal_thread(
        context=context,
        chat_id=chat_id,
        proposal_thread=ProposalThreadDraft(
            title="Weekly review calendar proposals",
            rationale=(
                scheduling_proposals.scheduling_rationale
                or "Calendar proposals generated from the Sunday review."
            ),
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
    return True


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
