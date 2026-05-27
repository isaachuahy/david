import asyncio
import httpcore
import httpx
from loguru import logger
from telegram.error import NetworkError, TimedOut
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    PicklePersistence,
    PersistenceInput,
    filters,
)

from observability.sentry import (
    capture_exception as capture_sentry_exception,
    init_sentry as bootstrap_sentry,
)
from orchestrator.artifact_writes import reconcile_artifact_writes
from orchestrator.review_manager import reconcile_review_workflows
from orchestrator.trigger_scheduler import setup_scheduler
from orchestrator.session_manager import (
    invalidate_restart_volatile_user_data,
    reconcile_orphaned_sessions,
)
from persistence.database import get_telegram_persistence_path, init_db
from config import ConfigError, load_config
from bot.handlers import (
    start, done_command, test_trigger, test_schedule,
    handle_confirm, handle_reject, handle_start_trigger,
    handle_delay_trigger, handle_confirm_weekly_state, handle_reject_weekly_state, 
    handle_message
)

async def _handle_application_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Reports uncaught Telegram application errors to logs and Sentry.

    This is the last-resort boundary for exceptions that escape handlers,
    scheduled jobs, or polling internals, so we keep the reporting concise and
    consistent to make production triage faster.
    """
    if context.error is None:
        return

    if update is None and _is_transient_polling_transport_error(context.error):
        logger.warning(
            "Suppressed transient Telegram polling transport error: type={error_type} message={error_message}",
            error_type=type(context.error).__name__,
            error_message=str(context.error),
        )
        return

    capture_sentry_exception(
        context.error,
        component="telegram",
        operation="application_error",
        message="Unhandled Telegram application error",
        tags={
            "has_update": str(update is not None).lower(),
        },
    )


def _walk_exception_chain(error: BaseException):
    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        yield current
        current = current.__cause__ or current.__context__


def _is_transient_polling_transport_error(error: BaseException) -> bool:
    transient_error_types = (
        NetworkError,
        TimedOut,
        httpx.ReadError,
        httpx.ConnectError,
        httpx.ReadTimeout,
        httpx.ConnectTimeout,
        httpcore.ReadError,
        httpcore.ConnectError,
        httpcore.ReadTimeout,
        httpcore.ConnectTimeout,
        TimeoutError,
        ConnectionResetError,
        BrokenPipeError,
    )
    return any(isinstance(exc, transient_error_types) for exc in _walk_exception_chain(error))

def main() -> int:
    bootstrap_sentry()

    try:
        config = load_config()
        logger.info("Initializing David's Telegram interface...")
        init_db()
        reconcile_orphaned_sessions()
        try:
            asyncio.run(reconcile_review_workflows())
        except Exception as exc:
            capture_sentry_exception(
                exc,
                component="startup",
                operation="reconcile_review_workflows",
                message="Failed to restore persisted Sunday review workflows during startup.",
                tags={"error_kind": "review_reconciliation"},
            )
            raise
        try:
            reconcile_artifact_writes()
        except Exception as exc:
            capture_sentry_exception(
                exc,
                component="startup",
                operation="reconcile_artifact_writes",
                message="Failed to restore retryable artifact writes during startup.",
                tags={"error_kind": "artifact_write_reconciliation"},
            )
            raise
        persistence = PicklePersistence(
            filepath=str(get_telegram_persistence_path()),
            store_data=PersistenceInput(
                bot_data=False,
                chat_data=False,
                user_data=True,
                callback_data=False,
            ),
        )
        app = (
            ApplicationBuilder()
            .token(config.telegram_bot_token)
            .persistence(persistence)
            .post_init(invalidate_restart_volatile_user_data)
            .build()
        )
        app.bot_data["allowed_user_id"] = config.allowed_user_id

        # Restrict the bot to only respond to a specific user for security reasons
        user_filter = filters.User(user_id=config.allowed_user_id)
        app.add_handler(CommandHandler("start", start, filters=user_filter))
        app.add_handler(CommandHandler("done", done_command, filters=user_filter))
        app.add_handler(CommandHandler("test_trigger", test_trigger, filters=user_filter))
        app.add_handler(CommandHandler("test_schedule", test_schedule, filters=user_filter))
        app.add_handler(CallbackQueryHandler(handle_confirm, pattern=r"^confirm_"))
        app.add_handler(CallbackQueryHandler(handle_confirm, pattern=r"^retry_artifact_write_"))
        app.add_handler(CallbackQueryHandler(handle_reject, pattern=r"^reject_"))
        app.add_handler(CallbackQueryHandler(handle_start_trigger, pattern=r"^start_trigger_"))
        app.add_handler(CallbackQueryHandler(handle_delay_trigger, pattern=r"^delay_trigger$"))
        app.add_handler(CallbackQueryHandler(handle_confirm_weekly_state, pattern=r"^confirm_weekly_state$"))
        app.add_handler(CallbackQueryHandler(handle_reject_weekly_state, pattern=r"^reject_weekly_state$"))
        # MessageHandler is a catch-all for any text messsages that aren't commands
        app.add_handler(MessageHandler(user_filter & filters.TEXT & ~filters.COMMAND, handle_message))
        app.add_error_handler(_handle_application_error)

        # Initialize the APScheduler cron jobs
        setup_scheduler(app, config.allowed_user_id)

        logger.info("Bot is now polling for messages...")
        app.run_polling()
        return 0
    except ConfigError as exc:
        capture_sentry_exception(
            exc,
            component="startup",
            operation="load_config",
            message=str(exc),
            tags={"error_kind": "config"},
        )
        return 1
    except Exception as exc:
        capture_sentry_exception(
            exc,
            component="application",
            operation="main",
            message="David failed during startup or runtime execution.",
            tags={"error_kind": "fatal"},
        )
        return 1

if __name__ == "__main__":
    raise SystemExit(main())
