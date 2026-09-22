# Cheap routing models — September 2026

Checked **2026-09-18**. Scope: short, state-constrained semantic routing through OpenRouter. This note identifies candidates; public general-purpose benchmarks do not establish David's routing accuracy.

## Why David currently uses Gemini Lite

The default router is `gemini-3.1-flash-lite` in `config.py`, and
`orchestrator/router.py` still calls the Gemini SDK. The OpenRouter adapter is
currently used by the benchmark. This makes Gemini the convenient implementation
baseline; it does not establish a market-wide quality advantage.

Our synthetic screening found 47/48 exact matches for Gemini 3.1 Flash Lite and
39/48 for Grok 4.3. Luna's successful responses matched their labels, but repeated
429 errors left coverage incomplete. The small, provisionally labeled dataset
supports keeping Gemini as a control, not declaring it best in class.
[Local results](../../data/routing_benchmarks/summary.md)

The earlier shortlist was too narrow. **Gemini 3.5 Flash-Lite**, released July 21,
should have been tested: Google reports gains over 3.1 on agentic and long-context
evaluations. Those are vendor results, not evidence about David's classification
accuracy. [Google release](https://blog.google/innovation-and-ai/models-and-research/gemini-models/gemini-3-6-flash-3-5-flash-lite-3-5-flash-cyber/)

## Candidate shortlist

Prices are USD per million uncached input/output tokens, as displayed by OpenRouter on the check date. Provider selection, promotions, caching, and reasoning tokens affect actual cost.

| Candidate / exact OpenRouter ID | Input / output | Why test it |
| --- | --- | --- |
| **GLM 5.3 Flash** — `z-ai/glm-5.3-flash` | **$0.15 / $0.50** standard; DeepInfra promotion **$0.075 / $0.25** | Strongest general capability/value evidence among this shortlist; schema output supported. [Providers and capabilities](https://openrouter.ai/z-ai/glm-5.3-flash) |
| **DeepSeek V4.1 Flash** — `deepseek/deepseek-v4.1-flash` | **$0.15 / $0.60** current floor | September 10 release; schema output supported. Provider prices range higher, so record the selected endpoint. [Providers and capabilities](https://openrouter.ai/deepseek/deepseek-v4.1-flash) |
| **Qwen3.8 Flash** — `qwen/qwen3.8-flash` | **$0.15 / $0.47** | August 26 release; inexpensive schema-capable challenger with controllable thinking. [OpenRouter](https://openrouter.ai/qwen/qwen3.8-flash), [Qwen model card](https://huggingface.co/Qwen/Qwen3.8-Flash-Next) |
| **GPT-5.6 Luna** — `openai/gpt-5.6-luna` | **$0.20 / $1.20** | Strong inexpensive proprietary candidate; supports structured output and reasoning disabled. Its local rate-limit failures should not be interpreted as semantic errors. [Official OpenAI specifications](https://developers.openai.com/api/docs/models/gpt-5.6-luna) |
| **Gemini 3.5 Flash-Lite** — `google/gemini-3.5-flash-lite` | **$0.30 / $2.50** | Fast current Google candidate; test minimal/low reasoning. Higher output price makes actual reasoning-token use important. [Google pricing](https://ai.google.dev/gemini-api/docs/pricing) |

DeepSeek's floor is time/provider dependent; its listed peak rate is $0.30/$1.20.
GLM's promotion is also provider dependent. Do not base a permanent budget on
temporary discounts. [DeepSeek providers](https://openrouter.ai/deepseek/deepseek-v4.1-flash),
[GLM providers](https://openrouter.ai/z-ai/glm-5.3-flash)

**Qwen3.7 Flash** (`qwen/qwen3.7-flash`) is cheaper at $0.03/$0.13 but its
documented endpoint offers JSON without schema enforcement, making it lower
priority for the existing harness. [OpenRouter](https://openrouter.ai/qwen/qwen3.7-flash)

MiniMax's current main generation is **M3**, not M2.7. `minimax/minimax-m3` starts at $0.23/$0.96; MiniMax's own endpoint is $0.30/$1.20. It is another useful candidate, but the lower-priced alternatives above deserve first coverage. This prioritization is an inference, not a measured David result. [OpenRouter prices](https://openrouter.ai/minimax/minimax-m3/pricing), [MiniMax release](https://www.minimax.io/blog/minimax-m3)

**Haiku 4.5** ($1/$5) and **Grok 4.3** ($1.25/$2.50) are less compelling for this
price-first routing shortlist. Grok's older Fast IDs redirect to 4.3 at its new
rates; old cheap-model comparisons can therefore mislead.
[Anthropic lineup](https://platform.claude.com/docs/en/models/overview),
[xAI retirement notice](https://docs.x.ai/developers/migration/may-15-retirement)

## Independent evidence

Artificial Analysis's current **Intelligence Index v4.3** combines ten evaluations covering agents, coding, knowledge, and reasoning. Its September 7 analysis explicitly places GLM-5.3-Flash on the intelligence-versus-cost-per-task frontier. This supports including it, not declaring it the best classifier. [Methodology and findings](https://artificialanalysis.ai/articles/artificial-analysis-intelligence-index-v4-3)

| Model / tested setting | AA intelligence score | Output tokens/sec |
| --- | ---: | ---: |
| [GLM-5.3-Flash](https://artificialanalysis.ai/models/glm-5-3-flash) | 42 | 98.6 |
| [DeepSeek V4.1 Flash, max](https://artificialanalysis.ai/models/deepseek-v4-1-flash) | 40 | 207.6 |
| [GPT-5.6 Luna, max](https://artificialanalysis.ai/models/gpt-5-6-luna) | 38 | 124.8 |
| [Gemini 3.5 Flash-Lite](https://artificialanalysis.ai/models/gemini-3-5-flash-lite) | 23 | 358.0 |

These figures are not comparable to a no-thinking classifier run. Output speed excludes time before generation and can conceal substantial reasoning. For short JSON decisions, measure complete-response p50/p95 latency and decision correctness in David. AA separately defines first-answer latency to include reasoning time. [Measurement definitions](https://artificialanalysis.ai/models/glm-5-3-flash)

Low token prices do not guarantee low task cost. AA reports $0.25 per index task
for GLM, $0.27 for DeepSeek at peak rates, $0.18 for Luna, and $0.12 for Gemini
3.5 Lite, with different quality levels and reasoning settings. These are broad
benchmark tasks, not classifier-call cost estimates. The model pages linked in
the table provide those measurements.

## Integration constraints

- **GLM:** explicitly request `low`; its model card says only `low`, `high`, and `max` are valid, and other values default to `max`. Do not assume `none` disables reasoning. [Z.ai model card](https://huggingface.co/zai-org/GLM-5.3-Flash)
- **Qwen3.8:** supports disabling thinking, but confirm the OpenRouter endpoint honors the mapped setting. [Qwen model card](https://huggingface.co/Qwen/Qwen3.8-Flash-Next)
- **DeepSeek:** use the explicit V4.1 ID. The first-party service retired older V4 Flash IDs and redirects them; peak/off-peak prices also differ. [September release and migration notice](https://deepseek.com/en/news/deepseek-v4-1-flash/)
- Require `provider.require_parameters: true` with `response_format.type: json_schema`, and retain application validation of permitted operations and targets. Supported endpoint filtering matters as much as the model name. [OpenRouter structured outputs](https://openrouter.ai/docs/guides/features/structured-outputs)

**Recommendation:** prioritize GLM 5.3 Flash, Luna, DeepSeek V4.1 Flash, and
Gemini 3.5 Flash-Lite; include Qwen3.8 Flash if expanding the field. Keep 3.1 Lite
as the measured control. GLM is the strongest new value candidate from this
research; Luna remains a strong proprietary contender. Keep reasoning settings
and provider IDs in results. Choose on incorrect actions, complete coverage,
latency, and cost per successful decision. This research did not change model
configuration or make paid calls.
