import os
import pytest
from unittest.mock import patch, mock_open, MagicMock

from reasoning.pro_client import generate_sunday_review, PROMPTS_DIR

@patch("reasoning.pro_client.genai.Client")
@patch("builtins.open", new_callable=mock_open, read_data="Context: $context_block\nPast: $past_events_block")
def test_generate_sunday_review_success(mock_file, mock_client_class):
    # Setup mock client and response
    mock_client_instance = MagicMock()
    mock_client_class.return_value = mock_client_instance
    
    mock_response = MagicMock()
    mock_response.parsed = MagicMock()
    mock_client_instance.models.generate_content.return_value = mock_response

    # Call the function
    generate_sunday_review("Mocked Context", "Mocked Past Events")

    # Assert file was opened correctly
    expected_path = os.path.join(PROMPTS_DIR, "sunday_review.txt")
    mock_file.assert_called_once_with(expected_path, "r", encoding="utf-8")

    # Assert generate_content was called with the correctly substituted string
    generate_content_kwargs = mock_client_instance.models.generate_content.call_args.kwargs
    assert generate_content_kwargs['contents'] == "Context: Mocked Context\nPast: Mocked Past Events"

@patch("builtins.open", side_effect=FileNotFoundError("File not found"))
@patch("reasoning.pro_client.genai.Client")
def test_generate_sunday_review_file_read_error(mock_client, mock_file):
    # Verify that if the file is missing, the error surfaces properly
    with pytest.raises(FileNotFoundError):
        generate_sunday_review("Mocked Context", "Mocked Past Events")
