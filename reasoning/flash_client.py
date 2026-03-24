from typing import Optional, cast
from pydantic import BaseModel, Field
from google import genai
from loguru import logger

class FlashResponse(BaseModel):
    message: str = Field(
        description="The text response to send directly back to the user."
    )
    should_escalate: bool = Field(
        description="True if the request requires deeper reasoning, priority tradeoffs, or proposes writing to the calendar. False for standard chat, retrieving context, or simple operational queries."
    )
    escalation_reason: Optional[str] = Field(
        description="If should_escalate is True, briefly explain the tradeoff or proposed action to the Pro model. Otherwise, null."
    )

def generate_flash_response(user_message: str, context_block: str, chat_history: Optional[list[dict]] = None) -> FlashResponse:
    """
    Sends the assembled context and user message to Gemini Flash.
    Enforces a strict Pydantic schema for the response.
    """
    logger.info("Sending request to Gemini Flash...")
    
    # Automatically picks up GEMINI_API_KEY from the environment
    client = genai.Client()
    
    system_instruction = (
        "You are David, a highly competent personal executive assistant for a single user (Isaac). "
        "Your primary goal is to bridge the gap between intention and execution by holding context "
        "across time and reducing decision fatigue. "
        "Always consult the provided context (Goals, Weekly State, Decision Log, Calendar) before responding. "
        "If the user asks to schedule an event, modify the calendar, or asks a complex question requiring "
        "priority tradeoffs against their goals, you MUST set should_escalate to True."
    )
    
    prompt = f"{context_block}\n\n"
    
    if chat_history:
        prompt += "<CHAT_HISTORY>\n"
        for turn in chat_history:
            role = "User" if turn.get("role") == "user" else "David"
            prompt += f"{role}: {turn.get('content')}\n\n"
        prompt += "</CHAT_HISTORY>\n\n"
        
    prompt += f"<USER_MESSAGE>\n{user_message}\n</USER_MESSAGE>"
    
    response = client.models.generate_content(
        model='gemini-3-flash-preview',
        contents=prompt,
        config={
            'response_mime_type': 'application/json',
            'response_schema': FlashResponse,
            'system_instruction': system_instruction,
            'temperature': 1.0
        }
    )
    
    return cast(FlashResponse, response.parsed)