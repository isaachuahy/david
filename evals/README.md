# Routing benchmark

Compare routing models through **OpenRouter** using 48 synthetic conversations:
30 development cases and 18 held-out cases, separated by scenario family.
Labels are provisional and need product review before release decisions.

## Contract

Each response contains `operation` and `target_id`. Application state limits
the schema to available operations and known drafts; Python validates their
relationship after generation.

| Operation | Meaning |
|---|---|
| `discuss` | Explore, reflect, acknowledge, or change topics; retain drafts. |
| `create_draft` | Request a new proposal, including changes to an existing event. |
| `revise_draft` | Give a concrete correction to an identified pending proposal. |
| `clarify` | Resolve an ambiguous action/target or unavailable operation. |

Pause and resume need no separate state machine in this experiment: a retained
draft can be referenced in later discussion. Confirmation/rejection remain
explicit button actions. This tests interpretation, not calendar execution.

## Run

Add `OPENROUTER_API_KEY` to the local `.env`. No separate provider keys or new
packages are needed. From the repository root:

```sh
.venv/bin/python scripts/benchmark_routing.py --dry-run
.venv/bin/python scripts/benchmark_routing.py --live --budget-usd 1
.venv/bin/python -m pytest tests/test_routing_benchmark.py -q
```

Default shortlist: **Gemini 3.5 Flash-Lite and GPT-5.6 Luna, both with low
reasoning** (`gemini-lite-new`, `gpt-luna-low`). Luna low is not yet live-tested;
the saved Luna results used none or minimal reasoning. Earlier candidates remain
selectable for explicit comparisons:

```sh
.venv/bin/python scripts/benchmark_routing.py --dry-run \
  --models gemini-lite-new gpt-luna-low gpt-luna-minimal gpt-luna
```

The model registry records exact IDs, reasoning settings, serving endpoints,
and price ceilings. After rate limits in price-sorted runs, the comparison pins
Luna to OpenAI, GLM to Fireworks, DeepSeek to DeepInfra FP8, and Gemini 3.5 to
Google Vertex global. DeepSeek's own endpoint lacks strict schema support;
provider capability must be checked separately from model capability.
Each manifest records the settings actually used; earlier runs retain their
original configuration. Endpoint changes are operational reruns, not prompt tuning.
The default models use low reasoning. Luna's optional `gpt-luna-minimal` alias
uses minimal reasoning, and `gpt-luna` preserves the no-reasoning baseline.
Grok uses no reasoning; the other Gemini aliases, GLM, and DeepSeek use low.
GLM defaults unsupported effort values to maximum, so it must not receive `none`.
Price ceilings use GLM's standard and DeepSeek's peak rates; reported costs reflect
the actual provider charge, including any discounts.
These are **configured model comparisons**, not equal-compute tests.
The current routing and chat models are available as baselines, but every model
receives the same compact experimental prompt. This does **not** measure the
deployed verbose prompt or the end-to-end Telegram workflow.

## Bounded improvement loop

1. Evaluate all selected models on development cases in interleaved order.
2. Ask the cheapest selected model to revise instructions from development
   failures only. By default, allow one candidate (`--rounds 2`).
3. Accept only a complete comparison with no model losing exact matches or
   increasing incorrect draft actions. Prefer higher accuracy, then shorter text.
4. Freeze the selected prompt and run held-out cases once.

The optimizer cannot edit cases, labels, code, or production configuration.
Use `--rounds 1` for a fixed-prompt comparison; `--repeats 2` measures variability
at additional cost. Do not tune further against exposed held-out results.
For rate-limited endpoints, `--interval-seconds 3` adds a pause between requests.
The report records this setting; request latency excludes the added waiting.
Use a frozen prompt and `--rounds 1` for such operational reruns.

## Spending and results

`--budget-usd` is the cumulative allowance for the ledger, not a fresh allowance
per invocation. Keep `data/routing_benchmarks/budget.json` across reruns and run
one process at a time. Each call reserves conservative input/output cost before
dispatch; errors retain reservations. Reported OpenRouter costs replace estimates
when available, with a 25% accounting margin. Development preserves 30% of the
remaining allowance for held-out evaluation. Incomplete coverage is reported.

Requests require schema support, disallow provider fallback, and cap token prices
and output length. No automatic retries, tools, calendars, or real conversations
are involved. OpenRouter's serving provider, request ID, usage, and charge are
recorded; unpinned models can vary providers between calls. Latency includes OpenRouter.

Each ignored run directory contains `report.md`, `report.json`, `attempts.jsonl`,
and frozen prompts/source hashes. Metrics include exact operation+target match,
incorrect draft actions that passed validation, invalid outputs, API failures,
median/p95 latency, and cost. Revisit ambiguous labels before declaring a winner.

Verified 2026-09-18: [model catalog and prices](https://openrouter.ai/api/v1/models),
[structured outputs](https://openrouter.ai/docs/guides/features/structured-outputs),
[provider controls](https://openrouter.ai/docs/guides/routing/provider-selection),
[usage accounting](https://openrouter.ai/docs/cookbook/administration/usage-accounting).
