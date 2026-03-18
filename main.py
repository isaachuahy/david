import os
from dotenv import load_dotenv
from loguru import logger
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes
from datetime import datetime, timezone, timedelta

from orchestrator.context_builder import build_context
from reasoning.flash_client import generate_flash_response
from orchestrator.confirmation_queue import add_pending_write, confirm_write, reject_write, get_pending_write
from orchestrator.trigger_scheduler import setup_scheduler, queue_trigger, consume_trigger
from orchestrator.session_manager import (
    start_session, end_session, reset_session_timeout, cancel_session_timeout, get_session_state,
    is_session_active, get_chat_history, append_chat_history,
    add_pending_write_ui_state, get_pending_write_ui_states, remove_pending_write_ui_state, clear_pending_write_ui_states
)
from orchestrator.review_manager import run_sunday_review, execute_weekly_state_update
from persistence.models import CalendarWriteStatus, SessionStatus

# Load environment variables from .env
load_dotenv()

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles the /start command."""
    user_id = update.effective_user.id
    logger.info(f"User {user_id} started the bot.")
    # Echo for now, but this is where we can add more complex interactions later
    await update.message.reply_text("Hello! I am David.")

async def test_trigger(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Temporary command to test the trigger queue."""
    # Default to daily_checkin if no argument is provided
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
    
    keyboard = [
        [
            InlineKeyboardButton("Confirm", callback_data=f"confirm_{write_id}"),
            InlineKeyboardButton("Reject", callback_data=f"reject_{write_id}")
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    message = await update.message.reply_text(
        "I propose scheduling 'David UI Test Event' for the next 15 minutes. Does this look good?",
        reply_markup=reply_markup
    )
    
    # Store the pending write in user data so we can cancel it if they type a text message
    add_pending_write_ui_state(context, write_id, message.message_id)

async def handle_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles inline button presses for the confirmation queue."""
    query = update.callback_query
    await query.answer() # Acknowledge the button press to Telegram
    
    data = query.data
    if data.startswith("confirm_"):
        write_id = data.split("confirm_")[1]
    elif data.startswith("reject_"):
        write_id = data.split("reject_")[1]
    elif data.startswith("start_trigger_"):
        trigger_type = data.split("start_trigger_")[1]
        consume_trigger(context, trigger_type)
        if trigger_type == "daily_checkin":
            await query.edit_message_text("🌅 *Daily Check-in Started.* What are your top priorities for today?", parse_mode="Markdown")
        elif trigger_type == "weekly_review":
            await query.edit_message_text("📅 *Starting Sunday Review. Analyzing your week...*", parse_mode="Markdown")
            await run_sunday_review(update, context)
        return
    elif data == "delay_trigger":
        await query.edit_message_text("Got it. We will chat first. I'll hold onto this trigger until you're ready.", parse_mode="Markdown")
        return
    elif data == "confirm_weekly_state":
        await execute_weekly_state_update(update, context)
        return
    else:
        return
        
    # Clear from state so it doesn't trigger the text interruption logic later
    remove_pending_write_ui_state(context, write_id)
        
    # Ensure it hasn't timed out or been interrupted already
    record = get_pending_write(write_id)
    if not record or record.status != CalendarWriteStatus.PENDING:
        await query.edit_message_text(text="❌ *This request is no longer valid or has already been processed.*", parse_mode="Markdown")
        return

    if data.startswith("confirm_"):
        success = confirm_write(write_id)
        text = f"{query.message.text}\n\n✅ *Event Confirmed and Scheduled.*" if success else f"{query.message.text}\n\n❌ *Failed to schedule event.*"
        await query.edit_message_text(text=text, parse_mode="Markdown")
    elif data.startswith("reject_"):
        success = reject_write(write_id)
        text = f"{query.message.text}\n\n🚫 *Event Rejected.*" if success else f"{query.message.text}\n\n❌ *Failed to reject event.*"
        await query.edit_message_text(text=text, parse_mode="Markdown")

async def done_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Closes the active session and checks for pending triggers."""
    if is_session_active(context):
        cancel_session_timeout(context, update.effective_user.id)
        await end_session(context, update.effective_chat.id)
    else:
        await update.message.reply_text("There is no active session to close.")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles ad-hoc messages by passing them through the ContextBuilder and Flash model."""
    # Block new messages if the session is currently synthesizing
    if get_session_state(context) == SessionStatus.CLOSING:
        await update.message.reply_text("⏳ *I am currently synthesizing our last session. Please give me a moment...*", parse_mode="Markdown")
        return
        
    # Check if they sent a text message while a write is waiting for confirmation
    pending_writes = get_pending_write_ui_states(context)
    if pending_writes:
        for write_id, message_id in pending_writes:
            record = get_pending_write(write_id)
            if record and record.status == CalendarWriteStatus.PENDING:
                logger.info(f"New message received. Auto-rejecting interrupted write {write_id}.")
                reject_write(write_id)
                try:
                    await context.bot.edit_message_text(
                        chat_id=update.effective_chat.id, message_id=message_id,
                        text="🚫 *Event cancelled due to new incoming message.*", parse_mode="Markdown"
                    )
                except Exception as e:
                    logger.error(f"Failed to update interrupted message UI: {e}")
        clear_pending_write_ui_states(context)

    text = update.message.text
    logger.info(f"Received message: {text}")
    
    # Start a session if one isn't active
    if not is_session_active(context):
        start_session(context)
    reset_session_timeout(context, update.effective_chat.id, update.effective_user.id)
    
    try:
        context_block = build_context()
        chat_history = get_chat_history(context)
        
        flash_response = generate_flash_response(user_message=text, context_block=context_block, chat_history=chat_history)
        
        logger.info(f"Flash Escalate Signal: {flash_response.should_escalate}")
        if flash_response.should_escalate:
            logger.info(f"Escalation Reason: {flash_response.escalation_reason}")
            
        await update.message.reply_text(flash_response.message)
        
        # Update conversation history
        append_chat_history(context, "user", text)
        append_chat_history(context, "assistant", flash_response.message)
        
    except Exception as e:
        logger.error(f"Error during reasoning loop: {e}")
        await update.message.reply_text("Sorry, I encountered an error while thinking. Please check the logs.")

def main():
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    authorized_user_id = os.getenv("AUTHORIZED_USER_ID")
    
    if not token or token == "your_telegram_bot_token_here" or not authorized_user_id:
        logger.error("Please set valid TELEGRAM_BOT_TOKEN and AUTHORIZED_USER_ID in your .env file.")
        return
        
    try:
        auth_user_id = int(authorized_user_id)
    except ValueError:
        logger.error("AUTHORIZED_USER_ID must be an integer.")
        return

    logger.info("Initializing David's Telegram interface...")
    app = ApplicationBuilder().token(token).build()

    # Restrict the bot to only respond to a specific user for security reasons
    user_filter = filters.User(user_id=auth_user_id)
    app.add_handler(CommandHandler("start", start, filters=user_filter))
    app.add_handler(CommandHandler("done", done_command, filters=user_filter))
    app.add_handler(CommandHandler("test_trigger", test_trigger, filters=user_filter))
    app.add_handler(CommandHandler("test_schedule", test_schedule, filters=user_filter))
    app.add_handler(CallbackQueryHandler(handle_button))
    # MessageHandler is a catch-all for any text messsages that aren't commands
    app.add_handler(MessageHandler(user_filter & filters.TEXT & ~filters.COMMAND, handle_message))

    # Initialize the APScheduler cron jobs
    setup_scheduler(app, auth_user_id)

    logger.info("Bot is now polling for messages...")
    app.run_polling()

if __name__ == "__main__":
    main()
