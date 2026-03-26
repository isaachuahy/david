import os
from typing import List
from string import Template
from pydantic import BaseModel, Field
from google import genai
from loguru import logger

from reasoning.parser import parse_model_response

# Resolve paths for the prompt template
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROMPTS_DIR = os.path.join(BASE_DIR, "reasoning", "prompts")

class ProposedEvent(BaseModel):
    summary: str = Field(description="The title of the calendar event.")
    start_time: str = Field(description="The start time in ISO 8601 format (UTC), e.g., 2026-03-22T09:00:00Z")
    end_time: str = Field(description="The end time in ISO 8601 format (UTC), e.g., 2026-03-22T11:00:00Z")
    description: str = Field(description="A brief description of the event's purpose.")

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
    
    prompt_path = os.path.join(PROMPTS_DIR, "sunday_review.txt")
    try:
        with open(prompt_path, "r", encoding="utf-8") as f:
            template = Template(f.read())
    except Exception as e:
        logger.error(f"Failed to read sunday_review.txt: {e}")
        raise

    prompt = template.safe_substitute(
        context_block=context_block,
        past_events_block=past_events_block
    )
    
    client = genai.Client()
    
    response = client.models.generate_content(
        model='gemini-3-pro-preview',
        contents=prompt,
        config={
            'response_mime_type': 'application/json',
            'response_schema': SundayReviewResponse,
            'temperature': 1.0 # Google recommends temperature of 1.0
        }
    )
    
    return parse_model_response(response, SundayReviewResponse)