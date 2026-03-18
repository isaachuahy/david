import datetime
import pytz
from loguru import logger
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, ContextTypes

# Default timezone for scheduled triggers.
# Change this if you want to run on a different local timezone.
TZ = pytz.timezone("America/Toronto")

async def prompt_next_trigger(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    """Reads the queue and prompts the user for the next pending trigger."""
    queue = context.bot_data.get('pending_triggers', [])
    if not queue:
        return
        
    next_trigger = queue[0]
    
    if next_trigger == "daily_checkin":
        text = "🌅 *Good morning!* Are you ready for your Daily Check-in?"
    elif next_trigger == "weekly_review":
        text = "📅 *Sunday Review.* Are you ready to synthesize the week?"
    else:
        return
        
    keyboard = [
        [InlineKeyboardButton("Start", callback_data=f"start_trigger_{next_trigger}")],
        [InlineKeyboardButton("Not Now / Chat First", callback_data="delay_trigger")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await context.bot.send_message(
        chat_id=chat_id,
        text=text,
        reply_markup=reply_markup,
        parse_mode="Markdown"
    )

async def queue_trigger(context: ContextTypes.DEFAULT_TYPE, trigger_type: str, chat_id: int):
    """Adds a trigger to the queue and prompts the user if it's the only one."""
    if 'pending_triggers' not in context.bot_data:
        context.bot_data['pending_triggers'] = []
        
    if trigger_type not in context.bot_data['pending_triggers']:
        context.bot_data['pending_triggers'].append(trigger_type)
        logger.info(f"Queued trigger: {trigger_type}")
        
        # If the queue was empty before this, prompt immediately
        if len(context.bot_data['pending_triggers']) == 1:
            await prompt_next_trigger(context, chat_id)

async def daily_cron(context: ContextTypes.DEFAULT_TYPE):
    await queue_trigger(context, "daily_checkin", context.job.chat_id)

async def weekly_cron(context: ContextTypes.DEFAULT_TYPE):
    await queue_trigger(context, "weekly_review", context.job.chat_id)

def setup_scheduler(app: Application, chat_id: int):
    """Registers the automated cron jobs."""
    logger.info("Setting up APScheduler triggers...")
    jq = app.job_queue
    
    jq.run_daily(
        daily_cron,
        time=datetime.time(hour=8, minute=0, tzinfo=TZ),
        chat_id=chat_id,
        name="daily_checkin"
    )
    
    jq.run_daily(
        weekly_cron,
        time=datetime.time(hour=8, minute=5, tzinfo=TZ),
        days=(6,), # 6 is Sunday
        chat_id=chat_id,
        name="weekly_review"
    )
