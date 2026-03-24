from loguru import logger
from telegram.ext import ContextTypes

from orchestrator.context_builder import build_context
from reasoning.flash_client import generate_flash_response, FlashResponse
from orchestrator.session_manager import get_chat_history, append_chat_history

async def process_message(text: str, context: ContextTypes.DEFAULT_TYPE) -> FlashResponse:
    """Processes an incoming text message, handles model routing, and updates history."""
    try:
        context_block = build_context()
        chat_history = get_chat_history(context)
        
        flash_response = generate_flash_response(
            user_message=text, 
            context_block=context_block, 
            chat_history=chat_history
        )
        
        logger.info(f"Flash Escalate Signal: {flash_response.should_escalate}")
        if flash_response.should_escalate:
            logger.info(f"Escalation Reason: {flash_response.escalation_reason}")
            
        append_chat_history(context, "user", text)
        append_chat_history(context, "assistant", flash_response.message)
        
        return flash_response
    except Exception as e:
        logger.error(f"Error during reasoning loop: {e}")
        raise
