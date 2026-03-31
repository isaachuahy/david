import asyncio
from enum import Enum
from loguru import logger
from telegram.ext import ContextTypes

from orchestrator.context_builder import build_context
from reasoning.flash_client import generate_flash_response, FlashResponse
from orchestrator.session_manager import get_chat_history, append_chat_history

# We use enum instead of string literals for message intents to ensure consistency and avoid typos.
# This also makes it easier to manage and update intents in the future, as we can simply add new values to the enum.
class MessageIntent(Enum):
    OPERATIONAL = "operational"
    BRAINSTORM = "brainstorm"
    GOAL_REVIEW = "goal_review"

THINKING_LEVELS = {
    MessageIntent.OPERATIONAL: "low",    # For lowest level, we can use a low thinking level to prioritize latency and cost.
    MessageIntent.BRAINSTORM: "high",    # Brainstorming may require more creative and expansive thinking, so we can use a high thinking level to allow for more exploration and idea generation.
    MessageIntent.GOAL_REVIEW: "high",  # Goal review may also benefit from a high thinking level to allow for more strategic and big-picture analysis, especially if the user is asking for guidance on priorities or direction.
}

def classify_intent(text: str) -> MessageIntent:
    """Classifies user intent based on keywords."""
    text_lower = text.lower()
    
    # Heuristic keyword-based classification.
    # Testing and iteration will be needed to refine these keyword lists for better accuracy.
    # Also consider using a lightweight intent classification model in the future if we find that keyword-based classification is too brittle or inaccurate.
    goal_keywords = ["GOAL_REVIEW", "goal", "priority", "priorities", "direction", "plan for the week", "what should i do"]
    if any(keyword in text_lower for keyword in goal_keywords):
        return MessageIntent.GOAL_REVIEW
        
    brainstorm_keywords = ["brainstorm", "what if", "let's discuss", "think about", "explore", "idea"]
    if any(keyword in text_lower for keyword in brainstorm_keywords):
        return MessageIntent.BRAINSTORM
        
    return MessageIntent.OPERATIONAL

async def process_message(text: str, context: ContextTypes.DEFAULT_TYPE) -> FlashResponse:
    """Processes an incoming text message, handles model routing, and updates history."""
    try:
        context_block = await asyncio.to_thread(build_context, context)
        chat_history = get_chat_history(context)
        
        # Classify intent and determine thinking level
        intent = classify_intent(text)
        thinking_level = THINKING_LEVELS.get(intent)
        logger.info(f"Classified intent as {intent.value} with level: {thinking_level}")
        
        flash_response = await asyncio.to_thread(
            generate_flash_response,
            user_message=text,
            context_block=context_block,
            chat_history=chat_history,
            thinking_level=thinking_level,
        )
            
        append_chat_history(context, "user", text)
        append_chat_history(context, "assistant", flash_response.message)
        
        return flash_response
    except Exception as e:
        logger.error(f"Error during reasoning loop: {e}")
        raise
