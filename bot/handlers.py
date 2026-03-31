import asyncio
from datetime import datetime, timezone, timedelta
from loguru import logger
from telegram import Update
from telegram.ext import ContextTypes

from orchestrator.router import process_message
from orchestrator.confirmation_queue import add_pending_write, confirm_write, reject_write, get_pending_write
from orchestrator.trigger_scheduler import queue_trigger, consume_trigger
from orchestrator.session_manager import (
    start_session, end_session, reset_session_timeout, cancel_session_timeout, get_session_state,
    is_session_active, track_confirmation_message, get_tracked_confirmation_messages, 
    untrack_confirmation_message, clear_tracked_confirmation_messages
)
from orchestrator.review_manager import run_sunday_review, execute_weekly_state_update
from orchestrator.time_utils import USER_TIMEZONE, calendar_event_sort_key, parse_iso, parse_user_datetime, format_user_datetime
from persistence.models import CalendarWriteStatus, SessionStatus
from reasoning.schemas import ProposedEvent
from bot.keyboards import build_calendar_confirmation_keyboard, build_weekly_state_keyboard

WEEKLY_REVIEW_EVENT_QUEUE_KEY = "weekly_review_event_queue"
WEEKLY_REVIEW_TOTAL_EVENTS_KEY = "weekly_review_total_events"
WEEKLY_REVIEW_PROCESSED_EVENTS_KEY = "weekly_review_processed_events"
WEEKLY_REVIEW_CURRENT_WRITE_ID_KEY = "weekly_review_current_write_id"


def clear_weekly_review_event_queue(context: ContextTypes.DEFAULT_TYPE):
    """Clears the in-memory weekly review event queue state."""
    context.user_data.pop(WEEKLY_REVIEW_EVENT_QUEUE_KEY, None)
    context.user_data.pop(WEEKLY_REVIEW_TOTAL_EVENTS_KEY, None)
    context.user_data.pop(WEEKLY_REVIEW_PROCESSED_EVENTS_KEY, None)
    context.user_data.pop(WEEKLY_REVIEW_CURRENT_WRITE_ID_KEY, None)


async def send_calendar_proposal(context: ContextTypes.DEFAULT_TYPE, chat_id: int, action, prefix_text: str = "") -> str:
    """Helper to process a calendar action, queue it, and send the Telegram confirmation UI."""
    # Used both for ad-hoc calendar proposals from the LLM and for proposed events generated during the Sunday Review process.
    start_dt = parse_user_datetime(action.start_time)
    end_dt = parse_user_datetime(action.end_time)
    
    write_id = add_pending_write(action.summary, start_dt, end_dt, action.description)
    reply_markup = build_calendar_confirmation_keyboard(write_id)
    
    full_text = f"{prefix_text}\n\n" if prefix_text else ""
    full_text += f"🗓️ *Proposed Event:*\n*{action.summary}*\n_{action.description}_\n\nStart: {format_user_datetime(start_dt)}\nEnd: {format_user_datetime(end_dt)}"
    
    message = await context.bot.send_message(
        chat_id=chat_id,
        text=full_text.strip(),
        reply_markup=reply_markup,
        parse_mode="Markdown"
    )
    track_confirmation_message(context, write_id, message.message_id)
    return write_id


async def send_next_weekly_review_event(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    """Sends the next queued weekly review proposal, if one remains."""
    queue = context.user_data.get(WEEKLY_REVIEW_EVENT_QUEUE_KEY)
    if not queue:
        clear_weekly_review_event_queue(context)
        await context.bot.send_message(
            chat_id=chat_id,
            text="Weekly review calendar proposals are complete.",
        )
        return

    total = context.user_data.get(WEEKLY_REVIEW_TOTAL_EVENTS_KEY, len(queue))
    processed = context.user_data.get(WEEKLY_REVIEW_PROCESSED_EVENTS_KEY, 0)
    next_position = processed + 1
    next_event = queue.pop(0)
    write_id = await send_calendar_proposal(
        context=context,
        chat_id=chat_id,
        action=next_event,
        prefix_text=(
            f"📅 *Weekly Review Proposal {next_position} of {total}*\n"
            "Please confirm or reject this event before I move to the next one."
        ),
    )
    context.user_data[WEEKLY_REVIEW_CURRENT_WRITE_ID_KEY] = write_id


async def advance_weekly_review_event_queue(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    write_id: str,
    outcome_text: str,
):
    """Advances the queued weekly review proposals after the current one is resolved."""
    current_write_id = context.user_data.get(WEEKLY_REVIEW_CURRENT_WRITE_ID_KEY)
    if current_write_id != write_id:
        return

    total = context.user_data.get(WEEKLY_REVIEW_TOTAL_EVENTS_KEY, 0)
    processed = context.user_data.get(WEEKLY_REVIEW_PROCESSED_EVENTS_KEY, 0) + 1
    context.user_data[WEEKLY_REVIEW_PROCESSED_EVENTS_KEY] = processed
    remaining_queue = context.user_data.get(WEEKLY_REVIEW_EVENT_QUEUE_KEY, [])

    if remaining_queue:
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"{outcome_text} weekly review proposal {processed} of {total}. Sending the next proposal now.",
        )
        await send_next_weekly_review_event(context, chat_id)
        return

    clear_weekly_review_event_queue(context)
    await context.bot.send_message(
        chat_id=chat_id,
        text=f"{outcome_text} weekly review proposal {processed} of {total}. Weekly review calendar proposals are complete.",
    )

# Handlers for Telegram bot commands and messages.
# These are the entry points for all user interactions, and they delegate to the Router and other orchestrator components to handle the logic and state management. 
# The handlers also manage session state and ensure that the user experience is smooth and responsive, even when waiting for LLM responses or handling confirmations.

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles the /start command."""
    user_id = update.effective_user.id
    logger.info(f"User {user_id} started the bot.")
    await update.message.reply_text("Hello! I am David.")

async def test_trigger(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Temporary command to test the trigger queue."""
    trigger_type = context.args[0] if context.args else "daily_checkin"
    await queue_trigger(context, trigger_type, update.effective_chat.id)

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

async def handle_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles confirmation of a proposed calendar write."""
    query = update.callback_query
    await query.answer()
    
    write_id = query.data.split("confirm_")[1]
    untrack_confirmation_message(context, write_id)
        
    record = get_pending_write(write_id)
    if not record or record.status != CalendarWriteStatus.PENDING:
        await query.edit_message_text(text="❌ *This request is no longer valid or has already been processed.*", parse_mode="Markdown")
        return

    created_event = await asyncio.to_thread(confirm_write, write_id)
    if created_event:
        text = f"{query.message.text}\n\n✅ *Event Confirmed and Scheduled.*"
        # Immediately update the local cache so the LLM knows about this new event
        # Note: This cache is only for the current session and will not persist across sessions.
        # This is a workaround to ensure that if the user schedules an event and then immediately asks David about their schedule, 
        # the new event will be included in the context without needing to wait for the next API fetch cycle.

        # Initialize the in-memory calendar cache if it hasn't been populated yet,
        # then append the newly created event so subsequent context builds see it
        # without waiting for another Calendar API fetch.

        if 'cached_events' not in context.user_data:
            context.user_data['cached_events'] = []
        context.user_data['cached_events'].append(created_event)
        context.user_data['cached_events'].sort(key=calendar_event_sort_key)
    else:
        text = f"{query.message.text}\n\n❌ *Failed to schedule event.*"
        
    await query.edit_message_text(text=text, parse_mode="Markdown")
    if created_event:
        await advance_weekly_review_event_queue(
            context=context,
            chat_id=query.message.chat_id,
            write_id=write_id,
            outcome_text="Confirmed",
        )

async def handle_reject(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles rejection of a proposed calendar write."""
    query = update.callback_query
    await query.answer()
    
    write_id = query.data.split("reject_")[1]
    untrack_confirmation_message(context, write_id)
        
    record = get_pending_write(write_id)
    if not record or record.status != CalendarWriteStatus.PENDING:
        await query.edit_message_text(text="❌ *This request is no longer valid or has already been processed.*", parse_mode="Markdown")
        return

    success = reject_write(write_id)
    text = f"{query.message.text}\n\n🚫 *Event Rejected.*" if success else f"{query.message.text}\n\n❌ *Failed to reject event.*"
    await query.edit_message_text(text=text, parse_mode="Markdown")
    if success:
        await advance_weekly_review_event_queue(
            context=context,
            chat_id=query.message.chat_id,
            write_id=write_id,
            outcome_text="Rejected",
        )

async def handle_start_trigger(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles starting a scheduled trigger."""
    query = update.callback_query
    await query.answer()
    
    trigger_type = query.data.split("start_trigger_")[1]
    consume_trigger(context, trigger_type)
    
    if trigger_type == "daily_checkin":
        await query.edit_message_text("🌅 *Daily Check-in Started.* What are your top priorities for today?", parse_mode="Markdown")
    elif trigger_type == "weekly_review":
        await query.edit_message_text("📅 *Starting Sunday Review. Analysing your week...*", parse_mode="Markdown")
        try:
            review = await asyncio.to_thread(run_sunday_review, context)
            
            # Send the synthesis message
            await context.bot.send_message(
                chat_id=update.effective_chat.id, 
                text=f"**Sunday Review Complete**\n\n{review.message}", 
                parse_mode="Markdown"
            )
            
            # Ask for confirmation before overwriting the weekly state
            context.user_data['proposed_weekly_state'] = {
                "content": review.weekly_state_content,
                "timestamp": datetime.now(timezone.utc).isoformat()
            }
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text=f"📝 *Proposed Weekly State Changes:*\n{review.state_change_summary}\n\nDo you want to apply these changes?",
                reply_markup=build_weekly_state_keyboard(),
                parse_mode="Markdown"
            )
            
            clear_weekly_review_event_queue(context)
            if review.proposed_events:
                sorted_events = sorted(
                    review.proposed_events,
                    key=lambda event: parse_user_datetime(event.start_time),
                )
                context.user_data[WEEKLY_REVIEW_EVENT_QUEUE_KEY] = sorted_events
                context.user_data[WEEKLY_REVIEW_TOTAL_EVENTS_KEY] = len(review.proposed_events)
                context.user_data[WEEKLY_REVIEW_PROCESSED_EVENTS_KEY] = 0
                await send_next_weekly_review_event(context, update.effective_chat.id)
            else:
                await context.bot.send_message(
                    chat_id=update.effective_chat.id,
                    text="No calendar events were proposed in this weekly review.",
                )
        except Exception as e:
            logger.error(f"Error during Sunday Review: {e}")
            await context.bot.send_message(chat_id=update.effective_chat.id, text="❌ An error occurred during the Sunday Review.")

async def handle_delay_trigger(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles delaying a scheduled trigger."""
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("Got it - let's chat first. I'll hold onto this trigger until you're ready.", parse_mode="Markdown")

async def handle_confirm_weekly_state(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles confirmation to overwrite the weekly state."""
    query = update.callback_query
    await query.answer()
    
    proposed_state = context.user_data.get('proposed_weekly_state')
    if not proposed_state or not isinstance(proposed_state, dict):
        await query.edit_message_text("❌ *No proposed weekly state found.*", parse_mode="Markdown")
        return
        
    # Lazy Expiration: Check if the proposal is older than 2 hours
    proposal_time = parse_iso(proposed_state["timestamp"])
    if datetime.now(timezone.utc) - proposal_time > timedelta(hours=2):
        del context.user_data['proposed_weekly_state']
        await query.edit_message_text("❌ *This weekly state proposal has expired (older than 2 hours).*", parse_mode="Markdown")
        return

    success = execute_weekly_state_update(proposed_state["content"])
    
    del context.user_data['proposed_weekly_state']
    
    if success:
        await query.edit_message_text("✅ *Weekly State successfully updated and backed up.*", parse_mode="Markdown")
    else:
        await query.edit_message_text("❌ *Failed to update weekly state. Please check the logs.*", parse_mode="Markdown")

async def handle_reject_weekly_state(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles rejection of the weekly state update."""
    query = update.callback_query
    await query.answer()
    
    if 'proposed_weekly_state' in context.user_data:
        del context.user_data['proposed_weekly_state']
        
    await query.edit_message_text("🚫 *Weekly state update rejected.*", parse_mode="Markdown")

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

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles ad-hoc messages by checking UI state and passing text to the Router."""
    # Block new messages if the session is currently synthesising
    if get_session_state(context) == SessionStatus.CLOSING:
        await update.message.reply_text("⏳ *I am currently synthesizing our last session. Please give me a moment...*", parse_mode="Markdown")
        return

    # Check if a text message was sent while a write is waiting for confirmation    
    pending_writes = get_tracked_confirmation_messages(context)
    if pending_writes:
        for write_id, message_id in pending_writes:
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
        
        if response.proposed_calendar_action:
            await send_calendar_proposal(
                context=context,
                chat_id=update.effective_chat.id,
                action=response.proposed_calendar_action,
                prefix_text=response.message
            )
        else:
            await update.message.reply_text(response.message)
    except Exception as e:
        logger.error(f"Error handling message: {e}")
        await update.message.reply_text("Sorry, I encountered an error. Please check the logs.")
