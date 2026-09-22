"""Measure model requests without putting telemetry into conversational memory."""

from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
import json
from threading import Lock

from loguru import logger

from config import MODEL_CONTEXT_LIMITS


_user_data: ContextVar[dict | None] = ContextVar("usage_user_data", default=None)
_lock = Lock()
TOKEN_FIELDS = ("input_tokens", "output_tokens", "cached_tokens", "reasoning_tokens")


@contextmanager
def usage_scope(user_data: dict):
    """Attribute nested model calls, including asyncio.to_thread calls, to this user."""
    token = _user_data.set(user_data)
    try:
        yield
    finally:
        _user_data.reset(token)


def _count(value) -> int | None:
    """Absent provider metadata is unknown, not zero or an estimated token count."""
    return value if type(value) is int and value >= 0 else None


def gemini_token_usage(response) -> dict:
    """Normalize Gemini output to include thinking, matching OpenRouter totals."""
    usage = getattr(response, "usage_metadata", None)
    candidates = _count(getattr(usage, "candidates_token_count", None))
    thoughts = _count(getattr(usage, "thoughts_token_count", None))
    return {
        "input_tokens": _count(getattr(usage, "prompt_token_count", None)),
        "output_tokens": candidates + (thoughts or 0) if candidates is not None else None,
        "cached_tokens": _count(getattr(usage, "cached_content_token_count", None)),
        "reasoning_tokens": thoughts,
    }


def history_size(history: list[dict]) -> dict:
    """Measure transcript size directly; characters are deliberately not called tokens."""
    return {
        "messages": len(history),
        # Measure only stored conversational content, without estimating tokenizer overhead.
        "characters": sum(len(str(turn.get("content", ""))) for turn in history),
    }


def record_model_usage(
    *, provider: str, model: str, operation: str, usage: dict,
    history: list[dict] | None = None, context_characters: int | None = None,
) -> None:
    """Log one request and accumulate reported usage separately from peak capacity."""
    counts = {field: _count(usage.get(field)) for field in TOKEN_FIELDS}
    limit_kind, limit = MODEL_CONTEXT_LIMITS.get((provider, model), (None, None))
    occupied = counts["input_tokens"]
    if limit_kind == "input_and_output":
        occupied = (
            occupied + counts["output_tokens"]
            if occupied is not None and counts["output_tokens"] is not None else None
        )
    percent = round(100 * occupied / limit, 3) if occupied is not None and limit else None
    data = _user_data.get()
    session_id = data.get("current_session_id") if data is not None else None
    entry = {
        "at": datetime.now(timezone.utc).isoformat(),
        "session_id": session_id, "provider": provider, "model": model,
        "operation": operation, **counts,
        "capacity_tokens": limit, "capacity_kind": limit_kind, "capacity_percent": percent,
        "history": history_size(history) if history is not None else None,
        "history_truncated": False if history is not None else None,
        "context_characters": context_characters,
    }
    logger.info("model_context_usage {}", json.dumps(entry))
    if data is None:
        return

    with _lock:
        # Reviews can run outside a chat session. Keep one latest measurement
        # for /context without inventing a session or changing session totals.
        data["last_model_usage"] = entry
        if not session_id:
            return
        # Keep only per-model aggregates and the last request in Telegram state;
        # individual request records already live in logs, without prompt content.
        summary = data.setdefault("session_usage", {"session_id": session_id, "models": {}})
        if summary["session_id"] != session_id:
            summary = data["session_usage"] = {"session_id": session_id, "models": {}}
        key = f"{operation}:{provider}:{model}"
        bucket = summary["models"].setdefault(key, {
            "requests": 0, "unreported_requests": 0,
            "reported_tokens": {field: 0 for field in TOKEN_FIELDS},
            "peak_input_tokens": None, "peak_capacity_percent": None,
        })
        bucket["requests"] += 1
        if counts["input_tokens"] is None or counts["output_tokens"] is None:
            bucket["unreported_requests"] += 1
        for field, count in counts.items():
            # Cached tokens are a subset of input; thinking is a subset of output.
            # Keep them separate and never add those subsets to token totals again.
            if count is not None:
                bucket["reported_tokens"][field] += count
        for field, value in (("peak_input_tokens", counts["input_tokens"]), ("peak_capacity_percent", percent)):
            if value is not None:
                previous = bucket[field]
                bucket[field] = max(previous, value) if previous is not None else value
        bucket["last_request"] = entry


def finish_session_usage(
    user_data: dict, session_id: str | None, history: list[dict], *, summary: dict | None = None,
) -> dict:
    """Finalize the captured session without consuming a newer session's usage."""
    with _lock:
        if summary is not None and summary.get("session_id") != session_id:
            raise ValueError("Usage summary belongs to a different session")
        stored = user_data.get("session_usage")
        if stored is not None and stored.get("session_id") == session_id:
            # A delayed synthesis job can outlive the shared user_data slot.
            # Only remove that slot when it still belongs to the closing job.
            user_data.pop("session_usage")
            if summary is None:
                summary = stored
        if summary is None:
            summary = {"session_id": session_id, "models": {}}
        summary["history"] = history_size(history)
        # Repeated history contributes to each request's input total. These are
        # cumulative provider counts, not the size of the final transcript.
        summary["reported_tokens"] = {
            field: sum(bucket["reported_tokens"][field] for bucket in summary["models"].values())
            for field in TOKEN_FIELDS
        }
        summary["unreported_requests"] = sum(
            bucket["unreported_requests"] for bucket in summary["models"].values()
        )
        summary["ended_at"] = datetime.now(timezone.utc).isoformat()
        user_data["last_session_usage"] = summary
    logger.info("session_context_usage {}", json.dumps(summary))
    return summary


def _format_request_usage(entry: dict) -> list[str]:
    """Use the same capacity labels for standalone reviews and session requests."""
    percent = entry["capacity_percent"]
    capacity = "unknown" if percent is None else f"{percent:.2f}%"
    kind = "input capacity" if entry["capacity_kind"] == "input" else "context window"
    limit = entry["capacity_tokens"]
    limit_text = f" / {limit:,} tokens" if limit else "; limit unknown"
    prompt_tokens = entry["input_tokens"]
    prompt_text = f"{prompt_tokens:,}" if prompt_tokens is not None else "unknown"
    output_tokens = entry["output_tokens"]
    output_text = f"{output_tokens:,}" if output_tokens is not None else "unknown"
    return [
        f"{entry['operation']} · {entry['model']}",
        f"Last request: {prompt_text} input tokens{limit_text}; {capacity} {kind}.",
        f"Output: {output_text} tokens (including thinking).",
    ]


def format_context_usage(user_data: dict) -> str:
    """Show measured request capacity and cumulative usage without making a model call."""
    summary = user_data.get("session_usage") or user_data.get("last_session_usage")
    latest = user_data.get("last_model_usage")
    lines = []
    if latest and latest["session_id"] is None:
        # This measurement is newer than any completed chat session. Show it
        # separately so review usage cannot be mistaken for old session totals.
        lines.extend([
            "Latest model request outside a chat session",
            *_format_request_usage(latest),
        ])
    if not summary:
        if lines:
            return "\n".join(lines)
        return "No model context measurements yet. Routing uses the full current-session history."
    active = bool(user_data.get("current_session_id")) and summary.get("session_id") == user_data["current_session_id"]
    history = history_size(user_data.get("chat_history", [])) if active else summary["history"]
    if lines:
        lines.append("")
    lines.extend([
        "Current session" if active else "Last completed session",
        f"History: {history['messages']:,} messages, {history['characters']:,} characters.",
        "Full current-session history is sent to routing and chat; no automatic truncation.",
    ])
    for bucket in summary["models"].values():
        # Report each role/model separately: summing context percentages would be misleading.
        last = bucket["last_request"]
        totals = bucket["reported_tokens"]
        lines.extend([
            "",
            *_format_request_usage(last),
            f"Session: {bucket['requests']} requests; {totals['input_tokens']:,} reported input / "
            f"{totals['output_tokens']:,} reported output tokens (including thinking).",
        ])
        if bucket["unreported_requests"]:
            lines.append(f"Usage incomplete for {bucket['unreported_requests']} request(s).")
    lines.append("\nSession totals count history again each time it is sent. Capacity is the last measured request, not a prediction of the next.")
    return "\n".join(lines)
