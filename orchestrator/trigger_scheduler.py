import datetime
import pytz
from loguru import logger
from telegram.ext import Application, ContextTypes
from bot.keyboards import build_trigger_keyboard

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

    # Using keyboards.py to build the trigger confirmation keyboard for standardisation and easy testing
    reply_markup = build_trigger_keyboard(next_trigger)
    
    await context.bot.send_message(
        chat_id=chat_id,
        text=text,
        reply_markup=reply_markup,
        parse_mode="Markdown"
    )

async def queue_trigger(context: ContextTypes.DEFAULT_TYPE, trigger_type: str, chat_id: int):
    """Adds a trigger to the queue and prompts the user if it's the only one."""
    if 'pending_triggers' not in context.bot_data:
        context.bot_data['pending_triggers'] = [] # Initialize the queue if it doesn't exist
        
    if trigger_type not in context.bot_data['pending_triggers']:
        context.bot_data['pending_triggers'].append(trigger_type)
        logger.info(f"Queued trigger: {trigger_type}")
        
        # If the queue was empty before this, prompt immediately
        if len(context.bot_data['pending_triggers']) == 1:
            await prompt_next_trigger(context, chat_id)
            
def consume_trigger(context: ContextTypes.DEFAULT_TYPE, trigger_type: str):
    """Removes a trigger from the pending queue after it has been executed."""
    queue = context.bot_data.get('pending_triggers', []) # If the key doesn't exist, return an empty list to avoid errors
    if trigger_type in queue:
        queue.remove(trigger_type)
        logger.info(f"Consumed trigger: {trigger_type}")

async def daily_cron(context: ContextTypes.DEFAULT_TYPE):
    # This function is called by the APScheduler daily trigger. It queues the daily check-in and prompts the user.
    await queue_trigger(context, "daily_checkin", context.job.chat_id)

async def weekly_cron(context: ContextTypes.DEFAULT_TYPE):
    await queue_trigger(context, "weekly_review", context.job.chat_id)

def setup_scheduler(app: Application, chat_id: int):
    """Registers the automated cron jobs."""
    logger.info("Setting up APScheduler triggers...")
    jq = app.job_queue
    
    # We schedule the daily check-in and weekly review to run at specific times. The actual user prompt and execution is handled in the respective cron functions.
    jq.run_daily(
        daily_cron,
        time=datetime.time(hour=8, minute=0, tzinfo=TZ),
        chat_id=chat_id,
        name="daily_checkin"
    )
    
    # Weekly review is scheduled for Sunday at 8:05 AM to give a small buffer after the daily check-in
    jq.run_daily(
        weekly_cron,
        time=datetime.time(hour=8, minute=5, tzinfo=TZ),
        days=(6,), # 6 is Sunday
        chat_id=chat_id,
        name="weekly_review"
    )
