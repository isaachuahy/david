from typing import List
from string import Template
from pydantic import BaseModel, Field
from google import genai
from loguru import logger
from observability.sentry import capture_exception as capture_sentry_exception

from reasoning.parser import parse_model_response
from reasoning.schemas import ProposedEvent
from runtime_paths import DEFAULT_PROMPTS_DIR, get_prompt_path

# Kept for existing tests and diagnostics; prompt reads use get_prompt_path.
PROMPTS_DIR = str(DEFAULT_PROMPTS_DIR)

class SundayReviewResponse(BaseModel):
    message: str = Field(description="A direct message to the user summarising the analysis.")
    state_change_summary: str = Field(description="A short, human-readable summary of the specific changes you are making to the weekly state.")
    weekly_state_content: str = Field(description="The exact markdown text to overwrite the weekly_state.md file.")
    proposed_events: List[ProposedEvent] = Field(default_factory=list, description="A list of proposed calendar events to schedule for the upcoming week.")

def generate_sunday_review(context_block: str, past_events_block: str) -> SundayReviewResponse:
    """
    Executes the Sunday Review using Gemini Pro.
    Reads the template, injects context, and returns structured output.
    """
    logger.info("Sending Sunday Review request to Gemini Pro...")
    
    prompt_path = get_prompt_path("sunday_review.txt")
    try:
        with open(str(prompt_path), "r", encoding="utf-8") as f:
            template = Template(f.read())
    except Exception as e:
        logger.error(f"Failed to read sunday_review.txt: {e}")
        capture_sentry_exception(
            e,
            component="gemini_pro",
            operation="read_prompt:sunday_review.txt",
        )
        raise

    # Template is designed with placeholders $context_block and $past_events_block for dynamic content injection
    # safe_substitute will raise an error if placeholders are missing, ensuring we catch template issues earlymes
    prompt = template.safe_substitute(
        context_block=context_block,
        past_events_block=past_events_block
    )
    
    client = genai.Client()
    
    try:
        response = client.models.generate_content(
            model='gemini-3-pro-preview',
            contents=prompt,
            config={
                'response_mime_type': 'application/json',
                'response_schema': SundayReviewResponse,
                'temperature': 1.0 # Google recommends temperature of 1.0
            }
        )
    except Exception as e:
        logger.error(f"Gemini Pro Sunday review request failed: {e}")
        capture_sentry_exception(e, component="gemini_pro", operation="generate_sunday_review")
        raise

    try:
        return parse_model_response(response, SundayReviewResponse)
    except Exception as e:
        logger.error(f"Gemini Pro Sunday review parsing failed: {e}")
        capture_sentry_exception(e, component="gemini_pro", operation="parse_sunday_review")
        raise
