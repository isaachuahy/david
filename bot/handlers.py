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
from persistence.models import CalendarWriteStatus, SessionStatus
from bot.keyboards import build_confirmation_keyboard

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
    now = datetime.now(timezone.utc)
    end = now + timedelta(minutes=15)
    write_id = add_pending_write(
        summary="David UI Test Event",
        start_time=now,
        end_time=end,
        description="Testing the Telegram inline buttons."
    )
    
    reply_markup = build_confirmation_keyboard(write_id)
    
    message = await update.message.reply_text(
        "I propose scheduling 'David UI Test Event' for the next 15 minutes. Does this look good?",
        reply_markup=reply_markup
    )
    
    # Store the pending write in user data so we can cancel it if they type a text message
    track_confirmation_message(context, write_id, message.message_id)

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

    success = confirm_write(write_id)
    text = f"{query.message.text}\n\n✅ *Event Confirmed and Scheduled.*" if success else f"{query.message.text}\n\n❌ *Failed to schedule event.*"
    await query.edit_message_text(text=text, parse_mode="Markdown")

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

async def handle_start_trigger(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles starting a scheduled trigger."""
    query = update.callback_query
    await query.answer()
    
    trigger_type = query.data.split("start_trigger_")[1]
    consume_trigger(context, trigger_type)
    
    if trigger_type == "daily_checkin":
        await query.edit_message_text("🌅 *Daily Check-in Started.* What are your top priorities for today?", parse_mode="Markdown")
    elif trigger_type == "weekly_review":
        await query.edit_message_text("📅 *Starting Sunday Review. Analyzing your week...*", parse_mode="Markdown")
        await run_sunday_review(update, context)

async def handle_delay_trigger(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles delaying a scheduled trigger."""
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("Got it. We will chat first. I'll hold onto this trigger until you're ready.", parse_mode="Markdown")

async def handle_confirm_weekly_state(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles confirmation to overwrite the weekly state."""
    query = update.callback_query
    await query.answer()
    await execute_weekly_state_update(update, context)

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
        await end_session(context, update.effective_chat.id)
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
        await update.message.reply_text(response.message)
    except Exception as e:
        logger.error(f"Error handling message: {e}")
        await update.message.reply_text("Sorry, I encountered an error. Please check the logs.")
