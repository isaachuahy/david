import os
import pytest
from unittest.mock import patch, mock_open, MagicMock

from reasoning.flash_client import generate_flash_response, PROMPTS_DIR, FlashResponse

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

@patch("builtins.open", side_effect=FileNotFoundError("File not found"))
@patch("reasoning.flash_client.genai.Client")
def test_generate_flash_response_file_read_error(mock_client, mock_file):
    # Verify that if the file is missing, the error surfaces properly
    with pytest.raises(FileNotFoundError):
        generate_flash_response("Hello", "<CONTEXT>")
