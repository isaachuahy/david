import asyncio
from pathlib import Path
from unittest.mock import MagicMock, patch

import main


@patch("observability.sentry.logger")
@patch("observability.sentry.sentry_sdk.set_tag")
@patch("observability.sentry.sentry_sdk.init")
def test_init_sentry_does_nothing_without_dsn(
    mock_sentry_init,
    mock_set_tag,
    mock_logger,
    monkeypatch,
):
    monkeypatch.delenv("SENTRY_DSN", raising=False)

    main.bootstrap_sentry()

    mock_sentry_init.assert_not_called()
    mock_set_tag.assert_not_called()
    mock_logger.info.assert_called_once_with(
        "Sentry is disabled because SENTRY_DSN is not configured."
    )


@patch("observability.sentry.logger")
@patch("observability.sentry.sentry_sdk.set_tag")
@patch("observability.sentry.sentry_sdk.init")
def test_init_sentry_initializes_sdk_when_dsn_is_present(
    mock_sentry_init,
    mock_set_tag,
    mock_logger,
    monkeypatch,
):
    monkeypatch.setenv("SENTRY_DSN", "https://example@sentry.io/123")
    monkeypatch.setenv("DAVID_ENVIRONMENT", "staging")
    monkeypatch.setenv("DAVID_RELEASE", "test-release")

    main.bootstrap_sentry()

    mock_sentry_init.assert_called_once_with(
        dsn="https://example@sentry.io/123",
        environment="staging",
        release="test-release",
        send_default_pii=False,
        enable_tracing=False,
    )
    mock_set_tag.assert_called_once_with("service", "david")
    mock_logger.info.assert_called_once_with(
        "Sentry is enabled for runtime error reporting."
    )


@patch("main.get_telegram_persistence_path", return_value=Path("/tmp/telegram_state.pkl"))
@patch("main.PicklePersistence")
@patch("main.PersistenceInput")
@patch("main.setup_scheduler")
@patch("main.ApplicationBuilder")
@patch("main.reconcile_orphaned_sessions")
@patch("main.init_db")
@patch("main.bootstrap_sentry")
@patch("main.load_config")
def test_main_initializes_db_and_reconciles_sessions_before_polling(
    mock_load_config,
    mock_init_sentry,
    mock_init_db,
    mock_reconcile,
    mock_application_builder,
    mock_setup_scheduler,
    mock_persistence_input,
    mock_pickle_persistence,
    mock_get_telegram_persistence_path,
):
    mock_load_config.return_value = MagicMock(
        telegram_bot_token="token",
        allowed_user_id=123,
        gemini_api_key="gemini-key",
        db_path=Path("/tmp/assistant.db"),
        google_token_path=Path("/tmp/token.json"),
        google_credentials_path=Path("/tmp/credentials.json"),
    )

    app = MagicMock()
    app.bot_data = {}
    builder = MagicMock()
    mock_application_builder.return_value = builder
    builder.token.return_value = builder
    builder.persistence.return_value = builder
    builder.post_init.return_value = builder
    builder.build.return_value = app

    assert main.main() == 0

    mock_init_sentry.assert_called_once_with()
    mock_init_db.assert_called_once_with()
    mock_reconcile.assert_called_once_with()
    mock_persistence_input.assert_called_once_with(
        bot_data=False,
        chat_data=False,
        user_data=True,
        callback_data=False,
    )
    mock_pickle_persistence.assert_called_once_with(
        filepath="/tmp/telegram_state.pkl",
        store_data=mock_persistence_input.return_value,
    )
    builder.token.assert_called_once_with("token")
    builder.persistence.assert_called_once_with(mock_pickle_persistence.return_value)
    builder.post_init.assert_called_once_with(main.invalidate_restart_volatile_user_data)
    assert app.bot_data["allowed_user_id"] == 123
    mock_setup_scheduler.assert_called_once_with(app, 123)
    app.run_polling.assert_called_once_with()


@patch("main.capture_sentry_exception")
@patch("main.bootstrap_sentry")
@patch("main.load_config")
def test_main_returns_nonzero_when_config_is_invalid(
    mock_load_config,
    mock_init_sentry,
    mock_capture_app_exception,
):
    config_error = main.ConfigError("Missing required environment variable: ALLOWED_USER_ID")
    mock_load_config.side_effect = config_error

    assert main.main() == 1

    mock_init_sentry.assert_called_once_with()
    mock_capture_app_exception.assert_called_once_with(
        config_error,
        component="startup",
        operation="load_config",
        message="Missing required environment variable: ALLOWED_USER_ID",
        tags={"error_kind": "config"},
    )


@patch("main.capture_sentry_exception")
def test_handle_application_error_reports_context_error(mock_capture_app_exception):
    error = RuntimeError("telegram failure")
    context = MagicMock()
    context.error = error

    asyncio.run(main._handle_application_error(update={"id": 1}, context=context))

    mock_capture_app_exception.assert_called_once_with(
        error,
        component="telegram",
        operation="application_error",
        message="Unhandled Telegram application error",
        tags={"has_update": "true"},
    )


@patch("main.capture_sentry_exception")
def test_handle_application_error_ignores_missing_context_error(mock_capture_app_exception):
    context = MagicMock()
    context.error = None

    asyncio.run(main._handle_application_error(update=None, context=context))

    mock_capture_app_exception.assert_not_called()


@patch("observability.sentry.logger")
@patch("observability.sentry.sentry_sdk.capture_exception")
@patch("observability.sentry.sentry_sdk.new_scope")
def test_capture_app_exception_logs_and_reports_with_tags(
    mock_new_scope,
    mock_capture_exception,
    mock_logger,
):
    error = RuntimeError("boom")
    scope = MagicMock()
    mock_new_scope.return_value.__enter__.return_value = scope

    main.capture_sentry_exception(
        error,
        component="calendar_auth",
        operation="refresh_token",
        message="Captured app failure",
        tags={"token_path": "/tmp/token.json"},
    )

    mock_logger.opt.assert_called_once_with(exception=error)
    mock_logger.opt.return_value.error.assert_called_once_with("Captured app failure")
    assert scope.set_tag.call_args_list == [
        (("component", "calendar_auth"),),
        (("operation", "refresh_token"),),
        (("token_path", "/tmp/token.json"),),
    ]
    mock_capture_exception.assert_called_once_with(error)
