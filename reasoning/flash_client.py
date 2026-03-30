import os
from string import Template
from typing import Optional
from pydantic import BaseModel, Field
from google import genai
from loguru import logger

from reasoning.parser import parse_model_response
from reasoning.schemas import ProposedEvent

# Resolve paths for the prompt template
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROMPTS_DIR = os.path.join(BASE_DIR, "reasoning", "prompts")

class FlashResponse(BaseModel):
    message: str = Field(
        description="The text response to send directly back to the user."
    )
    proposed_calendar_action: Optional[ProposedEvent] = Field(
        default=None,
        description="If the user's request implies a calendar action (scheduling, modifying, or deleting an event), populate this field. Otherwise, leave it null."
    )

class SessionSynthesisResponse(BaseModel):
    content: str = Field(
        description="A concise markdown block to append directly to the Recent Decisions section of decision_log.md."
    )

def _read_prompt_template(prompt_filename: str) -> str:
    """Reads a prompt template from the prompts directory."""
    prompt_path = os.path.join(PROMPTS_DIR, prompt_filename)
    try:
        with open(prompt_path, "r", encoding="utf-8") as f:
            return f.read().strip()
    except Exception as e:
        logger.error(f"Failed to read {prompt_filename}: {e}")
        raise

def _format_chat_history(chat_history: list[dict]) -> str:
    """Formats chat history into a plain-text transcript for prompting."""
    lines = []
    for turn in chat_history:
        role = "User" if turn.get("role") == "user" else "David"
        lines.append(f"{role}: {turn.get('content')}")
    return "\n\n".join(lines)

def generate_flash_response(user_message: str, context_block: str, chat_history: Optional[list[dict]] = None,
                            thinking_level: Optional[str] = None) -> FlashResponse:
    """
    Sends the assembled context and user message to Gemini Flash.
    Enforces a strict Pydantic schema for the response.
    """
    logger.info("Sending request to Gemini Flash...")
    
    # Automatically picks up GEMINI_API_KEY from the environment
    client = genai.Client()
    
    system_instruction = _read_prompt_template("system_prompt.txt")
    
    prompt = f"{context_block}\n\n"
    
    if chat_history:
        prompt += "<CHAT_HISTORY>\n"
        prompt += _format_chat_history(chat_history)
        prompt += "\n"
        prompt += "</CHAT_HISTORY>\n\n"
        
    prompt += f"<USER_MESSAGE>\n{user_message}\n</USER_MESSAGE>"
    
    # Base configuration
    generation_config = {
        'response_mime_type': 'application/json',
        'response_schema': FlashResponse,
        'system_instruction': system_instruction,
        'temperature': 1.0
    }
    
    if thinking_level:
        generation_config['thinking_config'] = {'thinking_level': thinking_level}

    response = client.models.generate_content(
        model='gemini-3-flash-preview',
        contents=prompt,
        config=generation_config
    )
    
    return parse_model_response(response, FlashResponse)

def generate_session_synthesis(chat_history: list[dict], session_date: str) -> SessionSynthesisResponse:
    """
    Synthesizes a completed session transcript into an append-ready markdown block.
    Uses Gemini Flash with a high thinking budget to keep the flow cost-effective.
    """
    logger.info("Sending session synthesis request to Gemini Flash...")

    client = genai.Client()
    template = Template(_read_prompt_template("synthesis.txt"))
    prompt = template.safe_substitute(
        chat_history=_format_chat_history(chat_history),
        session_date=session_date
    )

    response = client.models.generate_content(
        model='gemini-3-flash-preview',
        contents=prompt,
        config={
            'temperature': 1.0,
            'thinking_config': {'thinking_level': 'high'}
        }
    )

    content = response.text.strip()
    if not content:
        raise ValueError("Gemini Flash returned an empty session synthesis response.")

    return SessionSynthesisResponse(content=content)
