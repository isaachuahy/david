"""Offline checks for benchmark scoring, isolation, provider contracts, and spend bounds."""

import argparse
import io
import json
from dataclasses import replace
from unittest.mock import patch
from urllib.error import HTTPError

import pytest

from reasoning.model_client import ModelAccessError, ModelCallError, ModelReply, generate_structured, parse_provider_reply
from scripts.benchmark_routing import (
    ROOT, MODELS, Budget, BudgetExhausted, accept_candidate, allowed_operations,
    call_with_budget, case_payload, improvement_payload, load_cases, operation_schema,
    estimate_cost, run, score_prediction,
)


def reply(text='{"operation":"discuss","target_id":null}'):
    """Returns explicit usage so cost accounting can be tested without live calls."""
    return ModelReply(text, "test-model", 25, 100, 20, finish_reason="stop")


def test_dataset_is_valid_and_labels_are_not_sent_to_tested_models():
    """Every frozen expected answer must be legal under its supplied state."""
    data = load_cases(ROOT / "evals/routing_cases.json")
    assert len(data["cases"]) == 48
    for case in data["cases"]:
        # Check each fixture through the same input builder used for live calls.
        payload = case_payload(case, data["contexts"])
        assert set(payload) == {"message", "conversation", "state", "allowed_operations"}
        assert score_prediction(json.dumps(case["expected"]), case["expected"], payload["state"])["exact"]


@pytest.mark.parametrize("prediction", ["{}", "[]", '"discuss"', '{"operation":[],"target_id":null}', "not json"])
def test_malformed_outputs_fail_without_crashing(prediction):
    """Bad model output remains a scored attempt rather than disappearing from results."""
    score = score_prediction(prediction, {"operation": "discuss", "target_id": None}, {"drafts": []})
    assert not score["valid"]
    assert not score["exact"]


def test_state_constraints_and_unsafe_legal_actions_are_scored_separately():
    """A wrong revision can be legal; schema compliance is not semantic accuracy."""
    state = {"drafts": [{"id": "draft_a", "status": "pending"}, {"id": "draft_b", "status": "pending"}]}
    expected = {"operation": "revise_draft", "target_id": "draft_a"}
    wrong = score_prediction('{"operation":"revise_draft","target_id":"draft_b"}', expected, state)
    assert wrong["unsafe_allowed"]
    assert wrong["valid"]
    state["can_revise_drafts"] = False
    blocked = score_prediction('{"operation":"revise_draft","target_id":"draft_b"}', expected, state)
    assert blocked["unsafe_suggestion"]
    assert not blocked["unsafe_allowed"]
    assert "revise_draft" not in operation_schema(state)["properties"]["operation"]["enum"]
    assert "pause" not in allowed_operations(state)


def test_budget_reserves_before_calls_and_survives_restart(tmp_path):
    """Uncertain network outcomes must retain their allowance across retries/runs."""
    path = tmp_path / "ledger.json"
    budget = Budget(.01, path)
    ticket = budget.reserve(.009, "uncertain-call")
    reopened = Budget(.01, path)
    with pytest.raises(BudgetExhausted):
        reopened.reserve(.002, "would-exceed")
    budget.settle(ticket, None)
    assert budget.used == .009
    budget.settle(ticket, .001)
    assert budget.used == pytest.approx(.00125)
    assert Budget(.01, path).used == budget.used


def test_no_request_is_dispatched_when_budget_cannot_cover_it(tmp_path):
    """The spending guard runs before invoking the provider adapter."""
    with patch("reasoning.model_client.generate_structured") as caller:
        with pytest.raises(BudgetExhausted):
            call_with_budget(MODELS["grok"], "instruction", {}, {}, Budget(.000001, tmp_path / "ledger.json"), "case", 1024, caller)
        caller.assert_not_called()


def test_development_spending_preserves_holdout_allowance(tmp_path):
    """Prompt optimization cannot spend the allowance set aside for held-out calls."""
    budget = Budget(.01, tmp_path / "ledger.json")
    with pytest.raises(BudgetExhausted):
        budget.reserve(.008, "development", keep_usd=.003)
    assert budget.used == 0


def test_development_feedback_never_contains_holdout_cases():
    """Optimizer input is restricted even if the caller accidentally mixes results."""
    data = load_cases(ROOT / "evals/routing_cases.json")
    rows = [{"case_id": "test01", "split": "holdout", "exact": False, "error": None}]
    assert improvement_payload(data, rows, "instruction")["development_failures"] == []


def test_incomplete_or_less_safe_candidate_is_rejected():
    """Partial coverage and average gains cannot conceal worse unsafe routing."""
    baseline = {"model": {"complete": True, "unsafe_allowed": 0, "unsafe_suggestions": 0, "api_errors": 0, "exact": 5}}
    candidate = {"model": {**baseline["model"], "exact": 6, "unsafe_allowed": 1}}
    assert not accept_candidate(baseline, candidate, "old", "new")
    candidate["model"].update(unsafe_allowed=0, complete=False)
    assert not accept_candidate(baseline, candidate, "old", "new")
    candidate["model"]["complete"] = True
    assert accept_candidate(baseline, candidate, "old", "new")


def test_provider_usage_counts_reasoning_once():
    """OpenRouter normalizes reasoning usage and exposes the actual serving provider."""
    response = parse_provider_reply("test", {
        "id": "generation-test", "provider": "Google", "model": "google/test",
        "choices": [{"message": {"content": "{}", "reasoning": "private thought"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 100, "completion_tokens": 50,
                  "completion_tokens_details": {"reasoning_tokens": 30}, "cost": .0002},
    }, 10)
    assert response.text == "{}"
    assert response.output_tokens == 50
    assert response.reasoning_tokens == 30
    assert response.serving_provider == "Google"
    assert response.request_id == "generation-test"
    assert estimate_cost(MODELS["gemini-lite"], response) == .0002
    assert estimate_cost(MODELS["gemini-lite"], replace(response, cost_usd=0)) == 0


@pytest.mark.parametrize("endpoint_provider", [None, "baseten/fp8"])
def test_provider_requests_have_schema_and_caps_but_no_tools(monkeypatch, endpoint_provider):
    """OpenRouter must honor schemas and price caps without falling back silently."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-secret")
    schema = operation_schema({"drafts": []})
    envelope = {"choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}]}
    with patch("reasoning.model_client.urlopen", return_value=io.BytesIO(json.dumps(envelope).encode())) as send:
        generate_structured(provider="openrouter", model="test-model", instruction="route",
                            payload={"message": "hello"}, schema=schema, max_output_tokens=512,
                            endpoint_provider=endpoint_provider,
                            max_price={"prompt": .2, "completion": 1.2, "request": 0})
    request = send.call_args.args[0]
    body = json.loads(request.data)
    assert request.full_url == "https://openrouter.ai/api/v1/chat/completions"
    assert "test-secret" not in str(body)
    assert "tools" not in body
    assert body["max_tokens"] == 512
    assert body["response_format"]["json_schema"]["schema"] == schema
    assert body["provider"]["require_parameters"] is True
    assert body["provider"]["allow_fallbacks"] is False
    assert body["provider"]["max_price"]["completion"] == 1.2
    assert body["provider"].get("only") == ([endpoint_provider] if endpoint_provider else None)
    assert body["reasoning"]["exclude"] is True


def test_endpoint_selection_survives_budget_wrapper_and_manifest(tmp_path, monkeypatch):
    """A provider diagnostic must use and record the intended serving endpoint."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    args = argparse.Namespace(cases=ROOT / "evals/routing_cases.json",
                              prompt=ROOT / "evals/routing_prompt.txt", models=["gpt-luna"],
                              live=True, repeats=1, rounds=1, budget_usd=.10,
                              ledger=tmp_path / "budget.json", output=tmp_path / "run",
                              max_output_tokens=1024)
    with patch("reasoning.model_client.generate_structured", return_value=reply()) as caller:
        output = run(args, caller=caller)
    assert caller.call_count == 48
    assert all(call.kwargs["endpoint_provider"] == "openai" for call in caller.call_args_list)
    manifest = json.loads((output / "manifest.json").read_text())
    assert manifest["models"]["gpt-luna"]["endpoint_provider"] == "openai"


def test_provider_error_does_not_echo_credentials(monkeypatch):
    """Raw HTTP errors may echo secrets and must not enter benchmark artifacts."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-secret")
    error = HTTPError("https://openrouter.ai", 401, "test-secret", {}, io.BytesIO(b"test-secret"))
    with patch("reasoning.model_client.urlopen", side_effect=error):
        with pytest.raises(ModelCallError, match="openrouter HTTP 401") as caught:
            generate_structured(provider="openrouter", model="test", instruction="route", payload={}, schema={})
    assert "test-secret" not in str(caught.value)


@pytest.mark.parametrize("envelope", [{}, [], {"error": {"message": "test-secret"}}])
def test_bad_envelopes_fail_without_exposing_provider_messages(monkeypatch, envelope):
    """Malformed successful HTTP responses are operational failures too."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-secret")
    with patch("reasoning.model_client.urlopen", return_value=io.BytesIO(json.dumps(envelope).encode())):
        with pytest.raises(ModelCallError) as caught:
            generate_structured(provider="openrouter", model="test", instruction="route", payload={}, schema={})
    assert "test-secret" not in str(caught.value)


def test_error_envelopes_preserve_codes_and_stop_on_account_failure():
    """HTTP 200 failures need useful diagnostics and the same account stop rule."""
    with pytest.raises(ModelCallError, match="response error 502"):
        parse_provider_reply("test", {"error": {"code": 502, "message": "private"}}, 10)
    with pytest.raises(ModelAccessError, match="response error 401"):
        parse_provider_reply("test", {"error": {"code": 401, "message": "private"}}, 10)


def test_full_loop_refines_on_dev_then_runs_holdout_once(tmp_path, monkeypatch):
    """Exercise the complete optimizer loop with an injected provider, without spending."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    cases = tmp_path / "cases.json"
    fixture = {"contexts": {"empty": {"drafts": []}}, "cases": [
        {"id": "dev", "family": "dev_family", "split": "dev", "context": "empty", "history": [], "message": "Draft a meeting", "expected": {"operation": "create_draft", "target_id": None}},
        {"id": "test", "family": "test_family", "split": "holdout", "context": "empty", "history": [], "message": "Let's brainstorm", "expected": {"operation": "discuss", "target_id": None}},
    ]}
    cases.write_text(json.dumps(fixture))
    prompt = tmp_path / "prompt.txt"
    prompt.write_text("initial instruction")
    calls = []

    def caller(**kwargs):
        """Simulates one development mistake corrected by the candidate instruction."""
        calls.append(kwargs)
        if "development_failures" in kwargs["payload"]:
            assert "Let's brainstorm" not in json.dumps(kwargs["payload"])
            return reply('{"instruction":"improved instruction"}')
        if kwargs["instruction"] == "improved instruction" and kwargs["payload"]["message"] == "Draft a meeting":
            return reply('{"operation":"create_draft","target_id":null}')
        return reply()

    args = argparse.Namespace(cases=cases, prompt=prompt, models=["gemini-lite"], live=True,
                              repeats=1, rounds=2, budget_usd=.10, ledger=tmp_path / "budget.json",
                              output=tmp_path / "run", max_output_tokens=1024)
    output = run(args, caller=caller)
    report = json.loads((output / "report.json").read_text())
    assert len(calls) == 4
    assert report["rounds"][1]["accepted"]
    assert report["selected_prompt"] == "improved instruction"
    assert report["holdout"]["gemini-lite"]["exact"] == 1
    assert report["accounted_usd"] < .10
    assert (output / "attempts.jsonl").exists()


def test_account_failure_stops_all_models_and_holdout(tmp_path, monkeypatch):
    """A shared-key failure must produce diagnostics after exactly one attempted call."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    args = argparse.Namespace(cases=ROOT / "evals/routing_cases.json",
                              prompt=ROOT / "evals/routing_prompt.txt",
                              models=["gemini-lite", "gpt-luna"], live=True,
                              repeats=1, rounds=2, budget_usd=.10,
                              ledger=tmp_path / "budget.json", output=tmp_path / "run",
                              max_output_tokens=1024)
    with patch("reasoning.model_client.generate_structured", side_effect=ModelAccessError("openrouter HTTP 401")) as caller:
        output = run(args, caller=caller)
    caller.assert_called_once()
    report = json.loads((output / "report.json").read_text())
    assert report["stopped_reason"] == "ModelAccessError: openrouter HTTP 401"
    assert all(result["attempted"] == 0 for result in report["holdout"].values())
    assert report["accounted_usd"] > 0
