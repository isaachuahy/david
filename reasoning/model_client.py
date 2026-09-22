"""Structured-output transport for native Gemini and OpenRouter routing.

These calls return data only. They cannot reach David's calendar, memory, or
Telegram handlers. Callers validate returned data before applying any operation.
"""

import json
import os
from dataclasses import dataclass
from time import perf_counter
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from google import genai
from google.genai import errors, types

from observability.context_usage import gemini_token_usage

KEY_NAMES = {"openrouter": "OPENROUTER_API_KEY"}
ENDPOINT = "https://openrouter.ai/api/v1/chat/completions"


@dataclass(frozen=True)
class ModelReply:
    """Preserves output and billable usage even when the output fails validation."""

    text: str
    model: str
    latency_ms: float
    input_tokens: int | None
    output_tokens: int | None
    cached_tokens: int | None = 0
    reasoning_tokens: int | None = 0
    finish_reason: str = ""
    serving_provider: str = ""
    request_id: str = ""
    cost_usd: float | None = None


class ModelCallError(RuntimeError):
    """Reports a provider failure without exposing credentials or request bodies."""


class ModelAccessError(ModelCallError):
    """Signals that the shared key or account cannot be used."""


def generate_structured(
    *, provider: str, model: str, instruction: str, payload: dict, schema: dict,
    reasoning: str = "low", max_output_tokens: int = 1024, timeout: float = 30,
    max_price: dict | None = None, endpoint_provider: str | None = None,
    allow_provider_fallbacks: bool = False,
) -> ModelReply:
    """Makes one bounded request; retries belong to the caller's budget policy."""
    if provider == "gemini":
        return _generate_gemini(
            model=model, instruction=instruction, payload=payload, schema=schema,
            reasoning=reasoning, max_output_tokens=max_output_tokens, timeout=timeout,
        )
    if provider != "openrouter":
        raise ValueError(f"Unsupported structured model provider: {provider}")
    key = os.environ.get(KEY_NAMES[provider], "").strip()
    if not key:
        raise ModelCallError(f"Missing {KEY_NAMES[provider]}")
    content = json.dumps(payload, ensure_ascii=False)
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {key}"}
    preferences = {"require_parameters": True, "allow_fallbacks": allow_provider_fallbacks}
    if not allow_provider_fallbacks:
        # Experiments keep their price-sorted, single-provider policy. Production
        # may use another schema-capable endpoint for the same requested model.
        preferences["sort"] = "price"
    if endpoint_provider:
        # Pin the serving endpoint when diagnosing availability; inference still
        # goes through OpenRouter with the same schema and spending limits.
        preferences["only"] = [endpoint_provider]
    if max_price is not None:
        preferences["max_price"] = max_price
    body = {
        "model": model, "stream": False, "max_tokens": max_output_tokens,
        "messages": [{"role": "system", "content": instruction}, {"role": "user", "content": content}],
        "reasoning": {"effort": reasoning, "exclude": True},
        "provider": preferences,
        "response_format": {"type": "json_schema", "json_schema": {
            "name": "routing_result", "strict": True, "schema": schema,
        }},
    }

    request = Request(ENDPOINT, data=json.dumps(body).encode("utf-8"), headers=headers, method="POST")
    started = perf_counter()
    try:
        with urlopen(request, timeout=timeout) as response:
            raw = json.load(response)
    except HTTPError as error:
        # Provider error bodies can echo input or credentials. Record only the
        # status; the benchmark retains its cost reservation for uncertain calls.
        error_type = ModelAccessError if error.code in {401, 402, 403} else ModelCallError
        raise error_type(f"{provider} HTTP {error.code}") from None
    except (URLError, TimeoutError, OSError, ValueError):
        raise ModelCallError(f"{provider} transport or response error") from None
    latency = (perf_counter() - started) * 1000
    try:
        return parse_provider_reply(model, raw, latency)
    except (AttributeError, IndexError, KeyError, TypeError, ValueError):
        raise ModelCallError("openrouter invalid response envelope") from None


def _generate_gemini(
    *, model: str, instruction: str, payload: dict, schema: dict,
    reasoning: str, max_output_tokens: int, timeout: float,
) -> ModelReply:
    """Use Google's existing SDK directly with the same routing schema contract."""
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not key:
        raise ModelAccessError("Missing GEMINI_API_KEY")
    started = perf_counter()
    try:
        # Explicit Developer API selection prevents local Vertex environment
        # settings from changing where production routing sends the request.
        with genai.Client(api_key=key, vertexai=False, http_options=types.HttpOptions(
            timeout=int(timeout * 1000), retry_options=types.HttpRetryOptions(attempts=1),
        )) as client:
            response = client.models.generate_content(
                model=model, contents=json.dumps(payload, ensure_ascii=False),
                config=types.GenerateContentConfig(
                    system_instruction=instruction, response_mime_type="application/json",
                    response_json_schema=schema, max_output_tokens=max_output_tokens,
                    thinking_config=types.ThinkingConfig(thinking_level=reasoning.upper()),
                ),
            )
    except errors.APIError as error:
        error_type = ModelAccessError if error.code in {401, 403} else ModelCallError
        raise error_type(f"gemini HTTP {error.code}") from None
    except Exception:
        # SDK errors may contain request content. Keep transport failures concise.
        raise ModelCallError("gemini transport or response error") from None
    candidate = response.candidates[0] if response.candidates else None
    finish = getattr(candidate, "finish_reason", None)
    finish = getattr(finish, "value", finish) or ""
    return ModelReply(
        text=response.text or "", model=model, latency_ms=(perf_counter() - started) * 1000,
        **gemini_token_usage(response), finish_reason=str(finish).lower(),
        serving_provider="Google Gemini API", request_id=response.response_id or "",
    )


def parse_provider_reply(model: str, raw: dict, latency_ms: float) -> ModelReply:
    """Keeps final text and accounting; completion tokens already include reasoning."""
    if raw.get("error"):
        # A provider can fail inside an HTTP 200 envelope. Keep its numeric code
        # for diagnosis, without logging free-form messages that may echo input.
        error = raw["error"]
        code = error.get("code") if isinstance(error, dict) else None
        code = code if isinstance(code, int) else "unknown"
        error_type = ModelAccessError if code in {401, 402, 403} else ModelCallError
        raise error_type(f"openrouter response error {code}")
    usage = raw.get("usage") or {}
    choice = raw["choices"][0]
    content = choice["message"].get("content") or ""
    if not isinstance(content, str):
        raise ValueError("Expected text content.")
    return ModelReply(
        text=content, model=raw.get("model", model), latency_ms=latency_ms,
        input_tokens=usage.get("prompt_tokens"), output_tokens=usage.get("completion_tokens"),
        cached_tokens=(usage.get("prompt_tokens_details") or {}).get("cached_tokens", 0) or 0,
        reasoning_tokens=(usage.get("completion_tokens_details") or {}).get("reasoning_tokens", 0) or 0,
        finish_reason=choice.get("finish_reason", ""), serving_provider=raw.get("provider", ""),
        request_id=raw.get("id", ""), cost_usd=usage.get("cost"),
    )
