"""State-constrained routing for David's conversation and draft handlers."""

from typing import Literal

from loguru import logger
from pydantic import BaseModel, ConfigDict

from config import get_routing_model
from observability.context_usage import record_model_usage, TOKEN_FIELDS
from observability.sentry import capture_exception as capture_sentry_exception
from reasoning.model_client import generate_structured
from runtime_paths import get_prompt_path


class RoutingDecision(BaseModel):
    """Identifies one requested operation and its existing draft, when applicable."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    operation: Literal["discuss", "create_draft", "revise_draft", "clarify"]
    target_id: str | None

    @property
    def allows_calendar_proposals(self) -> bool:
        """Discussion and clarification cannot produce new calendar draft fields."""
        return self.operation in {"create_draft", "revise_draft"}


def _can_revise(draft: dict) -> bool:
    """Resumed, resolved, and retry-blocked drafts keep their application limits."""
    return draft["status"] == "pending" and draft.get("can_revise", True)


def allowed_operations(state: dict) -> list[str]:
    """Derives capabilities from application state without interpreting language."""
    operations = ["discuss", "clarify"]
    if state.get("can_create_draft", True):
        operations.append("create_draft")
    # At least one live, editable draft must exist before revision is offered.
    if state.get("can_revise_drafts", True) and any(_can_revise(draft) for draft in state["drafts"]):
        operations.append("revise_draft")
    return operations


def operation_schema(state: dict) -> dict:
    """Restricts generation to available operations and known draft references."""
    return {
        "type": "object", "additionalProperties": False,
        "properties": {
            "operation": {"type": "string", "enum": allowed_operations(state)},
            # The validator below also checks which IDs permit revision.
            "target_id": {"type": ["string", "null"], "enum": [None] + [d["id"] for d in state["drafts"]]},
        },
        "required": ["operation", "target_id"],
    }


def valid_operation(prediction: dict, state: dict) -> bool:
    """Checks relationships JSON shape alone cannot enforce across fields."""
    if set(prediction) != {"operation", "target_id"}:
        return False
    operation, target = prediction["operation"], prediction["target_id"]
    if not isinstance(operation, str) or operation not in allowed_operations(state):
        return False
    # Resolve references against this turn's supplied objects, never invented IDs.
    drafts = {draft["id"]: draft for draft in state["drafts"]}
    if target is not None and (not isinstance(target, str) or target not in drafts):
        return False
    if operation == "revise_draft":
        return target in drafts and _can_revise(drafts[target])
    if operation in {"create_draft", "clarify"}:
        return target is None
    return True


def route_turn(text: str, chat_history: list[dict], state: dict) -> RoutingDecision:
    """Classifies a turn through the shared client without changing application state."""
    model = get_routing_model()
    # Preserve qualifications and references anywhere in the current session.
    # Capacity is measured from provider usage; history is never silently cut.
    history = [
        {"role": turn.get("role"), "content": str(turn.get("content", ""))}
        for turn in chat_history
    ]
    reply = None
    try:
        reply = generate_structured(
            provider=model.provider, model=model.model,
            instruction=get_prompt_path("routing.txt").read_text(encoding="utf-8"),
            payload={"message": text, "conversation": history, "state": state,
                     "allowed_operations": allowed_operations(state)},
            schema=operation_schema(state), reasoning=model.reasoning,
            max_output_tokens=1024, timeout=model.timeout_seconds,
            allow_provider_fallbacks=True,
        )
        if reply.finish_reason != "stop":
            raise ValueError("Routing generation did not complete.")
        decision = RoutingDecision.model_validate_json(reply.text)
        if decision.operation == "revise_draft" and "revise_draft" not in allowed_operations(state):
            # A revision without editable pending work needs clarification, not
            # a guessed target or a newly created draft. Clear any proposed ID.
            logger.info(
                "Routing fallback revise_draft target_id={} -> clarify: no revisable draft.",
                decision.target_id,
            )
            decision = RoutingDecision(operation="clarify", target_id=None)
        if not valid_operation(decision.model_dump(), state):
            raise ValueError("Routing result is not allowed by the current state.")
    except Exception as error:
        capture_sentry_exception(error, component="router", operation="route_turn", tags={"model": model.model})
        raise ValueError("I couldn't interpret that message reliably. Please try again.") from error
    finally:
        # Invalid decisions still consumed tokens. Failed requests without usage
        # remain explicitly unreported in the session summary.
        record_model_usage(
            provider=model.provider, model=model.model, operation="routing",
            usage={field: getattr(reply, field, None) for field in TOKEN_FIELDS}, history=history,
        )

    logger.info(
        "Routing model={} provider={} elapsed_ms={:.0f} input_tokens={} output_tokens={} cost_usd={} decision={}",
        model.model, reply.serving_provider, reply.latency_ms,
        reply.input_tokens, reply.output_tokens, reply.cost_usd, decision.model_dump(),
    )
    return decision
