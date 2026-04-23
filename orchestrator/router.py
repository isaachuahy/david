import asyncio
from dataclasses import dataclass
from enum import Enum
from loguru import logger
from telegram.ext import ContextTypes

from observability.sentry import capture_exception as capture_sentry_exception
from orchestrator.context_builder import build_context
from reasoning.flash_client import generate_flash_response, FlashResponse
from orchestrator.session_manager import get_chat_history, append_chat_history

class MessageIntent(Enum):
    OPERATIONAL = "operational"
    STRATEGIC = "strategic"


@dataclass(frozen=True)
class RoutingDecision:
    """
    Captures both how we should reason about the message and which long-lived
    context sections are needed to answer it well.
    """

    intent: MessageIntent
    needs_calendar_context: bool
    needs_strategy_context: bool

THINKING_LEVELS = {
    MessageIntent.OPERATIONAL: "low",
    MessageIntent.STRATEGIC: "high",
}


def _contains_any(text: str, keywords: tuple[str, ...]) -> bool:
    """Returns True when the message contains any keyword or phrase."""
    return any(keyword in text for keyword in keywords)


def _select_context_profile(routing_decision: RoutingDecision) -> str:
    """
    Maps routing flags into the smallest context profile that still gives the
    model the information it needs for this turn.
    """
    if routing_decision.needs_calendar_context and routing_decision.needs_strategy_context:
        return "full"
    if routing_decision.needs_calendar_context:
        return "calendar_context"
    if routing_decision.needs_strategy_context:
        return "priority_strategy"
    return "lean"


def classify_intent(text: str) -> RoutingDecision:
    """
    Classifies the message into a reasoning mode and the context dependencies
    needed for that mode.
    """
    text_lower = text.lower()

    # Calendar-aware prompts need current schedule state to answer correctly or
    # to ground a proposed event in real availability.
    calendar_keywords = (
        "schedule",
        "reschedule",
        "cancel",
        "move",
        "book",
        "calendar",
        "event",
        "meeting",
        "appointment",
        "free",
        "available",
        "busy",
        "open slot",
        "next event",
        "later today",
        "this afternoon",
        "tonight",
        "tomorrow morning",
        "time block",
        "block off",
        "fit this in",
    )
    needs_calendar_context = _contains_any(text_lower, calendar_keywords)

    # Strategy-aware prompts need longer-lived memory so David can reason about
    # priorities, tradeoffs, and continuity across sessions.
    strategy_keywords = (
        "goal",
        "goals",
        "priority",
        "priorities",
        "focus",
        "application",
        "applications",
        "direction",
        "tradeoff",
        "tradeoffs",
        "align",
        "alignment",
        "what should i do",
        "what should i focus on",
        "plan for the week",
        "weekly plan",
        "review",
        "brainstorm",
        "what if",
        "let's discuss",
        "think about",
        "explore",
        "idea",
        "remember",
        "last time",
        "we decided",
        "preference",
        "why did we",
    )
    needs_strategy_context = _contains_any(text_lower, strategy_keywords)

    intent = (
        MessageIntent.STRATEGIC
        if needs_strategy_context
        else MessageIntent.OPERATIONAL
    )
    return RoutingDecision(
        intent=intent,
        needs_calendar_context=needs_calendar_context,
        needs_strategy_context=needs_strategy_context,
    )

async def process_message(text: str, context: ContextTypes.DEFAULT_TYPE) -> FlashResponse:
    """Processes an incoming text message, handles model routing, and updates history."""
    routing_decision = None
    try:
        routing_decision = classify_intent(text)
        context_profile = _select_context_profile(routing_decision)
        context_block = await asyncio.to_thread(build_context, context, profile=context_profile)
        chat_history = get_chat_history(context)

        thinking_level = THINKING_LEVELS.get(routing_decision.intent)
        logger.info(
            "Classified intent as {} with level: {} and context profile: {}",
            routing_decision.intent.value,
            thinking_level,
            context_profile,
        )
        
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
        capture_sentry_exception(
            e,
            component="router",
            operation="process_message",
            tags={"intent": routing_decision.intent.value} if routing_decision is not None else None,
        )
        raise
