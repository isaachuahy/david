from loguru import logger
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, CallbackQueryHandler, filters

from orchestrator.trigger_scheduler import setup_scheduler
from orchestrator.session_manager import reconcile_orphaned_sessions
from persistence.database import init_db
from config import ConfigError, load_config
from bot.handlers import (
    start, done_command, test_trigger, test_schedule,
    handle_confirm, handle_reject, handle_start_trigger,
    handle_delay_trigger, handle_confirm_weekly_state, handle_reject_weekly_state, 
    handle_message
)

def main() -> int:
    try:
        config = load_config()
    except ConfigError as exc:
        logger.error(str(exc))
        return 1

    logger.info("Initializing David's Telegram interface...")
    init_db()
    reconcile_orphaned_sessions()
    app = ApplicationBuilder().token(config.telegram_bot_token).build()
    app.bot_data["allowed_user_id"] = config.allowed_user_id

    # Restrict the bot to only respond to a specific user for security reasons
    user_filter = filters.User(user_id=config.allowed_user_id)
    app.add_handler(CommandHandler("start", start, filters=user_filter))
    app.add_handler(CommandHandler("done", done_command, filters=user_filter))
    app.add_handler(CommandHandler("test_trigger", test_trigger, filters=user_filter))
    app.add_handler(CommandHandler("test_schedule", test_schedule, filters=user_filter))
    app.add_handler(CallbackQueryHandler(handle_confirm, pattern=r"^confirm_"))
    app.add_handler(CallbackQueryHandler(handle_reject, pattern=r"^reject_"))
    app.add_handler(CallbackQueryHandler(handle_start_trigger, pattern=r"^start_trigger_"))
    app.add_handler(CallbackQueryHandler(handle_delay_trigger, pattern=r"^delay_trigger$"))
    app.add_handler(CallbackQueryHandler(handle_confirm_weekly_state, pattern=r"^confirm_weekly_state$"))
    app.add_handler(CallbackQueryHandler(handle_reject_weekly_state, pattern=r"^reject_weekly_state$"))
    # MessageHandler is a catch-all for any text messsages that aren't commands
    app.add_handler(MessageHandler(user_filter & filters.TEXT & ~filters.COMMAND, handle_message))

    # Initialize the APScheduler cron jobs
    setup_scheduler(app, config.allowed_user_id)

    logger.info("Bot is now polling for messages...")
    app.run_polling()
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
