# Chat model comparison research

Verified 2026-09-22. Scope: a live comparison for David, using the installed `google-genai` 1.68.0 SDK and the remaining $0.9305594 of the previously approved $0.95 evaluation budget. No paid calls were made for this research.

## Candidate and settings

Use `gemini-3.8-flash` as the first candidate against the current `gemini-3-flash-preview`. Google identifies 3.8 Flash as generally available; this makes it a relevant replacement candidate, not evidence that it will produce better advice for David. [Google migration guide](https://ai.google.dev/gemini-api/docs/latest-model)

| Model | Standard text input / million tokens | Output / million tokens, including thinking | Default thinking | Input / output limit |
| --- | ---: | ---: | --- | --- |
| `gemini-3-flash-preview` | $0.50 | $3.00 | High | 1,048,576 / 65,536 |
| `gemini-3.8-flash` | $0.75 | $3.75 | Medium | 1,048,576 / 65,536 |
| `gemini-3.1-pro-preview` | $2.00 at ≤200k input; $4.00 above | $12.00 at ≤200k input; $18.00 above | High | 1,048,576 / 65,536 |

3.8 Flash's rates above are introductory through December 31, 2026; on January 1, 2027 its input/output rates become $1.50/$7.50. The Pro alternative is substantially more expensive. [Pricing](https://ai.google.dev/gemini-api/docs/pricing), [3 Flash limits](https://ai.google.dev/gemini-api/docs/models/gemini-3-flash-preview), [3.8 Flash limits](https://ai.google.dev/gemini-api/docs/models/gemini-3.8-flash), [3.1 Pro limits](https://ai.google.dev/gemini-api/docs/models/gemini-3.1-pro-preview), [thinking defaults](https://ai.google.dev/gemini-api/docs/thinking)

For a matched experiment, set thinking to `HIGH` for both chat models, keep temperature 1, and run both current and improved prompts. This matches the current model's documented default while removing thinking-level configuration as a confound. Both support high thinking. Keeping each model's default is also a valid drop-in configuration comparison, but it compares medium versus high as well as model weights. This is an experimental-design recommendation, not a provider requirement. [Thinking controls](https://ai.google.dev/gemini-api/docs/thinking)

## CountTokens is not an exact-budget shortcut in this SDK

The REST `countTokens` API accepts `generateContentRequest`, including system instructions and other steering information. However, the public `Models.count_tokens` method in SDK 1.68.0 only accepts `model`, `contents`, and `CountTokensConfig`; its Gemini Developer API conversion explicitly rejects `system_instruction`, `tools`, and `generation_config`. Passing the production system prompt/schema through that public configuration therefore fails before a network call. [REST reference](https://ai.google.dev/api/tokens), [installed conversion](../../.venv/lib/python3.13/site-packages/google/genai/models.py), [SDK v1.68.0 source](https://github.com/googleapis/python-genai/blob/v1.68.0/google/genai/models.py)

A direct REST call using the fully serialized generation request can include those fields. This is preferable to counting only user text for an estimate, but the cited reference does not promise that its count is a billing ceiling. Its simple text example returns 10 from counting and 11 prompt tokens from generation. Do not treat the estimate as a mathematically exact reservation without a provider guarantee. This distinction matters because this task has a hard spend limit. [REST token-count example and request contract](https://ai.google.dev/api/tokens)

## Conservative budget option

The existing runner reserves the entire published input limit plus the output limit for every actual request. This avoids undercounting instructions or JSON schema. At full output capacity, a 3.8 Flash reservation is $1.032192, exceeding the remaining budget. Formula: `(1,048,576 × 0.75 + 65,536 × 3.75) / 1,000,000`. These are conservative reservations, not expected charges. [Runner](../../scripts/eval_assistant_live.py), [pricing](https://ai.google.dev/gemini-api/docs/pricing)

The simplest compatible experiment is an explicit, equal `max_output_tokens=8192` cap on both chat models. Full-input reservations become $0.817152 for 3.8 Flash and $0.548864 for 3 Flash. Admit each request only if its reservation plus prior accounted spend fits the remaining approved budget. Replace the reservation with reported usage after success; retain it and stop on unknown usage or errors. If the budget cannot admit the next request, report the experiment incomplete rather than rerunning or exceeding the limit.

This cap is a change from David's uncapped chat generation and must appear in the experiment manifest. Google documents that the cap includes both thinking and visible output and can truncate a response. Require a successful completion reason, and classify cap exhaustion as an incomplete generation rather than a normal advice score. Do not claim this proves behavior under an uncapped production configuration. [Thinking token limits](https://ai.google.dev/gemini-api/docs/thinking)

Use real model responses, retain transcripts and separate structural checks from qualitative judgments. Paired inputs, identical routing decisions, prompt hashes, model IDs, thinking settings, output limits, usage, and finish reasons should be recorded with each comparison. These are evaluation-design recommendations; a small comparison does not establish a general model ranking.
