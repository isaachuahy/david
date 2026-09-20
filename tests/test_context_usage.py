"""Verify real usage attribution and accounting, independent of live providers."""

import asyncio
from copy import deepcopy
import json
from threading import Event
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from config import MODEL_CONTEXT_LIMITS
from bot.handlers import context_command, weekly_review_command
from observability.context_usage import (
    finish_session_usage, format_context_usage, gemini_token_usage,
    record_model_usage, usage_scope,
)
from reasoning.schemas import WeekReviewResponse


def test_finishing_old_session_preserves_new_session_usage():
    """A delayed finalizer may only consume the bucket for its own session."""
    data = {"current_session_id": "new"}
    with usage_scope(data):
        record_model_usage(
            provider="gemini", model="gemini-3-flash-preview", operation="chat",
            usage={"input_tokens": 75, "output_tokens": 10},
        )
    newer_usage = deepcopy(data["session_usage"])

    summary = finish_session_usage(data, "old", [{"content": "Old history"}])

    assert data["session_usage"] == newer_usage
    assert summary["session_id"] == "old"
    assert summary["reported_tokens"]["input_tokens"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("new_session_timing", ["before_job", "during_job"])
@pytest.mark.parametrize("synthesis_fails", [False, True])
async def test_delayed_synthesis_preserves_new_session(
    tmp_path, monkeypatch, new_session_timing, synthesis_fails,
):
    """Closing work keeps old usage even when another session replaces user_data."""
    from orchestrator.session_manager import end_session, execute_synthesis_task, start_session
    from persistence.database import get_db, init_db
    from persistence.models import SessionStatus

    monkeypatch.setenv("DAVID_DB_PATH", str(tmp_path / "assistant.db"))
    monkeypatch.setenv("DAVID_CONTEXT_DIR", str(tmp_path / "context"))
    init_db()
    assert "session_usage" not in get_db().table_names()
    context = MagicMock()
    context.user_data = {}
    context.bot.send_message = AsyncMock()
    old_id = start_session(context)
    context.user_data["chat_history"] = [{"role": "user", "content": "Old history"}]
    with usage_scope(context.user_data):
        record_model_usage(
            provider="gemini", model="gemini-3-flash-preview", operation="chat",
            usage={"input_tokens": 100, "output_tokens": 20},
        )
    await end_session(context, chat_id=456, user_id=123)
    context.job.data = context.job_queue.run_once.call_args.kwargs["data"]
    entered = asyncio.Event()
    release = Event()
    loop = asyncio.get_running_loop()

    def synthesize(*args, **kwargs):
        """Pause at the model boundary to reproduce the race without sleeps."""
        loop.call_soon_threadsafe(entered.set)
        if not release.wait(timeout=5):
            raise TimeoutError("Test did not release synthesis")
        record_model_usage(
            provider="gemini", model="gemini-3-flash-preview", operation="synthesis",
            usage={"input_tokens": 300, "output_tokens": 50},
        )
        if synthesis_fails:
            raise ValueError("Simulated response parsing failure")
        return SimpleNamespace(content="Old session notes")

    def start_new_session():
        """Create real replacement state whose usage and history must survive."""
        new_id = start_session(context)
        context.user_data["chat_history"] = [{"role": "user", "content": "New history"}]
        context.user_data["cached_events"] = [{"summary": "New event"}]
        context.user_data["calendar_cache_metadata"] = {"scope": "new"}
        with usage_scope(context.user_data):
            record_model_usage(
                provider="gemini", model="gemini-3-flash-preview", operation="chat",
                usage={"input_tokens": 75, "output_tokens": 10},
            )
        return new_id, deepcopy(context.user_data)

    with (
        patch("orchestrator.session_manager.generate_session_synthesis", side_effect=synthesize),
        patch("orchestrator.session_manager.prompt_next_trigger", new_callable=AsyncMock) as prompt,
    ):
        if new_session_timing == "before_job":
            new_id, newer_state = start_new_session()
        task = asyncio.create_task(execute_synthesis_task(context))
        try:
            await asyncio.wait_for(entered.wait(), timeout=5)
            if new_session_timing == "during_job":
                new_id, newer_state = start_new_session()
        finally:
            release.set()
            await task

    # Use real SQLite to verify first-write table creation and the stored owner,
    # not just a mocked upsert call that could hide cross-session corruption.
    summary = json.loads(get_db()["session_usage"].get(old_id)["summary_json"])
    assert summary["session_id"] == old_id
    assert summary["reported_tokens"]["input_tokens"] == 400
    assert summary["reported_tokens"]["output_tokens"] == 70
    assert summary["history"] == {"messages": 1, "characters": 11}
    assert context.user_data["current_session_id"] == new_id
    assert context.user_data["session_state"] == SessionStatus.ACTIVE
    for field in ("session_usage", "chat_history", "cached_events", "calendar_cache_metadata", "last_model_usage"):
        # Finishing the old job must preserve all state owned by the newer one.
        assert context.user_data[field] == newer_state[field]
    assert get_db()["sessions"].get(new_id)["status"] == SessionStatus.ACTIVE.value
    prompt.assert_not_awaited()


@pytest.mark.asyncio
async def test_usage_follows_worker_threads_and_stays_with_its_session():
    """Parallel sessions cannot contaminate one another's request totals."""
    first = {"current_session_id": "first"}
    second = {"current_session_id": "second"}

    async def call(data, tokens):
        """Exercise the same thread propagation used by chat and reviews."""
        with usage_scope(data):
            await asyncio.to_thread(
                record_model_usage, provider="gemini", model="gemini-3-flash-preview",
                operation="chat", usage={"input_tokens": tokens, "output_tokens": 20},
                history=[{"role": "user", "content": "Hello"}],
            )

    await asyncio.gather(call(first, 100), call(second, 200))
    first_summary = finish_session_usage(first, "first", [{"role": "user", "content": "Hello"}])
    second_summary = finish_session_usage(second, "second", [])
    assert first_summary["reported_tokens"]["input_tokens"] == 100
    assert second_summary["reported_tokens"]["input_tokens"] == 200
    assert first_summary["history"] == {"messages": 1, "characters": 5}


def test_cumulative_usage_is_distinct_from_window_occupancy(monkeypatch):
    """Repeated inputs add to cost totals but do not consume a growing context window."""
    # Exercise combined windows without depending on a provider absent from this PR.
    monkeypatch.setitem(
        MODEL_CONTEXT_LIMITS,
        ("test-provider", "combined-model"), ("input_and_output", 1_050_000),
    )
    data = {"current_session_id": "session"}
    with usage_scope(data):
        for _ in range(2):
            # Cached input and reasoning output are subsets, never added twice.
            record_model_usage(
                provider="test-provider", model="combined-model", operation="chat",
                usage={"input_tokens": 1000, "output_tokens": 50, "cached_tokens": 500, "reasoning_tokens": 30},
                history=[{"content": "Discuss only"}],
            )
    summary = finish_session_usage(data, "session", [{"content": "Discuss only"}])
    bucket = next(iter(summary["models"].values()))
    assert summary["reported_tokens"]["input_tokens"] == 2000
    assert summary["reported_tokens"]["output_tokens"] == 100
    assert bucket["peak_input_tokens"] == 1000
    assert bucket["peak_capacity_percent"] == 0.1
    assert bucket["last_request"]["history_truncated"] is False


def test_unknown_usage_and_unknown_model_limits_are_visible():
    """Unavailable provider metadata must not look like zero occupancy."""
    data = {"current_session_id": "session"}
    with usage_scope(data):
        record_model_usage(provider="gemini", model="custom-model", operation="chat", usage={})
    report = format_context_usage(data)
    assert "unknown input tokens" in report
    assert "limit unknown" in report
    assert "Usage incomplete for 1 request" in report
    assert "0.00%" not in report


def test_gemini_thinking_counts_and_input_limit_are_separate():
    """Gemini's published input capacity should not include its output tokens."""
    usage = gemini_token_usage(SimpleNamespace(usage_metadata=SimpleNamespace(
        prompt_token_count=1_048_576, candidates_token_count=100, thoughts_token_count=50,
        cached_content_token_count=200,
    )))
    assert usage == {"input_tokens": 1_048_576, "output_tokens": 150, "reasoning_tokens": 50, "cached_tokens": 200}
    data = {"current_session_id": "session"}
    with usage_scope(data):
        record_model_usage(provider="gemini", model="gemini-3-flash-preview", operation="chat", usage=usage)
    assert "100.00% input capacity" in format_context_usage(data)
    assert all(count is None for count in gemini_token_usage(None).values())


@pytest.mark.asyncio
@pytest.mark.parametrize("previous_session", [False, True])
async def test_context_shows_standalone_review_usage(previous_session):
    """A real review call must reach /context even when no chat session owns it."""
    update = MagicMock()
    update.effective_user.id = 123
    update.effective_chat.id = 456
    update.message.reply_text = AsyncMock()
    context = MagicMock()
    context.bot_data = {"allowed_user_id": 123}
    context.user_data = {}
    if previous_session:
        context.user_data["current_session_id"] = "previous"
        with usage_scope(context.user_data):
            record_model_usage(
                provider="gemini", model="gemini-3-flash-preview", operation="chat",
                usage={"input_tokens": 100, "output_tokens": 20},
            )
        finish_session_usage(context.user_data, "previous", [])
        context.user_data.pop("current_session_id")
    previous_summary = deepcopy(context.user_data.get("last_session_usage"))

    with (
        patch("orchestrator.review_manager._read_context_markdown", return_value="# Context"),
        patch("orchestrator.review_manager.get_past_events", return_value=[]),
        patch("orchestrator.review_manager.get_upcoming_events", return_value=[]),
        patch("orchestrator.review_manager.save_review_workflow_sync"),
        patch("orchestrator.review_manager.genai.Client") as client,
        patch("bot.handlers.send_review_stage_gate", new_callable=AsyncMock) as gate,
    ):
        # Keep command routing, worker-thread generation, and usage attribution
        # real; replace only external provider, persistence, and Telegram I/O.
        client.return_value.models.generate_content.return_value = SimpleNamespace(
            parsed=WeekReviewResponse(summary="Review ready."),
            usage_metadata=SimpleNamespace(
                prompt_token_count=1200, candidates_token_count=100, thoughts_token_count=50,
            ),
        )
        await weekly_review_command(update, context)
        client.return_value.models.generate_content.assert_called_once()
        gate.assert_awaited_once()

    update.message.reply_text.reset_mock()
    await context_command(update, context)
    report = update.message.reply_text.await_args.args[0]
    assert report.startswith("Latest model request outside a chat session")
    assert "review:week_review · gemini-3-flash-preview" in report
    assert "1,200 input tokens" in report
    assert "0.11% input capacity" in report
    assert "Output: 150 tokens (including thinking)." in report
    assert "No model context measurements" not in report
    assert not context.user_data.get("current_session_id")
    assert "session_usage" not in context.user_data
    assert context.user_data.get("last_session_usage") == previous_summary
    if previous_session:
        assert report.index("review:week_review") < report.index("Last completed session")


def test_new_chat_request_replaces_standalone_review_display():
    """Resuming conversation must not keep an older review as the latest request."""
    data = {}
    with usage_scope(data):
        record_model_usage(
            provider="gemini", model="custom-review", operation="review:week_review", usage={},
        )
        report = format_context_usage(data)
        assert "unknown input tokens" in report
        assert "Output: unknown tokens" in report
        data["current_session_id"] = "new-session"
        record_model_usage(
            provider="gemini", model="gemini-3-flash-preview", operation="chat",
            usage={"input_tokens": 200, "output_tokens": 30},
        )
    report = format_context_usage(data)
    assert report.startswith("Current session")
    assert "200 input tokens" in report
    assert "custom-review" not in report


@pytest.mark.asyncio
async def test_context_command_reads_metrics_without_routing_or_history_changes():
    """The indicator is deterministic and cannot create a conversation or draft."""
    update = MagicMock()
    update.effective_user.id = 123
    update.message.reply_text = AsyncMock()
    context = MagicMock()
    context.bot_data = {"allowed_user_id": 123}
    context.user_data = {"chat_history": [{"role": "user", "content": "Keep me"}]}
    with patch("bot.handlers.process_message") as process:
        await context_command(update, context)
    process.assert_not_called()
    assert "No model context measurements" in update.message.reply_text.await_args.args[0]
    assert context.user_data["chat_history"] == [{"role": "user", "content": "Keep me"}]


@pytest.mark.asyncio
@patch("orchestrator.session_manager.prompt_next_trigger", new_callable=AsyncMock)
@patch("orchestrator.session_manager.append_to_decision_log")
@patch("orchestrator.session_manager.persist_decision")
@patch("orchestrator.session_manager.get_db")
async def test_final_summary_includes_synthesis_and_survives_history_clear(mock_db, mock_persist, mock_append, mock_trigger):
    """Session finalization must persist the final model call before resetting state."""
    from orchestrator.session_manager import execute_synthesis_task
    context = MagicMock()
    context.user_data = {"current_session_id": "session", "chat_history": [{"content": "Hello"}]}
    context.job.data = {"session_id": "session", "chat_id": 456, "chat_history": [{"content": "Hello"}]}
    context.bot.send_message = AsyncMock()

    def synthesize(*args, **kwargs):
        """Record inside the worker thread, as the real Gemini client does."""
        record_model_usage(provider="gemini", model="gemini-3-flash-preview", operation="synthesis",
                           usage={"input_tokens": 300, "output_tokens": 50})
        return SimpleNamespace(content="Session notes")

    with patch("orchestrator.session_manager.generate_session_synthesis", side_effect=synthesize):
        await execute_synthesis_task(context)
    saved = json.loads(mock_db.return_value["session_usage"].upsert.call_args.args[0]["summary_json"])
    assert saved["reported_tokens"]["input_tokens"] == 300
    assert saved["reported_tokens"]["output_tokens"] == 50
    assert saved["history"] == {"messages": 1, "characters": 5}
    assert context.user_data["chat_history"] == []
    assert "Last completed session" in format_context_usage(context.user_data)
    assert "synthesis" in format_context_usage(context.user_data)
