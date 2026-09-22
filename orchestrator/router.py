import asyncio
import json

from loguru import logger
from telegram.ext import ContextTypes

from observability.sentry import capture_exception as capture_sentry_exception
from orchestrator.context_builder import build_context
from orchestrator.session_manager import get_chat_history, append_chat_history
from reasoning.flash_client import generate_flash_response, FlashResponse
from reasoning.routing import RoutingDecision


async def process_message(
    text: str,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    routing_decision: RoutingDecision,
    routing_state: dict,
    workflow_context: str = "",
) -> FlashResponse:
    """Generates an answer from a handler-validated decision without classifying."""
    try:
        chat_history = get_chat_history(context)

        # Discussion can ask about availability or priorities. Give the answering
        # model the complete compact context instead of guessing dependencies
        # from operation labels; calendar reads retain the existing session cache.
        context_block = await asyncio.to_thread(build_context, context, profile="full")
        context_block += f"\n\n<TURN_ROUTING>\n{routing_decision.model_dump_json()}\n</TURN_ROUTING>"
        context_block += f"\n\n<PENDING_DRAFTS>\n{json.dumps(routing_state, ensure_ascii=False)}\n</PENDING_DRAFTS>"
        if workflow_context:
            context_block += f"\n\n<WORKFLOW_CONTEXT>\n{workflow_context}\n</WORKFLOW_CONTEXT>"
        if routing_decision.operation == "clarify":
            context_block += (
                "\nAsk one focused question to resolve the user's intended action or target. "
                "If an operation is unavailable, explain the required next step."
            )

        flash_response = await asyncio.to_thread(
            generate_flash_response,
            user_message=text,
            context_block=context_block,
            chat_history=chat_history,
            allow_calendar_proposals=routing_decision.allows_calendar_proposals,
        )
        append_chat_history(context, "user", text)
        append_chat_history(context, "assistant", flash_response.message)
        return flash_response
    except Exception as error:
        logger.error("Error during reasoning loop: {}", error)
        capture_sentry_exception(
            error, component="router", operation="process_message",
            tags={"operation": routing_decision.operation},
        )
        raise
