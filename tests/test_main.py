from unittest.mock import MagicMock, patch

import main


@patch("main.setup_scheduler")
@patch("main.ApplicationBuilder")
@patch("main.reconcile_orphaned_sessions")
@patch("main.init_db")
@patch("main.os.getenv")
def test_main_initializes_db_and_reconciles_sessions_before_polling(
    mock_getenv,
    mock_init_db,
    mock_reconcile,
    mock_application_builder,
    mock_setup_scheduler,
):
    mock_getenv.side_effect = lambda key: {
        "TELEGRAM_BOT_TOKEN": "token",
        "AUTHORIZED_USER_ID": "123",
    }.get(key)

    app = MagicMock()
    mock_application_builder.return_value.token.return_value.build.return_value = app

    main.main()

    mock_init_db.assert_called_once_with()
    mock_reconcile.assert_called_once_with()
    app.run_polling.assert_called_once_with()
