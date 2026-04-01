from pathlib import Path
from unittest.mock import MagicMock, patch

import main


@patch("main.setup_scheduler")
@patch("main.ApplicationBuilder")
@patch("main.reconcile_orphaned_sessions")
@patch("main.init_db")
@patch("main.load_config")
def test_main_initializes_db_and_reconciles_sessions_before_polling(
    mock_load_config,
    mock_init_db,
    mock_reconcile,
    mock_application_builder,
    mock_setup_scheduler,
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
    mock_application_builder.return_value.token.return_value.build.return_value = app

    assert main.main() == 0

    mock_init_db.assert_called_once_with()
    mock_reconcile.assert_called_once_with()
    assert app.bot_data["allowed_user_id"] == 123
    mock_setup_scheduler.assert_called_once_with(app, 123)
    app.run_polling.assert_called_once_with()


@patch("main.logger")
@patch("main.load_config")
def test_main_returns_nonzero_when_config_is_invalid(mock_load_config, mock_logger):
    mock_load_config.side_effect = main.ConfigError("Missing required environment variable: ALLOWED_USER_ID")

    assert main.main() == 1

    mock_logger.error.assert_called_once_with(
        "Missing required environment variable: ALLOWED_USER_ID"
    )
