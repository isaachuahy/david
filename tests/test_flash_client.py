import os
import pytest
from unittest.mock import patch, mock_open, MagicMock

from reasoning.flash_client import (
    generate_flash_response,
    generate_session_synthesis,
    PROMPTS_DIR,
    FlashResponse,
)


@patch("reasoning.flash_client.genai.Client")
@patch("builtins.open", new_callable=mock_open, read_data="Mocked system prompt.")
def test_generate_flash_response_reads_system_prompt(mock_file, mock_client_class):
    # Setup mock client and response
    mock_client_instance = MagicMock()
    mock_client_class.return_value = mock_client_instance
    
    mock_response = MagicMock()
    mock_response.parsed = FlashResponse(message="Mocked flash response.")
    mock_client_instance.models.generate_content.return_value = mock_response

    # Call the function
    generate_flash_response("Hello", "<CONTEXT>")

    # Assert file was opened correctly
    expected_path = os.path.join(PROMPTS_DIR, "system_prompt.txt")
    mock_file.assert_called_once_with(expected_path, "r", encoding="utf-8")

    # Assert generate_content was called with the loaded system prompt
    generate_content_kwargs = mock_client_instance.models.generate_content.call_args.kwargs
    assert generate_content_kwargs['config']['system_instruction'] == "Mocked system prompt."

@patch("reasoning.flash_client.capture_sentry_exception")
@patch("builtins.open", side_effect=FileNotFoundError("File not found"))
@patch("reasoning.flash_client.genai.Client")
def test_generate_flash_response_file_read_error(
    mock_client,
    mock_file,
    mock_capture_exception,
):
    # Verify that if the file is missing, the error surfaces properly
    with pytest.raises(FileNotFoundError):
        generate_flash_response("Hello", "<CONTEXT>")

    file_error = mock_capture_exception.call_args.args[0]
    assert "File not found" in str(file_error)
    mock_capture_exception.assert_called_once_with(
        file_error,
        component="gemini_flash",
        operation="read_prompt:system_prompt.txt",
    )


@patch("reasoning.flash_client.capture_sentry_exception")
@patch("builtins.open", new_callable=mock_open, read_data="Mocked system prompt.")
@patch("reasoning.flash_client.genai.Client")
def test_generate_flash_response_reports_gemini_request_failures(
    mock_client_class,
    mock_file,
    mock_capture_exception,
):
    mock_client_instance = MagicMock()
    mock_client_instance.models.generate_content.side_effect = RuntimeError("Gemini down")
    mock_client_class.return_value = mock_client_instance

    with pytest.raises(RuntimeError, match="Gemini down"):
        generate_flash_response("Hello", "<CONTEXT>")

    mock_capture_exception.assert_called_once_with(
        mock_client_instance.models.generate_content.side_effect,
        component="gemini_flash",
        operation="generate_flash_response",
    )


@patch("reasoning.flash_client.capture_sentry_exception")
@patch("reasoning.flash_client.parse_model_response", side_effect=ValueError("Bad schema"))
@patch("builtins.open", new_callable=mock_open, read_data="Mocked system prompt.")
@patch("reasoning.flash_client.genai.Client")
def test_generate_flash_response_reports_parse_failures(
    mock_client_class,
    mock_file,
    mock_parse_model_response,
    mock_capture_exception,
):
    mock_client_instance = MagicMock()
    mock_client_instance.models.generate_content.return_value = MagicMock()
    mock_client_class.return_value = mock_client_instance

    with pytest.raises(ValueError, match="Bad schema"):
        generate_flash_response("Hello", "<CONTEXT>")

    mock_capture_exception.assert_called_once_with(
        mock_parse_model_response.side_effect,
        component="gemini_flash",
        operation="parse_flash_response",
    )

@patch("reasoning.flash_client.genai.Client")
@patch("builtins.open", new_callable=mock_open, read_data="### Session - $session_date\n\n<CHAT_HISTORY>\n$chat_history\n</CHAT_HISTORY>")
def test_generate_session_synthesis_injects_session_date_and_history(mock_file, mock_client_class):
    mock_client_instance = MagicMock()
    mock_client_class.return_value = mock_client_instance

    mock_response = MagicMock()
    mock_response.text = "### Session - 2026-03-30\n- Decided to focus on outreach."
    mock_client_instance.models.generate_content.return_value = mock_response

    result = generate_session_synthesis(
        [{"role": "user", "content": "Let's prioritize outreach."}],
        session_date="2026-03-30"
    )

    expected_path = os.path.join(PROMPTS_DIR, "synthesis.txt")
    mock_file.assert_called_once_with(expected_path, "r", encoding="utf-8")
    generate_content_kwargs = mock_client_instance.models.generate_content.call_args.kwargs
    assert "2026-03-30" in generate_content_kwargs["contents"]
    assert "User: Let's prioritize outreach." in generate_content_kwargs["contents"]
    assert generate_content_kwargs["config"]["thinking_config"] == {"thinking_level": "high"}
    assert result.content == "### Session - 2026-03-30\n- Decided to focus on outreach."


@patch("reasoning.flash_client.capture_sentry_exception")
@patch("builtins.open", new_callable=mock_open, read_data="### Session - $session_date\n\n<CHAT_HISTORY>\n$chat_history\n</CHAT_HISTORY>")
@patch("reasoning.flash_client.genai.Client")
def test_generate_session_synthesis_reports_empty_response(
    mock_client_class,
    mock_file,
    mock_capture_exception,
):
    mock_client_instance = MagicMock()
    mock_response = MagicMock()
    mock_response.text = "   "
    mock_client_instance.models.generate_content.return_value = mock_response
    mock_client_class.return_value = mock_client_instance

    with pytest.raises(ValueError, match="empty session synthesis response"):
        generate_session_synthesis(
            [{"role": "user", "content": "Let's prioritize outreach."}],
            session_date="2026-03-30",
        )

    empty_error = mock_capture_exception.call_args.args[0]
    assert "empty session synthesis response" in str(empty_error)
    mock_capture_exception.assert_called_once_with(
        empty_error,
        component="gemini_flash",
        operation="empty_session_synthesis",
    )
