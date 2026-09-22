import io
import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from orchestrator.router import process_message
from reasoning.routing import RoutingDecision, route_turn, operation_schema, valid_operation
from reasoning.flash_client import FlashResponse
from reasoning.model_client import ModelReply, generate_structured


def decision(**overrides) -> RoutingDecision:
    """Builds explicit classifier outputs for deterministic orchestration tests."""
    return RoutingDecision(**{
        "operation": "discuss",
        "target_id": None,
        **overrides,
    })


@patch("reasoning.routing.generate_structured")
def test_classifier_receives_original_message_history_and_pending_work(mock_client, monkeypatch):
    """Ambiguous follow-ups must be classified with their conversational referent."""
    monkeypatch.setenv("DAVID_ROUTING_MODEL", "openai/gpt-5.6-luna")
    expected = decision(target_id="draft_1")
    mock_client.return_value = SimpleNamespace(
        text=expected.model_dump_json(), finish_reason="stop", serving_provider="OpenAI",
        latency_ms=20, input_tokens=100, output_tokens=20, cost_usd=0.0001,
    )
    # An old, long qualification must survive both former truncation points.
    history = [{"role": "user", "content": "Discuss only until I ask. " + "x" * 3000}]
    history += [{"role": "assistant", "content": f"Discussion message {index}"} for index in range(20)]
    state = {"drafts": [{"id": "draft_1", "status": "pending", "summary": "Focus 9–11"}]}

    result = route_turn("Actually, is there a better way to do this?", history, state)

    assert result == expected
    kwargs = mock_client.call_args.kwargs
    assert kwargs["model"] == "openai/gpt-5.6-luna"
    assert kwargs["reasoning"] == "low"
    assert kwargs["allow_provider_fallbacks"] is True
    assert "endpoint_provider" not in kwargs
    assert "max_input_price" not in kwargs
    assert kwargs["schema"] == operation_schema(state)
    assert kwargs["payload"]["conversation"] == history
    assert kwargs["payload"]["state"] == state
    assert kwargs["payload"]["message"] == "Actually, is there a better way to do this?"


@patch("reasoning.routing.capture_sentry_exception")
@patch("reasoning.routing.generate_structured", side_effect=TimeoutError("Timed out"))
def test_classifier_failure_does_not_guess_a_workflow_action(mock_client, mock_capture):
    """An unavailable classifier must leave the pending workflow untouched."""
    with pytest.raises(ValueError, match="couldn't interpret"):
        route_turn("Change it", [], {"drafts": []})
    mock_capture.assert_called_once()


def test_production_transport_preserves_schema_and_timeout_without_benchmark_pins(monkeypatch):
    """Runtime requests permit schema-capable provider fallback for the same model."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    envelope = {"choices": [{"message": {"content": decision().model_dump_json()}, "finish_reason": "stop"}]}
    schema = operation_schema({"drafts": []})
    with patch("reasoning.model_client.urlopen", return_value=io.BytesIO(json.dumps(envelope).encode())) as send:
        generate_structured(
            provider="openrouter", model="openai/gpt-5.6-luna", instruction="Route",
            payload={"message": "Hello"}, schema=schema, timeout=10, allow_provider_fallbacks=True,
        )
    body = json.loads(send.call_args.args[0].data)
    assert body["provider"] == {"require_parameters": True, "allow_fallbacks": True}
    assert body["response_format"]["json_schema"]["schema"] == schema
    assert body["reasoning"] == {"effort": "low", "exclude": True}
    assert send.call_args.kwargs["timeout"] == 10
    assert "tools" not in body


@patch("reasoning.model_client.genai.Client")
def test_native_gemini_routing_preserves_schema_reasoning_and_usage(mock_client, monkeypatch):
    """The native adapter must honor routing settings and include thinking usage."""
    from google.genai import types
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    client = mock_client.return_value.__enter__.return_value
    client.models.generate_content.return_value = types.GenerateContentResponse(
        candidates=[types.Candidate(
            finish_reason="STOP", content=types.Content(parts=[types.Part(text=decision().model_dump_json())]),
        )],
        usage_metadata=types.GenerateContentResponseUsageMetadata(
            prompt_token_count=150, candidates_token_count=20, thoughts_token_count=10,
            cached_content_token_count=50,
        ),
        response_id="gemini-request",
    )
    schema = operation_schema({"drafts": []})
    with patch("reasoning.model_client.urlopen") as openrouter:
        reply = generate_structured(
            provider="gemini", model="gemini-3.5-flash-lite", instruction="Route",
            payload={"message": "Hello"}, schema=schema, reasoning="low", timeout=10,
        )
    openrouter.assert_not_called()
    client_args = mock_client.call_args.kwargs
    assert client_args["vertexai"] is False
    assert client_args["http_options"].timeout == 10000
    assert client_args["http_options"].retry_options.attempts == 1
    request = client.models.generate_content.call_args.kwargs
    assert request["model"] == "gemini-3.5-flash-lite"
    assert request["config"].response_json_schema == schema
    assert request["config"].thinking_config.thinking_level.value == "LOW"
    assert reply.finish_reason == "stop"
    assert reply.input_tokens == 150
    assert reply.output_tokens == 30
    assert reply.reasoning_tokens == 10
    assert reply.cached_tokens == 50


@pytest.mark.parametrize(
    "action", ["discuss", "clarify"],
)
def test_discussion_and_navigation_cannot_authorize_proposals(action):
    """Discussion operations cannot turn a response into a calendar draft."""
    assert not decision(operation=action).allows_calendar_proposals


@pytest.mark.parametrize(
    "prediction, expected",
    [
        ({"operation": "revise_draft", "target_id": "live"}, True),
        ({"operation": "revise_draft", "target_id": "locked"}, False),
        ({"operation": "revise_draft", "target_id": "resolved"}, False),
        ({"operation": "revise_draft", "target_id": None}, False),
        ({"operation": "discuss", "target_id": "invented"}, False),
        ({"operation": "create_draft", "target_id": "live"}, False),
        ({"operation": "create_draft", "target_id": None}, False),
        ({"operation": "clarify", "target_id": None}, True),
    ],
)
def test_application_state_constrains_operations_and_targets(prediction, expected):
    """Valid JSON still needs permission and target checks before dispatch."""
    state = {"can_create_draft": False, "drafts": [
        {"id": "live", "status": "pending"},
        {"id": "locked", "status": "pending", "can_revise": False},
        {"id": "resolved", "status": "accepted"},
    ]}
    assert valid_operation(prediction, state) is expected
    assert "create_draft" not in operation_schema(state)["properties"]["operation"]["enum"]


@pytest.mark.parametrize("text, finish_reason", [
    ('{"operation":"revise_draft","target_id":"invented"}', "stop"),
    ('{"operation":"discuss","target_id":null,"extra":"ignored?"}', "stop"),
    ('{"operation":"discuss","target_id":null}', "length"),
    ("not JSON", "stop"),
])
@patch("reasoning.routing.capture_sentry_exception")
@patch("reasoning.routing.generate_structured")
def test_invalid_or_incomplete_routing_fails_closed(mock_client, mock_capture, text, finish_reason):
    """Never dispatch partial output or repair a malformed operation by guessing."""
    mock_client.return_value = SimpleNamespace(text=text, finish_reason=finish_reason)
    with pytest.raises(ValueError, match="couldn't interpret"):
        route_turn("Change it", [], {"drafts": [{"id": "live", "status": "pending"}]})


@pytest.mark.parametrize("state, target, expected", [
    ({"drafts": []}, None, "clarify"),
    ({"drafts": []}, "stale", "clarify"),
    ({"drafts": [{"id": "draft_1", "status": "pending", "can_revise": False}]}, "draft_1", "clarify"),
    ({"drafts": [{"id": "draft_1", "status": "pending"}]}, "draft_1", "revise_draft"),
])
@patch("reasoning.routing.generate_structured")
def test_revision_without_editable_work_becomes_clarification(mock_client, state, target, expected):
    """Application state determines whether a model-selected revision can proceed."""
    mock_client.return_value = ModelReply(
        text=decision(operation="revise_draft", target_id=target).model_dump_json(),
        model="test", latency_ms=1, input_tokens=50, output_tokens=10, finish_reason="stop",
    )

    result = route_turn("Move that to tomorrow", [], state)

    assert result.operation == expected
    assert result.target_id == (target if expected == "revise_draft" else None)
    assert valid_operation(result.model_dump(), state)
    mock_client.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "route, allows",
    [
        (decision(operation="create_draft"), True),
        (decision(), False),
        (decision(operation="clarify"), False),
    ],
)
@patch("orchestrator.router.generate_flash_response", return_value=FlashResponse(message="Response"))
@patch("orchestrator.router.build_context", return_value="<CONTEXT>")
async def test_process_message_applies_routing_and_preserves_real_history(
    mock_build, mock_generate, route, allows,
):
    """The route controls schema and context without becoming conversational memory."""
    context = MagicMock()
    context.user_data = {"chat_history": [{"role": "assistant", "content": "Previous draft"}]}
    await process_message(
        "My actual words", context, routing_decision=route,
        routing_state={"drafts": []}, workflow_context="Internal draft state",
    )
    mock_build.assert_called_once_with(context, profile="full")
    kwargs = mock_generate.call_args.kwargs
    assert kwargs["allow_calendar_proposals"] is allows
    assert kwargs["user_message"] == "My actual words"
    assert "Internal draft state" in kwargs["context_block"]
    assert context.user_data["chat_history"][-2:] == [
        {"role": "user", "content": "My actual words"},
        {"role": "assistant", "content": "Response"},
    ]
    assert "Internal draft state" not in str(context.user_data["chat_history"])


@pytest.mark.asyncio
@patch("orchestrator.router.generate_flash_response", return_value=FlashResponse(message="Response"))
@patch("orchestrator.router.build_context", return_value="<CONTEXT>")
@patch("reasoning.routing.generate_structured")
async def test_response_generation_never_calls_classifier(
    mock_classify, mock_build, mock_generate,
):
    """A handler-authorized revision uses the same decision through generation."""
    context = MagicMock()
    context.user_data = {}
    await process_message(
        "Move to 3pm", context,
        routing_decision=decision(operation="revise_draft", target_id="draft_1"),
        routing_state={"drafts": [{"id": "draft_1", "status": "pending"}]},
    )
    mock_classify.assert_not_called()
    assert mock_generate.call_args.kwargs["allow_calendar_proposals"] is True


@pytest.mark.asyncio
@patch("orchestrator.router.capture_sentry_exception")
@patch("orchestrator.router.build_context", side_effect=RuntimeError("Calendar failed"))
async def test_process_message_failure_preserves_history(mock_build, mock_capture):
    """Failed answers must not become evidence in session synthesis."""
    context = MagicMock()
    context.user_data = {"chat_history": []}
    with pytest.raises(RuntimeError, match="Calendar failed"):
        await process_message("Free at 2?", context, routing_decision=decision(), routing_state={"drafts": []})
    assert context.user_data["chat_history"] == []
    assert mock_capture.call_args.kwargs["tags"] == {"operation": "discuss"}
