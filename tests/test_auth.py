from unittest.mock import MagicMock, mock_open, patch

import pytest

from integrations.auth import get_calendar_credentials

@patch("integrations.auth.capture_sentry_exception")
@patch("integrations.auth.get_google_credentials_path")
@patch("integrations.auth.get_google_token_path")
@patch("integrations.auth.Credentials.from_authorized_user_file")
def test_get_calendar_credentials_reports_token_load_failure(
    mock_from_authorized_user_file,
    mock_get_google_token_path,
    mock_get_google_credentials_path,
    mock_capture_exception,
):
    token_path = MagicMock()
    token_path.exists.return_value = True
    credentials_path = MagicMock()
    credentials_path.exists.return_value = False
    mock_get_google_token_path.return_value = token_path
    mock_get_google_credentials_path.return_value = credentials_path
    token_load_error = ValueError("corrupt token")
    mock_from_authorized_user_file.side_effect = token_load_error

    with pytest.raises(FileNotFoundError):
        get_calendar_credentials()

    assert mock_capture_exception.call_count == 2
    assert mock_capture_exception.call_args_list[0].args == (token_load_error,)
    assert mock_capture_exception.call_args_list[0].kwargs == {
        "component": "calendar_auth",
        "operation": "load_token",
    }
    missing_error = mock_capture_exception.call_args_list[1].args[0]
    assert "OAuth credentials not found" in str(missing_error)
    assert mock_capture_exception.call_args_list[1].kwargs == {
        "component": "calendar_auth",
        "operation": "missing_credentials_file",
    }


@patch("integrations.auth.capture_sentry_exception")
@patch("integrations.auth.Request")
@patch("integrations.auth.get_google_credentials_path")
@patch("integrations.auth.get_google_token_path")
@patch("integrations.auth.Credentials.from_authorized_user_file")
@patch("integrations.auth.InstalledAppFlow.from_client_secrets_file")
@patch("builtins.open", new_callable=mock_open)
def test_get_calendar_credentials_reports_refresh_failure(
    mock_file,
    mock_flow_from_client_secrets,
    mock_from_authorized_user_file,
    mock_get_google_token_path,
    mock_get_google_credentials_path,
    mock_request_class,
    mock_capture_exception,
):
    token_path = MagicMock()
    token_path.exists.return_value = True
    token_path.parent = MagicMock()
    credentials_path = MagicMock()
    credentials_path.exists.return_value = True
    mock_get_google_token_path.return_value = token_path
    mock_get_google_credentials_path.return_value = credentials_path

    creds = MagicMock()
    creds.valid = False
    creds.expired = True
    creds.refresh_token = "refresh-token"
    refresh_error = RuntimeError("refresh failed")
    creds.refresh.side_effect = refresh_error
    mock_from_authorized_user_file.return_value = creds

    flow = MagicMock()
    new_creds = MagicMock()
    new_creds.to_json.return_value = '{"token":"new"}'
    flow.run_local_server.return_value = new_creds
    mock_flow_from_client_secrets.return_value = flow

    result = get_calendar_credentials()

    assert result is new_creds
    mock_capture_exception.assert_called_once_with(
        refresh_error,
        component="calendar_auth",
        operation="refresh_token",
    )
    mock_request_class.assert_called_once_with()


@patch("integrations.auth.capture_sentry_exception")
@patch("integrations.auth.get_google_credentials_path")
@patch("integrations.auth.get_google_token_path")
def test_get_calendar_credentials_reports_missing_credentials_file(
    mock_get_google_token_path,
    mock_get_google_credentials_path,
    mock_capture_exception,
):
    token_path = MagicMock()
    token_path.exists.return_value = False
    credentials_path = MagicMock()
    credentials_path.exists.return_value = False
    mock_get_google_token_path.return_value = token_path
    mock_get_google_credentials_path.return_value = credentials_path

    with pytest.raises(FileNotFoundError, match="OAuth credentials not found"):
        get_calendar_credentials()

    missing_error = mock_capture_exception.call_args.args[0]
    assert "OAuth credentials not found" in str(missing_error)
    mock_capture_exception.assert_called_once_with(
        missing_error,
        component="calendar_auth",
        operation="missing_credentials_file",
    )


@patch("integrations.auth.capture_sentry_exception")
@patch("integrations.auth.get_google_credentials_path")
@patch("integrations.auth.get_google_token_path")
@patch("integrations.auth.InstalledAppFlow.from_client_secrets_file")
def test_get_calendar_credentials_reports_oauth_bootstrap_failure(
    mock_flow_from_client_secrets,
    mock_get_google_token_path,
    mock_get_google_credentials_path,
    mock_capture_exception,
):
    token_path = MagicMock()
    token_path.exists.return_value = False
    credentials_path = MagicMock()
    credentials_path.exists.return_value = True
    mock_get_google_token_path.return_value = token_path
    mock_get_google_credentials_path.return_value = credentials_path

    flow = MagicMock()
    bootstrap_error = RuntimeError("browser bootstrap failed")
    flow.run_local_server.side_effect = bootstrap_error
    mock_flow_from_client_secrets.return_value = flow

    with pytest.raises(RuntimeError, match="browser bootstrap failed"):
        get_calendar_credentials()

    mock_capture_exception.assert_called_once_with(
        bootstrap_error,
        component="calendar_auth",
        operation="oauth_bootstrap",
    )


@patch("integrations.auth.capture_sentry_exception")
@patch("integrations.auth.get_google_credentials_path")
@patch("integrations.auth.get_google_token_path")
@patch("integrations.auth.InstalledAppFlow.from_client_secrets_file")
@patch("builtins.open", side_effect=OSError("disk full"))
def test_get_calendar_credentials_reports_token_save_failure(
    mock_file,
    mock_flow_from_client_secrets,
    mock_get_google_token_path,
    mock_get_google_credentials_path,
    mock_capture_exception,
):
    token_path = MagicMock()
    token_path.exists.return_value = False
    token_path.parent = MagicMock()
    credentials_path = MagicMock()
    credentials_path.exists.return_value = True
    mock_get_google_token_path.return_value = token_path
    mock_get_google_credentials_path.return_value = credentials_path

    flow = MagicMock()
    new_creds = MagicMock()
    new_creds.to_json.return_value = '{"token":"new"}'
    flow.run_local_server.return_value = new_creds
    mock_flow_from_client_secrets.return_value = flow

    with pytest.raises(OSError, match="disk full"):
        get_calendar_credentials()

    save_error = mock_capture_exception.call_args.args[0]
    assert "disk full" in str(save_error)
    mock_capture_exception.assert_called_once_with(
        save_error,
        component="calendar_auth",
        operation="save_token",
    )
