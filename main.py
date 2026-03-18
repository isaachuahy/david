import os
from dotenv import load_dotenv
from loguru import logger
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes
from datetime import datetime, timezone, timedelta

from orchestrator.context_builder import build_context
from reasoning.flash_client import generate_flash_response
from reasoning.pro_client import generate_sunday_review
from integrations.calendar import get_past_events
from orchestrator.confirmation_queue import add_pending_write, confirm_write, reject_write, get_pending_write
from orchestrator.trigger_scheduler import setup_scheduler, queue_trigger
from orchestrator.session_manager import start_session, end_session

# Load environment variables from .env
load_dotenv()

SESSION_INACTIVITY_TIMEOUT = timedelta(minutes=30)
SESSION_TIMEOUT_JOB_PREFIX = "session_inactivity_timeout"

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles the /start command."""
    user_id = update.effective_user.id
    logger.info(f"User {user_id} started the bot.")
    # Echo for now, but this is where we can add more complex interactions later
    await update.message.reply_text("Hello! I am David.")

async def timeout_pending_write(context: ContextTypes.DEFAULT_TYPE):
    """Job to automatically reject a pending write if no action is taken."""
    write_id, chat_id, message_id = context.job.data
    
    record = get_pending_write(write_id)
    if record and record.get("status") == "pending":
        logger.info(f"Pending write {write_id} timed out. Auto-rejecting.")
        reject_write(write_id)
        try:
            await context.bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text="⏳ *Request timed out after 30 seconds. Event not scheduled.*",
                parse_mode="Markdown"
            )
        except Exception as e:
            logger.error(f"Failed to update timeout message UI: {e}")

def get_session_timeout_job_name(user_id: int) -> str:
    """Builds a stable job name for a user's inactivity timeout."""
    return f"{SESSION_TIMEOUT_JOB_PREFIX}_{user_id}"

def cancel_session_timeout(context: ContextTypes.DEFAULT_TYPE, user_id: int):
    """Cancels any scheduled inactivity timeout for the user."""
    for job in context.job_queue.get_jobs_by_name(get_session_timeout_job_name(user_id)):
        job.schedule_removal()

def reset_session_timeout(context: ContextTypes.DEFAULT_TYPE, chat_id: int, user_id: int):
    """Restarts the inactivity timeout countdown for the active session."""
    cancel_session_timeout(context, user_id)
    context.job_queue.run_once(
        timeout_inactive_session,
        SESSION_INACTIVITY_TIMEOUT,
        data={"chat_id": chat_id},
        name=get_session_timeout_job_name(user_id),
        chat_id=chat_id,
        user_id=user_id,
    )

async def timeout_inactive_session(context: ContextTypes.DEFAULT_TYPE):
    """Closes the active session after 30 minutes without a new message."""
    chat_id = context.job.data["chat_id"]

    if context.user_data.get('session_state') != 'ACTIVE':
        logger.info(f"Inactivity timeout fired for chat {chat_id}, but no active session remained.")
        return

    logger.info(f"Session timed out after 30 minutes of inactivity for chat {chat_id}.")
    await end_session(context, chat_id)
    await context.bot.send_message(
        chat_id=chat_id,
        text="Session closed after 30 minutes of inactivity. Transcript ready for synthesis."
    )

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
    context.user_data['pending_write'] = (write_id, message.message_id)
    
    # Schedule the 30-second timeout
    context.job_queue.run_once(
        timeout_pending_write, 30, data=(write_id, update.effective_chat.id, message.message_id)
    )

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
        queue = context.bot_data.get('pending_triggers', [])
        if trigger_type in queue:
            queue.remove(trigger_type)
        if trigger_type == "daily_checkin":
            await query.edit_message_text("🌅 *Daily Check-in Started.* What are your top priorities for today?", parse_mode="Markdown")
        elif trigger_type == "weekly_review":
            await query.edit_message_text("📅 *Starting Sunday Review. Analyzing your week...*", parse_mode="Markdown")
            
            try:
                context_block = build_context()
                
                # Fetch and format past events
                past_events_raw = get_past_events(days=7)
                if not past_events_raw:
                    past_events_block = "No events found in the past week."
                else:
                    lines = []
                    for event in past_events_raw:
                        start_time = event['start'].get('dateTime', event['start'].get('date'))
                        summary = event.get('summary', 'Busy / No Title')
                        lines.append(f"- [{start_time}] {summary}")
                    past_events_block = "\n".join(lines)
                
                review = generate_sunday_review(context_block, past_events_block)
                
                # Send the synthesis message
                await context.bot.send_message(
                    chat_id=update.effective_chat.id, 
                    text=f"**Sunday Review Complete**\n\n{review.message}", 
                    parse_mode="Markdown"
                )
                
                # Ask for confirmation before overwriting the weekly state
                context.user_data['proposed_weekly_state'] = review.weekly_state_content
                state_keyboard = [[InlineKeyboardButton("Confirm Weekly State Update", callback_data="confirm_weekly_state")]]
                await context.bot.send_message(
                    chat_id=update.effective_chat.id,
                    text=f"📝 *Proposed Weekly State Changes:*\n{review.state_change_summary}\n\nDo you want to apply these changes?",
                    reply_markup=InlineKeyboardMarkup(state_keyboard),
                    parse_mode="Markdown"
                )
                
                # Propose calendar events individually
                for event in review.proposed_events:
                    # Clean the 'Z' for fromisoformat compatibility
                    start_str = event.start_time.replace('Z', '+00:00')
                    end_str = event.end_time.replace('Z', '+00:00')
                    start_dt = datetime.fromisoformat(start_str)
                    end_dt = datetime.fromisoformat(end_str)
                    
                    write_id = add_pending_write(event.summary, start_dt, end_dt, event.description)
                    
                    keyboard = [
                        [InlineKeyboardButton("Confirm", callback_data=f"confirm_{write_id}"), InlineKeyboardButton("Reject", callback_data=f"reject_{write_id}")]
                    ]
                    
                    await context.bot.send_message(
                        chat_id=update.effective_chat.id,
                        text=f"🗓️ *Proposed Event:*\n**{event.summary}**\n_{event.description}_\n\nStart: {start_dt.strftime('%Y-%m-%d %H:%M UTC')}\nEnd: {end_dt.strftime('%Y-%m-%d %H:%M UTC')}",
                        reply_markup=InlineKeyboardMarkup(keyboard),
                        parse_mode="Markdown"
                    )
            except Exception as e:
                logger.error(f"Error during Sunday Review: {e}")
                await context.bot.send_message(chat_id=update.effective_chat.id, text="❌ An error occurred during the Sunday Review.")
        return
    elif data == "delay_trigger":
        await query.edit_message_text("Got it. We will chat first. I'll hold onto this trigger until you're ready.", parse_mode="Markdown")
        return
    elif data == "confirm_weekly_state":
        proposed_state = context.user_data.get('proposed_weekly_state')
        if not proposed_state:
            await query.edit_message_text("❌ *No proposed weekly state found or it has expired.*", parse_mode="Markdown")
            return
            
        context_dir = os.path.join(os.path.dirname(__file__), "context")
        weekly_state_path = os.path.join(context_dir, "weekly_state.md")
        
        if os.path.exists(weekly_state_path):
            backup_filename = f"weekly_state_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.md"
            backup_path = os.path.join(context_dir, backup_filename)
            with open(weekly_state_path, "r", encoding="utf-8") as src, open(backup_path, "w", encoding="utf-8") as dst:
                dst.write(src.read())
                
        with open(weekly_state_path, "w", encoding="utf-8") as f:
            f.write(proposed_state)
            
        del context.user_data['proposed_weekly_state']
        await query.edit_message_text("✅ *Weekly State successfully updated and backed up.*", parse_mode="Markdown")
        return
    else:
        return
        
    # Clear from state so it doesn't trigger the text interruption logic later
    if 'pending_write' in context.user_data and context.user_data['pending_write'][0] == write_id:
        del context.user_data['pending_write']
        
    # Ensure it hasn't timed out or been interrupted already
    record = get_pending_write(write_id)
    if not record or record.get("status") != "pending":
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
    if context.user_data.get('session_state') == 'ACTIVE':
        cancel_session_timeout(context, update.effective_user.id)
        await end_session(context, update.effective_chat.id)
        await update.message.reply_text("Session closed. Transcript ready for synthesis.")
    else:
        await update.message.reply_text("There is no active session to close.")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles ad-hoc messages by passing them through the ContextBuilder and Flash model."""
    # Check if they sent a text message while a write is waiting for confirmation
    if 'pending_write' in context.user_data:
        write_id, message_id = context.user_data.pop('pending_write')
        record = get_pending_write(write_id)
        if record and record.get("status") == "pending":
            logger.info(f"New message received. Auto-rejecting interrupted write {write_id}.")
            reject_write(write_id)
            try:
                await context.bot.edit_message_text(
                    chat_id=update.effective_chat.id, message_id=message_id,
                    text="🚫 *Event cancelled due to new incoming message.*", parse_mode="Markdown"
                )
            except Exception as e:
                logger.error(f"Failed to update interrupted message UI: {e}")

    text = update.message.text
    logger.info(f"Received message: {text}")
    
    # Start a session if one isn't active
    if context.user_data.get('session_state') != 'ACTIVE':
        start_session(context)
    reset_session_timeout(context, update.effective_chat.id, update.effective_user.id)
    
    try:
        context_block = build_context()
        chat_history = context.user_data.get('chat_history', [])
        
        flash_response = generate_flash_response(user_message=text, context_block=context_block, chat_history=chat_history)
        
        logger.info(f"Flash Escalate Signal: {flash_response.should_escalate}")
        if flash_response.should_escalate:
            logger.info(f"Escalation Reason: {flash_response.escalation_reason}")
            
        await update.message.reply_text(flash_response.message)
        
        # Update conversation history
        chat_history.append({"role": "user", "content": text})
        chat_history.append({"role": "assistant", "content": flash_response.message})
        context.user_data['chat_history'] = chat_history
        
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
