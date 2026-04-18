import os
import pytest
from unittest.mock import patch, mock_open, MagicMock

from reasoning.pro_client import generate_sunday_review, PROMPTS_DIR, SundayReviewResponse


@patch("reasoning.pro_client.genai.Client")
@patch("builtins.open", new_callable=mock_open, read_data="Context: $context_block\nPast: $past_events_block")
def test_generate_sunday_review_success(mock_file, mock_client_class):
    # Setup mock client and response
    mock_client_instance = MagicMock()
    mock_client_class.return_value = mock_client_instance
    
    mock_response = MagicMock()
    mock_response.parsed = SundayReviewResponse(
        message="Mocked Sunday Review",
        state_change_summary="Mocked changes",
        weekly_state_content="Mocked weekly state",
        proposed_events=[]
    )
    mock_client_instance.models.generate_content.return_value = mock_response

    # Call the function
    generate_sunday_review("Mocked Context", "Mocked Past Events")

    # Assert file was opened correctly
    expected_path = os.path.join(PROMPTS_DIR, "sunday_review.txt")
    mock_file.assert_called_once_with(expected_path, "r", encoding="utf-8")

    # Assert generate_content was called with the correctly substituted string
    generate_content_kwargs = mock_client_instance.models.generate_content.call_args.kwargs
    assert generate_content_kwargs['contents'] == "Context: Mocked Context\nPast: Mocked Past Events"

@patch("reasoning.pro_client.capture_sentry_exception")
@patch("builtins.open", side_effect=FileNotFoundError("File not found"))
@patch("reasoning.pro_client.genai.Client")
def test_generate_sunday_review_file_read_error(
    mock_client,
    mock_file,
    mock_capture_exception,
):
    # Verify that if the file is missing, the error surfaces properly
    with pytest.raises(FileNotFoundError):
        generate_sunday_review("Mocked Context", "Mocked Past Events")

    file_error = mock_capture_exception.call_args.args[0]
    assert "File not found" in str(file_error)
    mock_capture_exception.assert_called_once_with(
        file_error,
        component="gemini_pro",
        operation="read_prompt:sunday_review.txt",
    )


@patch("reasoning.pro_client.capture_sentry_exception")
@patch("builtins.open", new_callable=mock_open, read_data="Context: $context_block\nPast: $past_events_block")
@patch("reasoning.pro_client.genai.Client")
def test_generate_sunday_review_reports_gemini_request_failures(
    mock_client_class,
    mock_file,
    mock_capture_exception,
):
    mock_client_instance = MagicMock()
    mock_client_instance.models.generate_content.side_effect = RuntimeError("Gemini Pro down")
    mock_client_class.return_value = mock_client_instance

    with pytest.raises(RuntimeError, match="Gemini Pro down"):
        generate_sunday_review("Mocked Context", "Mocked Past Events")

    mock_capture_exception.assert_called_once_with(
        mock_client_instance.models.generate_content.side_effect,
        component="gemini_pro",
        operation="generate_sunday_review",
    )


@patch("reasoning.pro_client.capture_sentry_exception")
@patch("reasoning.pro_client.parse_model_response", side_effect=ValueError("Bad Sunday schema"))
@patch("builtins.open", new_callable=mock_open, read_data="Context: $context_block\nPast: $past_events_block")
@patch("reasoning.pro_client.genai.Client")
def test_generate_sunday_review_reports_parse_failures(
    mock_client_class,
    mock_file,
    mock_parse_model_response,
    mock_capture_exception,
):
    mock_client_instance = MagicMock()
    mock_client_instance.models.generate_content.return_value = MagicMock()
    mock_client_class.return_value = mock_client_instance

    with pytest.raises(ValueError, match="Bad Sunday schema"):
        generate_sunday_review("Mocked Context", "Mocked Past Events")

    mock_capture_exception.assert_called_once_with(
        mock_parse_model_response.side_effect,
        component="gemini_pro",
        operation="parse_sunday_review",
    )
