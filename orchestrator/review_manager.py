import os
from datetime import datetime
from loguru import logger
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from orchestrator.context_builder import build_context
from integrations.calendar import get_past_events
from reasoning.pro_client import generate_sunday_review
from orchestrator.confirmation_queue import add_pending_write
from orchestrator.time_utils import parse_iso
from orchestrator.session_manager import track_confirmation_message

async def run_sunday_review(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Executes the complete Sunday Review flow."""
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
            start_dt = parse_iso(event.start_time)
            end_dt = parse_iso(event.end_time)
            
            write_id = add_pending_write(event.summary, start_dt, end_dt, event.description)
            
            keyboard = [
                [InlineKeyboardButton("Confirm", callback_data=f"confirm_{write_id}"), InlineKeyboardButton("Reject", callback_data=f"reject_{write_id}")]
            ]
            
            message = await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text=f"🗓️ *Proposed Event:*\n**{event.summary}**\n_{event.description}_\n\nStart: {start_dt.strftime('%Y-%m-%d %H:%M UTC')}\nEnd: {end_dt.strftime('%Y-%m-%d %H:%M UTC')}",
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode="Markdown"
            )
            
            track_confirmation_message(context, write_id, message.message_id)
    except Exception as e:
        logger.error(f"Error during Sunday Review: {e}")
        await context.bot.send_message(chat_id=update.effective_chat.id, text="❌ An error occurred during the Sunday Review.")

async def execute_weekly_state_update(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Backs up and overwrites the weekly state file."""
    query = update.callback_query
    proposed_state = context.user_data.get('proposed_weekly_state')
    if not proposed_state:
        await query.edit_message_text("❌ *No proposed weekly state found or it has expired.*", parse_mode="Markdown")
        return
        
    # Resolve context directory relative to the current file
    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    context_dir = os.path.join(base_dir, "context")
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
