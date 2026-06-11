import asyncio
from functools import wraps
from datetime import datetime, timedelta
from loguru import logger
from telegram import Update
from telegram.ext import ContextTypes

from orchestrator.router import process_message
from orchestrator.confirmation_queue import (
    accept_proposal_item,
    confirm_write,
    get_pending_write,
    get_proposal_item,
    mark_proposal_item_accepted,
    reject_proposal_item,
    reject_write,
)
from orchestrator.trigger_scheduler import queue_trigger, consume_trigger
from orchestrator.session_manager import (
    start_session, end_session, reset_session_timeout, cancel_session_timeout, get_session_state,
    is_session_active, get_tracked_confirmation_messages, 
    untrack_confirmation_message, clear_tracked_confirmation_messages
)
from orchestrator.artifact_writes import retry_artifact_write
from orchestrator.review_manager import (
    load_review_workflow,
    prepare_final_review_stage,
    start_weekly_review_workflow,
    transition_review_stage,
)
from orchestrator.time_utils import USER_TIMEZONE, calendar_event_sort_key
from persistence.models import (
    ArtifactWriteStatus,
    CalendarWriteStatus,
    ProposalItemStatus,
    ReviewStage,
    ReviewWorkflowStatus,
    SessionStatus,
    StageStatus,
)
from reasoning.schemas import ProposedEvent
from bot.keyboards import build_artifact_write_retry_keyboard
from bot.proposal_flow import (
    advance_proposal_thread_after_item_resolution,
    revise_active_proposal_item,
    send_calendar_proposal,
    send_proposal_thread,
)
from bot.review_flow import (
    ACTIVE_ARTIFACT_WRITE_RETRY_KEY,
    ACTIVE_REVIEW_RESUME_PROMPT_KEY,
    ACTIVE_REVIEW_STAGE_CONFIRMATION_KEY,
    ACTIVE_REVIEW_WORKFLOW_ID_KEY,
    advance_review_after_confirmed_stage,
    apply_confirmed_review_stage_artifacts,
    discard_review_workflow,
    revise_active_review_stage,
    resume_review_workflow,
    send_retryable_artifact_write_notice,
    send_review_stage_gate,
)
from observability.sentry import capture_exception as capture_sentry_exception

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
    # David can read calendars and schedule events, so every Telegram entrypoint
    # needs this guard before touching stateful orchestration.
    @wraps(handler)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        if not await _is_authorized(update, context):
            return
        return await handler(update, context, *args, **kwargs)

    return wrapper


def _rollback_failed_router_turn(context: ContextTypes.DEFAULT_TYPE, user_text: str) -> None:
    """
    Removes the latest router turn when handler-side validation rejects it.

    The router writes chat history before this Telegram layer validates calendar
    proposals. If validation fails here, we remove only that failed pair so
    David does not remember an undelivered proposal as real conversation state.
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
        # Only remove the failed turn pair; older context remains available for
        # the next normal message in the same session.
        chat_history.pop()
        chat_history.pop()


# Handlers for Telegram bot commands and messages.
# These are the entry points for all user interactions, and they delegate to the Router and other orchestrator components to handle the logic and state management. 
# The handlers also manage session state and ensure that the user experience is smooth and responsive, even when waiting for LLM responses or handling confirmations.

@authorized_only
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles the /start command."""
    user_id = update.effective_user.id
    logger.info(f"User {user_id} started the bot.")
    await update.message.reply_text("Hello! I am David.")
    await send_retryable_artifact_write_notice(context, update.effective_chat.id)

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


async def _start_weekly_review_interaction(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
):
    """
    Starts the durable Sunday review workflow and shows the first review gate.

    This is shared by both the scheduled trigger and the manual command so the
    review id, persisted workflow state, and first confirmation boundary stay
    aligned regardless of how David's review is launched.
    """
    review_workflow = await start_weekly_review_workflow()
    context.user_data[ACTIVE_REVIEW_WORKFLOW_ID_KEY] = review_workflow.id
    await send_review_stage_gate(
        context,
        chat_id,
        review_workflow,
    )
    return review_workflow


@authorized_only
async def weekly_review_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Manually starts the gated Sunday review workflow for local testing."""
    await update.message.reply_text(
        "📅 *Starting Sunday Review. Analysing your week...*",
        parse_mode="Markdown",
    )
    try:
        await _start_weekly_review_interaction(
            context,
            update.effective_chat.id,
        )
    except Exception as e:
        logger.error(f"Error during manual Sunday Review: {e}")
        capture_sentry_exception(
            e,
            component="handlers",
            operation="weekly_review_command",
            message="Failed to start or execute the Sunday review flow from the manual command.",
            tags={
                "review_id": context.user_data.get(ACTIVE_REVIEW_WORKFLOW_ID_KEY, "unknown"),
            },
        )
        context.user_data.pop(ACTIVE_REVIEW_WORKFLOW_ID_KEY, None)
        await update.message.reply_text("❌ An error occurred during the Sunday Review.")


@authorized_only
async def handle_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles confirmation of a proposed calendar item or legacy calendar write."""
    query = update.callback_query
    await query.answer()

    if query.data.startswith("retry_artifact_write_"):
        await _handle_artifact_write_retry(update, context)
        return

    if query.data.startswith("confirm_review_stage_"):
        await _handle_review_stage_confirm(update, context)
        return

    if query.data.startswith("confirm_item_"):
        await _handle_proposal_item_confirm(update, context)
        return

    write_id = query.data.split("confirm_")[1]
    await _handle_legacy_calendar_write_confirm(update, context, write_id)


@authorized_only
async def handle_review_resume(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles explicit startup recovery actions for interrupted reviews."""
    query = update.callback_query
    await query.answer()

    if query.data.startswith("resume_review_"):
        review_id = query.data.split("resume_review_")[1]
        await query.edit_message_text(
            "Continuing the interrupted Sunday review...",
        )
        await resume_review_workflow(context, query.message.chat_id, review_id)
        return

    review_id = query.data.split("discard_review_")[1]
    await discard_review_workflow(context, review_id)
    await query.edit_message_text(
        "Discarded the interrupted Sunday review. It will not be resumed on the next restart.",
    )


async def _handle_artifact_write_retry(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """
    Retries a previously confirmed artifact write and resumes its review stage.

    These retries are operational recovery for confirmed markdown changes; they
    replay stored content and should not rerun the model or mutate proposals.
    """
    query = update.callback_query
    write_id = query.data.split("retry_artifact_write_")[1]
    executed_write = retry_artifact_write(write_id)
    if executed_write is None:
        await query.edit_message_text("❌ *This retry is no longer available.*", parse_mode="Markdown")
        return

    if executed_write.status != ArtifactWriteStatus.EXECUTED:
        context.user_data[ACTIVE_ARTIFACT_WRITE_RETRY_KEY] = {
            "write_id": executed_write.id,
            "review_id": executed_write.source_id,
            "stage": executed_write.source_stage,
        }
        await query.edit_message_text(
            "❌ *The write still could not be applied. The review remains paused.*",
            reply_markup=build_artifact_write_retry_keyboard(executed_write.id),
            parse_mode="Markdown",
        )
        return

    retry_state = context.user_data.pop(ACTIVE_ARTIFACT_WRITE_RETRY_KEY, {})
    review_id = (
        retry_state.get("review_id")
        if isinstance(retry_state, dict)
        else executed_write.source_id
    ) or executed_write.source_id
    stage_value = (
        retry_state.get("stage")
        if isinstance(retry_state, dict)
        else executed_write.source_stage
    ) or executed_write.source_stage
    record = await load_review_workflow(review_id) if review_id else None
    if record is None or not stage_value:
        await query.edit_message_text(
            "✅ *Write retried successfully, but I could not resume the review automatically.*",
            parse_mode="Markdown",
        )
        return

    stage = ReviewStage(stage_value)
    await query.edit_message_text(
        f"✅ *{stage.value.replace('_', ' ').title()} write retried successfully.*",
        parse_mode="Markdown",
    )
    await advance_review_after_confirmed_stage(context, query.message.chat_id, record, stage)


async def _handle_review_stage_confirm(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """
    Confirms the active Sunday-review stage and advances the gated workflow.

    Artifact-producing stages must execute their confirmed markdown writes
    before downstream stages run, otherwise later prompts could read stale
    decision-log or weekly-state context.
    """
    query = update.callback_query
    stage = ReviewStage(query.data.split("confirm_review_stage_")[1])
    active_confirmation = context.user_data.get(ACTIVE_REVIEW_STAGE_CONFIRMATION_KEY)
    review_id = (
        active_confirmation.get("review_id")
        if isinstance(active_confirmation, dict)
        else context.user_data.get(ACTIVE_REVIEW_WORKFLOW_ID_KEY)
    )
    record = await load_review_workflow(review_id) if review_id else None
    if record is None:
        await query.edit_message_text("❌ *This review stage is no longer available.*", parse_mode="Markdown")
        return

    artifacts_applied = await apply_confirmed_review_stage_artifacts(context, stage, record)
    if not artifacts_applied:
        retry_state = context.user_data.get(ACTIVE_ARTIFACT_WRITE_RETRY_KEY)
        retry_write_id = (
            retry_state.get("write_id")
            if isinstance(retry_state, dict)
            else None
        )
        await query.edit_message_text(
            "❌ *I could not apply the confirmed review changes. The review is still paused here.*",
            reply_markup=(
                build_artifact_write_retry_keyboard(retry_write_id)
                if retry_write_id
                else None
            ),
            parse_mode="Markdown",
        )
        return

    await query.edit_message_text(
        f"✅ *{stage.value.replace('_', ' ').title()} confirmed.*",
        parse_mode="Markdown",
    )
    await advance_review_after_confirmed_stage(context, query.message.chat_id, record, stage)


async def _handle_proposal_item_confirm(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """
    Converts one accepted proposal item into a calendar write and executes it.

    Proposal items become terminal only after the calendar side effect succeeds;
    if this item was the last one in a Sunday-review thread, the review can
    complete after the proposal queue is depleted.
    """
    query = update.callback_query
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
        _update_cached_events_after_calendar_write(context, item, created_event)
    else:
        text = f"{query.message.text}\n\n❌ *Failed to execute calendar action.*"

    await query.edit_message_text(text=text, parse_mode="Markdown")
    if not created_event:
        return

    advanced_thread = await advance_proposal_thread_after_item_resolution(
        context=context,
        chat_id=query.message.chat_id,
        outcome_text="Confirmed",
        item=item,
    )
    if not advanced_thread:
        await _show_final_review_after_proposal_thread_depleted(
            context,
            query.message.chat_id,
        )


def _update_cached_events_after_calendar_write(
    context: ContextTypes.DEFAULT_TYPE,
    record,
    created_event: dict,
) -> None:
    """
    Keeps the session calendar cache aligned after a confirmed calendar write.

    The cache is only per-session, but updating it immediately lets follow-up
    routing see the new calendar state without waiting for another API fetch.
    """
    if "cached_events" not in context.user_data:
        context.user_data["cached_events"] = []

    if record.action_type == "cancel":
        context.user_data["cached_events"] = [
            event
            for event in context.user_data["cached_events"]
            if event.get("id") != record.target_event_id
        ]
    elif record.action_type == "reschedule":
        existing = [
            event for event in context.user_data["cached_events"]
            if event.get("id") != record.target_event_id
        ]
        existing.append(created_event)
        context.user_data["cached_events"] = existing
    else:
        context.user_data["cached_events"].append(created_event)

    context.user_data["cached_events"].sort(key=calendar_event_sort_key)


async def _show_final_review_after_proposal_thread_depleted(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
) -> None:
    """
    Shows the final review after the last weekly-review proposal item resolves.

    Calendar proposals are reviewed item by item. Once that queue is depleted,
    the Sunday review should still pause at the deterministic final-review gate
    instead of completing implicitly.
    """
    review_id = context.user_data.get(ACTIVE_REVIEW_WORKFLOW_ID_KEY)
    if not review_id:
        return

    record = await load_review_workflow(review_id)
    if record is None:
        return

    final_record = await prepare_final_review_stage(record)
    context.user_data[ACTIVE_REVIEW_WORKFLOW_ID_KEY] = final_record.id
    await send_review_stage_gate(context, chat_id, final_record)


async def _handle_legacy_calendar_write_confirm(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    write_id: str,
) -> None:
    """
    Confirms an older pending CalendarWriteRecord that is not proposal-threaded.

    Most new calendar paths use proposal items. This branch remains for any
    still-pending legacy writes and keeps their cache update behavior intact.
    """
    query = update.callback_query
    untrack_confirmation_message(context, write_id)

    record = get_pending_write(write_id)
    if not record or record.status != CalendarWriteStatus.PENDING:
        await query.edit_message_text(text="❌ *This request is no longer valid or has already been processed.*", parse_mode="Markdown")
        return

    created_event = await asyncio.to_thread(confirm_write, write_id)
    if created_event:
        action_label = record.action_type.capitalize()
        text = f"{query.message.text}\n\n✅ *{action_label} confirmed and executed.*"
        _update_cached_events_after_calendar_write(context, record, created_event)
    else:
        text = f"{query.message.text}\n\n❌ *Failed to execute calendar action.*"
        
    await query.edit_message_text(text=text, parse_mode="Markdown")

@authorized_only
async def handle_reject(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles rejection of a proposed calendar item or legacy calendar write."""
    query = update.callback_query
    await query.answer()

    if query.data.startswith("reject_review_stage_"):
        stage = ReviewStage(query.data.split("reject_review_stage_")[1])
        active_confirmation = context.user_data.get(ACTIVE_REVIEW_STAGE_CONFIRMATION_KEY)
        review_id = (
            active_confirmation.get("review_id")
            if isinstance(active_confirmation, dict)
            else context.user_data.get(ACTIVE_REVIEW_WORKFLOW_ID_KEY)
        )
        record = await load_review_workflow(review_id) if review_id else None
        if record is None:
            await query.edit_message_text("❌ *This review stage is no longer available.*", parse_mode="Markdown")
            return
        await transition_review_stage(
            record,
            stage=stage,
            stage_status=StageStatus.IN_REVISION,
        )
        context.user_data[ACTIVE_REVIEW_STAGE_CONFIRMATION_KEY] = {
            "review_id": record.id,
            "stage": stage.value,
        }
        await query.edit_message_text(
            "📝 *Revision requested.* Send the correction you want me to apply to this review stage.",
            parse_mode="Markdown",
        )
        return

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
            advanced_thread = await advance_proposal_thread_after_item_resolution(
                context=context,
                chat_id=query.message.chat_id,
                outcome_text="Rejected",
                item=item,
            )
            if not advanced_thread:
                await _show_final_review_after_proposal_thread_depleted(
                    context,
                    query.message.chat_id,
                )
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
            await _start_weekly_review_interaction(
                context,
                update.effective_chat.id,
            )
            # Only consume the trigger after the review workflow is durable and has
            # actually started. This keeps the trigger retryable if startup fails.
            consume_trigger(context, trigger_type)
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


async def _close_active_review_stage_keyboard(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    active_stage_confirmation: dict,
) -> None:
    """
    Removes the stale review-gate keyboard before applying text feedback.

    A message during a review gate is treated as revision feedback for the
    latest visible proposal. Closing the old inline keyboard prevents a delayed
    button press from confirming or rejecting the pre-revision proposal.
    """
    message_id = active_stage_confirmation.get("message_id")
    if not isinstance(message_id, int):
        return

    try:
        await context.bot.edit_message_reply_markup(
            chat_id=update.effective_chat.id,
            message_id=message_id,
            reply_markup=None,
        )
    except Exception as error:
        logger.error(f"Failed to close stale review-stage keyboard: {error}")
        capture_sentry_exception(
            error,
            component="handlers",
            operation="close_review_stage_keyboard",
            message="Failed to close stale review-stage keyboard before revision.",
            tags={
                "review_id": str(active_stage_confirmation.get("review_id", "unknown")),
                "stage": str(active_stage_confirmation.get("stage", "unknown")),
            },
        )


@authorized_only
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles ad-hoc messages by checking UI state and passing text to the Router."""
    # Block new messages if the session is currently synthesising
    if get_session_state(context) == SessionStatus.CLOSING:
        await update.message.reply_text("⏳ *I am currently synthesizing our last session. Please give me a moment...*", parse_mode="Markdown")
        return

    resume_prompt = context.user_data.get(ACTIVE_REVIEW_RESUME_PROMPT_KEY)
    if isinstance(resume_prompt, dict):
        review_id = resume_prompt.get("review_id")
        if review_id:
            try:
                await discard_review_workflow(context, review_id)
                await update.message.reply_text(
                    "I discarded the interrupted Sunday review and will treat this as a new message."
                )
            except Exception as error:
                logger.error(f"Failed to discard interrupted review before handling message: {error}")
                capture_sentry_exception(
                    error,
                    component="handlers",
                    operation="discard_review_on_message",
                    message="Failed to discard interrupted review before normal message routing.",
                    tags={"review_id": review_id},
                )
                await update.message.reply_text(
                    "I couldn't discard the interrupted Sunday review yet. Please try the resume or discard button first."
                )
                return

    active_stage_confirmation = context.user_data.get(ACTIVE_REVIEW_STAGE_CONFIRMATION_KEY)
    if isinstance(active_stage_confirmation, dict):
        review_id = active_stage_confirmation.get("review_id")
        stage_value = active_stage_confirmation.get("stage")
        if review_id and stage_value:
            await _close_active_review_stage_keyboard(
                update,
                context,
                active_stage_confirmation,
            )
            revised_record = await revise_active_review_stage(
                context,
                review_id,
                ReviewStage(stage_value),
                update.message.text,
            )
            if revised_record:
                await update.message.reply_text("📝 *Revision applied.*", parse_mode="Markdown")
                await send_review_stage_gate(
                    context,
                    update.effective_chat.id,
                    revised_record,
                )
                return

    if await send_retryable_artifact_write_notice(context, update.effective_chat.id):
        return

    # Check if a text message was sent while a proposal is waiting for confirmation.
    pending_confirmations = get_tracked_confirmation_messages(context)
    if pending_confirmations:
        active_items = [
            (item_id, message_id)
            for item_id, message_id in pending_confirmations
            if item_id.startswith("pi_")
        ]
        for item_id, message_id in active_items:
            item = get_proposal_item(item_id)
            if not item or item.status not in {
                ProposalItemStatus.ACTIVE,
                ProposalItemStatus.IN_REVISION,
            }:
                # Stale tracked proposal UIs should not capture unrelated text.
                # Once removed, this message can continue into normal routing.
                untrack_confirmation_message(context, item_id)
                continue

            revised = await revise_active_proposal_item(
                context,
                update.effective_chat.id,
                item_id,
                message_id,
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
        _rollback_failed_router_turn(context, text)
        await update.message.reply_text(str(e))
    except Exception as e:
        logger.error(f"Error handling message: {e}")
        if _is_calendar_auth_error(e):
            await update.message.reply_text(CALENDAR_AUTH_ERROR_TEXT)
            return

        await update.message.reply_text("Sorry, I encountered an error. Please check the logs.")
