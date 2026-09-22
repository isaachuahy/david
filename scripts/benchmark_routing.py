"""Budgeted routing benchmark and bounded prompt-improvement loop.

Usage: python scripts/benchmark_routing.py --dry-run
       python scripts/benchmark_routing.py --live --budget-usd 1

The optimizer sees development failures only. Held-out cases run after prompt
selection; no generated instructions, model choice, or actions reach the bot.
"""

import argparse
import hashlib
import json
import math
import os
import random
import statistics
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
from reasoning.model_client import KEY_NAMES, ModelAccessError, ModelReply, generate_structured


@dataclass(frozen=True)
class BenchmarkModel:
    """Pins the model, inference setting, and dated standard token prices."""

    provider: str
    model: str
    input_usd_per_million: float
    output_usd_per_million: float
    reasoning: str
    endpoint_provider: str | None = None


# OpenRouter catalog prices verified 2026-09-18; also sent as request price caps.
# Keep the comparison focused on inexpensive routing candidates.
# GLM uses its standard price, and DeepSeek its peak price, to avoid relying on
# temporary discounts. Their low reasoning setting is explicitly supported.
# Pin alternatives after price-sorted runs hit rate limits. These endpoint
# choices are serialized into each run's manifest alongside the model settings.
MODELS = {
    "gemini-lite": BenchmarkModel("openrouter", "google/gemini-3.1-flash-lite", .25, 1.50, "low"),
    "gemini-lite-new": BenchmarkModel("openrouter", "google/gemini-3.5-flash-lite", .30, 2.50, "low", "google-vertex/global"),
    "gemini-flash": BenchmarkModel("openrouter", "google/gemini-3-flash-preview", .50, 3.00, "low"),
    "gpt-luna": BenchmarkModel("openrouter", "openai/gpt-5.6-luna", .20, 1.20, "none", "openai"),
    # Keep the no-reasoning baseline available for comparisons with this setting.
    "gpt-luna-minimal": BenchmarkModel("openrouter", "openai/gpt-5.6-luna", .20, 1.20, "minimal", "openai"),
    # Give low reasoning its own alias so historical comparisons stay explicit.
    "gpt-luna-low": BenchmarkModel("openrouter", "openai/gpt-5.6-luna", .20, 1.20, "low", "openai"),
    "glm-flash": BenchmarkModel("openrouter", "z-ai/glm-5.3-flash", .15, .50, "low", "fireworks"),
    "deepseek-flash": BenchmarkModel("openrouter", "deepseek/deepseek-v4.1-flash", .30, 1.20, "low", "deepinfra/fp8"),
    "grok": BenchmarkModel("openrouter", "x-ai/grok-4.3", 1.25, 2.50, "none"),
}
DEFAULT_MODELS = ["gemini-lite-new", "gpt-luna-low"]
PRICE_SOURCES = {"openrouter": "https://openrouter.ai/api/v1/models"}


def digest(value: object) -> str:
    """Hashes exact experiment inputs so comparisons can be reproduced."""
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def write_json(path: Path, value: object) -> None:
    """Writes one experiment artifact at a time, independent of bot persistence."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


class Budget:
    """Reserves cost before dispatch and retains reservations for uncertain calls.

    Reuse the same ledger across runs to keep the user's cumulative allowance.
    Run only one benchmark process per ledger. A corrupt ledger fails closed.
    """

    def __init__(self, limit: float, path: Path):
        if not math.isfinite(limit) or limit <= 0:
            raise ValueError("Budget must be a positive finite USD amount.")
        self.path = path
        self.limit = limit
        self.entries = json.loads(path.read_text())["entries"] if path.exists() else []

    @property
    def used(self) -> float:
        """Includes completed costs and calls whose billing remains uncertain."""
        return sum(entry["accounted_usd"] for entry in self.entries)

    def reserve(self, amount: float, purpose: str, keep_usd: float = 0) -> int:
        """Persists the reservation before an API request can spend money."""
        if not math.isfinite(amount) or amount <= 0 or self.used + amount + keep_usd > self.limit:
            raise BudgetExhausted("Remaining allowance cannot cover the next request.")
        self.entries.append({"purpose": purpose, "accounted_usd": amount, "status": "reserved"})
        self.save()
        return len(self.entries) - 1

    def settle(self, index: int, cost: float | None) -> None:
        """Usage-based estimates include a margin for cache writes and overhead."""
        if cost is not None and math.isfinite(cost) and cost >= 0:
            self.entries[index].update(accounted_usd=cost * 1.25, status="usage_estimate")
        self.save()

    def save(self) -> None:
        """Updates the cumulative ledger before the next sequential call."""
        write_json(self.path, {"limit_usd": self.limit, "accounted_usd": self.used, "entries": self.entries})


class BudgetExhausted(RuntimeError):
    """Stops new calls while preserving completed results."""


def allowed_operations(state: dict) -> list[str]:
    """Derives capabilities from application state without interpreting language."""
    operations = ["discuss", "clarify"]
    if state.get("can_create_draft", True):
        operations.append("create_draft")
    if state.get("can_revise_drafts", True) and any(
        draft["status"] == "pending" for draft in state["drafts"]
    ):
        operations.append("revise_draft")
    return operations


def operation_schema(state: dict) -> dict:
    """Restricts generation to available operations and known draft references."""
    return {
        "type": "object", "additionalProperties": False,
        "properties": {
            "operation": {"type": "string", "enum": allowed_operations(state)},
            "target_id": {"type": ["string", "null"], "enum": [None] + [d["id"] for d in state["drafts"]]},
        },
        "required": ["operation", "target_id"],
    }


def case_payload(case: dict, contexts: dict) -> dict:
    """Excludes labels, split, IDs, and family metadata from tested model input."""
    state = contexts[case["context"]]
    return {
        "message": case["message"], "conversation": case["history"],
        "state": state, "allowed_operations": allowed_operations(state),
    }


def valid_operation(prediction: dict, state: dict) -> bool:
    """Checks relationships JSON shape alone cannot enforce across fields."""
    if set(prediction) != {"operation", "target_id"}:
        return False
    operation, target = prediction["operation"], prediction["target_id"]
    if not isinstance(operation, str) or operation not in allowed_operations(state):
        return False
    drafts = {draft["id"]: draft for draft in state["drafts"]}
    if target is not None and (not isinstance(target, str) or target not in drafts):
        return False
    if operation == "revise_draft":
        return target in drafts and drafts[target]["status"] == "pending"
    if operation in {"create_draft", "clarify"}:
        return target is None
    return True


def score_prediction(text: str, expected: dict, state: dict) -> dict:
    """Scores raw interpretation separately from invalid actions blocked by code."""
    try:
        prediction = json.loads(text)
        if not isinstance(prediction, dict):
            raise ValueError("Output must be an object.")
        valid = valid_operation(prediction, state)
    except (ValueError, TypeError):
        prediction, valid = {}, False
    operation_ok = prediction.get("operation") == expected["operation"]
    target_ok = "target_id" in prediction and prediction["target_id"] == expected["target_id"]
    mutates = isinstance(prediction.get("operation"), str) and prediction["operation"] in {"create_draft", "revise_draft"}
    unsafe = mutates and (not operation_ok or not target_ok)
    return {
        "prediction": prediction, "valid": valid, "operation_ok": operation_ok,
        "target_ok": target_ok, "exact": valid and operation_ok and target_ok,
        "unsafe_suggestion": unsafe, "unsafe_allowed": unsafe and valid,
        "unnecessary_clarification": prediction.get("operation") == "clarify" and expected["operation"] != "clarify",
    }


def load_cases(path: Path) -> dict:
    """Validates labels and prevents related families crossing the held-out split."""
    data = json.loads(path.read_text(encoding="utf-8"))
    seen, families = set(), {}
    for case in data["cases"]:
        # The fixture is human-reviewable ground truth; neither the model nor
        # optimizer may silently repair labels or split assignments.
        if case["id"] in seen or case["split"] not in {"dev", "holdout"}:
            raise ValueError("Duplicate case ID or invalid split.")
        seen.add(case["id"])
        if families.setdefault(case["family"], case["split"]) != case["split"]:
            raise ValueError("A case family crosses the held-out split.")
        if not valid_operation(case["expected"], data["contexts"][case["context"]]):
            raise ValueError(f"Invalid expected operation in {case['id']}")
    if {case["split"] for case in data["cases"]} != {"dev", "holdout"}:
        raise ValueError("Both development and held-out cases are required.")
    return data


def estimate_cost(model: BenchmarkModel, reply: ModelReply) -> float | None:
    """Prefers OpenRouter's reported charge, otherwise uses standard token prices."""
    if isinstance(reply.cost_usd, (int, float)) and math.isfinite(reply.cost_usd) and reply.cost_usd >= 0:
        return reply.cost_usd
    if any(not isinstance(value, int) or value < 0 for value in (reply.input_tokens, reply.output_tokens)):
        return None
    return (reply.input_tokens * model.input_usd_per_million + reply.output_tokens * model.output_usd_per_million) / 1_000_000


def reservation_cost(model: BenchmarkModel, instruction: str, payload: dict, schema: dict, max_tokens: int) -> float:
    """Uses byte length plus overhead and capped output as a conservative bound."""
    input_bound = len(json.dumps([instruction, payload, schema], ensure_ascii=False).encode()) + 2048
    return (input_bound * model.input_usd_per_million * 1.5 + max_tokens * model.output_usd_per_million * 1.25) / 1_000_000


def call_with_budget(model: BenchmarkModel, instruction: str, payload: dict, schema: dict,
                     budget: Budget, purpose: str, max_tokens: int, caller: Callable,
                     keep_usd: float = 0) -> tuple[ModelReply, float | None]:
    """Accounts for one attempt; failures retain their reservation and never retry."""
    ticket = budget.reserve(reservation_cost(model, instruction, payload, schema, max_tokens), purpose, keep_usd)
    reply = caller(provider=model.provider, model=model.model, instruction=instruction,
                   payload=payload, schema=schema, reasoning=model.reasoning, max_output_tokens=max_tokens,
                   endpoint_provider=model.endpoint_provider,
                   max_price={"prompt": model.input_usd_per_million,
                              "completion": model.output_usd_per_million, "request": 0})
    cost = estimate_cost(model, reply)
    budget.settle(ticket, cost)
    return reply, cost


def evaluate(data: dict, models: dict, instruction: str, split: str, repeats: int,
             budget: Budget, output: Path, round_name: str, max_tokens: int,
             caller: Callable = generate_structured, keep_usd: float = 0,
             interval_seconds: float = 0) -> list[dict]:
    """Interleaves models and repeats; every attempt remains visible in the denominator."""
    cases = [case for case in data["cases"] if case["split"] == split]
    jobs = [(case, name, repeat) for case in cases for name in models for repeat in range(repeats)]
    random.Random(17).shuffle(jobs)
    results, consecutive_errors = [], {name: 0 for name in models}
    for case, name, repeat in jobs:
        # Stop an unavailable model after two consecutive failures rather than
        # wasting the user's allowance on every remaining case.
        if consecutive_errors[name] >= 2:
            continue
        # Optional pacing isolates rate limits from interpretation quality.
        # Waiting is outside the adapter's measured request latency.
        if results and interval_seconds:
            time.sleep(interval_seconds)
        state = data["contexts"][case["context"]]
        row = {"case_id": case["id"], "family": case["family"], "split": split,
               "model": name, "repeat": repeat, "round": round_name, "expected": case["expected"]}
        try:
            reply, cost = call_with_budget(
                models[name], instruction, case_payload(case, data["contexts"]), operation_schema(state),
                budget, f"{round_name}:{name}:{case['id']}:{repeat}", max_tokens, caller, keep_usd,
            )
            row.update(score_prediction(reply.text, case["expected"], state))
            # Valid JSON from a truncated generation is still an incomplete call.
            if reply.finish_reason != "stop":
                row.update(valid=False, exact=False, unsafe_allowed=False)
            row.update(reply=asdict(reply), estimated_cost_usd=cost, error=None)
            consecutive_errors[name] = 0
        except BudgetExhausted:
            print("Budget stop: preserving completed results.", flush=True)
            break
        except Exception as error:
            row.update(score_prediction("", case["expected"], state))
            row.update(reply=None, estimated_cost_usd=None, error=f"{type(error).__name__}: {error}",
                       fatal=isinstance(error, ModelAccessError))
            consecutive_errors[name] += 1
        results.append(row)
        with (output / "attempts.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
        print(f"{round_name} {len(results)}/{len(jobs)} {name} {case['id']}: "
              f"{'pass' if row['exact'] else row['error'] or 'mismatch'}", flush=True)
        if row.get("fatal"):
            break
    return results


def summarize(rows: list[dict], models: dict, planned_per_model: int) -> dict:
    """Keeps coverage, operational failures, interpretation errors, and cost distinct."""
    summary = {}
    for name in models:
        # Missing attempts must not make a partially evaluated model look best.
        attempts = [row for row in rows if row["model"] == name]
        latencies = sorted(row["reply"]["latency_ms"] for row in attempts if row["reply"])
        count = len(attempts)
        summary[name] = {
            "attempted": count, "planned": planned_per_model, "complete": count == planned_per_model,
            "exact": sum(row["exact"] for row in attempts),
            "accuracy": sum(row["exact"] for row in attempts) / count if count else None,
            "operation_errors": sum(not row["operation_ok"] for row in attempts),
            "target_errors": sum(not row["target_ok"] for row in attempts),
            "invalid_outputs": sum(not row["valid"] for row in attempts),
            "unsafe_suggestions": sum(row["unsafe_suggestion"] for row in attempts),
            "unsafe_allowed": sum(row["unsafe_allowed"] for row in attempts),
            "unnecessary_clarifications": sum(row["unnecessary_clarification"] for row in attempts),
            "api_errors": sum(row["error"] is not None for row in attempts),
            "median_ms": statistics.median(latencies) if latencies else None,
            "p95_ms": latencies[max(0, math.ceil(len(latencies) * .95) - 1)] if latencies else None,
            "estimated_cost_usd": sum(row["estimated_cost_usd"] or 0 for row in attempts),
            "unknown_cost_calls": sum(row["estimated_cost_usd"] is None for row in attempts),
        }
    return summary


def improvement_payload(data: dict, rows: list[dict], instruction: str) -> dict:
    """Builds optimizer feedback from development examples only, never holdout."""
    by_id = {case["id"]: case for case in data["cases"] if case["split"] == "dev"}
    failures = []
    for row in rows:
        # Labels are allowed only in this development feedback call. The tested
        # models always receive case_payload, which contains no expected answer.
        if row["split"] == "dev" and not row["exact"] and row["error"] is None:
            failures.append({"input": case_payload(by_id[row["case_id"]], data["contexts"]),
                             "expected": row["expected"], "predicted": row["prediction"]})
    return {"current_instruction": instruction, "development_failures": failures[:8]}


def accept_candidate(previous: dict, candidate: dict, old_prompt: str, new_prompt: str) -> bool:
    """Requires complete comparisons and no increase in unsafe actions for any model."""
    if any(not candidate[name]["complete"] or not previous[name]["complete"] for name in previous):
        return False
    if any(candidate[name]["unsafe_allowed"] > previous[name]["unsafe_allowed"]
           or candidate[name]["unsafe_suggestions"] > previous[name]["unsafe_suggestions"]
           or candidate[name]["exact"] < previous[name]["exact"]
           or candidate[name]["api_errors"] > previous[name]["api_errors"] for name in previous):
        return False
    old_exact = sum(item["exact"] for item in previous.values())
    new_exact = sum(item["exact"] for item in candidate.values())
    return new_exact > old_exact or (new_exact == old_exact and len(new_prompt) < len(old_prompt))


def write_report(output: Path, report: dict) -> None:
    """Writes a compact leaderboard while clearly labeling incomplete comparisons."""
    write_json(output / "report.json", report)
    lines = ["# Routing benchmark", "", "Synthetic cases; provisional labels; held-out screening, not production validation.", "",
             f"Budget accounted: **US${report['accounted_usd']:.4f} / ${report['budget_usd']:.2f}** (includes uncertainty reserves).", "",
             "| Model | Coverage | Exact | Unsafe allowed | API errors | Median / p95 ms | Estimated USD |",
             "|---|---:|---:|---:|---:|---:|---:|"]
    for name, result in report["holdout"].items():
        # Keep denominators visible: unavailable or interrupted models have no
        # defensible rank and should not be compared using a partial percentage.
        latency = f"{result['median_ms']:.0f} / {result['p95_ms']:.0f}" if result["median_ms"] is not None else "—"
        cost = f"${result['estimated_cost_usd']:.5f}" + (" + unknown" if result["unknown_cost_calls"] else "")
        lines.append(f"| {name} | {result['attempted']}/{result['planned']} | {result['exact']}/{result['attempted']} | "
                     f"{result['unsafe_allowed']} | {result['api_errors']} | {latency} | {cost} |")
    lines += ["", "Costs use OpenRouter's reported charge when available, otherwise standard token estimates. Unknown-cost calls retain a conservative budget reservation.",
              "", f"Run stopped: {report.get('stopped_reason') or 'No account error'}.",
              "", f"Skipped providers/models: {json.dumps(report['skipped'])}", "",
              "The operation contract has no pause/resume state: discussion retains drafts and can refer back to them.",
              "Prompt refinement evaluated a candidate on development cases." if len(report["rounds"]) > 1 else "No prompt candidate was evaluated; the original instruction was retained.",
              "Held-out results must not be fed into further refinement of this dataset.",
              "", "See report.json for per-model diagnostics, prompt versions, source hashes, and pricing sources; attempts.jsonl contains each prediction."]
    (output / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def run(args: argparse.Namespace, caller: Callable = generate_structured) -> Path | None:
    """Runs baseline → bounded development refinement → frozen held-out evaluation."""
    load_dotenv(ROOT / ".env")
    interval_seconds = getattr(args, "interval_seconds", 0)
    if not math.isfinite(interval_seconds) or not 0 <= interval_seconds <= 30:
        raise ValueError("Request interval must be between 0 and 30 seconds.")
    data = load_cases(args.cases)
    instruction = args.prompt.read_text(encoding="utf-8").strip()
    requested = {name: MODELS[name] for name in args.models}
    models = {name: model for name, model in requested.items() if os.getenv(KEY_NAMES[model.provider], "").strip()}
    skipped = {name: f"Missing {KEY_NAMES[model.provider]}" for name, model in requested.items() if name not in models}
    counts = {split: sum(case["split"] == split for case in data["cases"]) for split in ("dev", "holdout")}
    print(json.dumps({"cases": counts, "models": list(models), "skipped": skipped,
                      "repeats": args.repeats, "rounds": args.rounds, "live": args.live}, indent=2), flush=True)
    if not args.live:
        return None
    if not models:
        raise ValueError("No requested provider has a configured API key.")
    if args.budget_usd is None:
        raise ValueError("Live runs require an explicit --budget-usd allowance.")
    output = args.output
    output.mkdir(parents=True, exist_ok=False)
    budget = Budget(args.budget_usd, args.ledger)
    # Preserve part of the remaining allowance for evaluation after development.
    # Coverage is still reported explicitly if this cannot fund the whole suite.
    holdout_allowance = max(0, budget.limit - budget.used) * .30
    try:
        git_head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    except subprocess.SubprocessError:
        git_head = "unavailable"
    report = {"created_at": datetime.now(timezone.utc).isoformat(), "git_head": git_head,
              "dataset_hash": digest(data), "runner_hash": digest(Path(__file__).read_text()),
              "client_hash": digest((ROOT / "reasoning/model_client.py").read_text()),
              "models": {name: asdict(model) for name, model in requested.items()}, "skipped": skipped,
              "price_date": "2026-09-18", "price_sources": PRICE_SOURCES, "repeats": args.repeats,
              "max_output_tokens": args.max_output_tokens, "holdout_allowance_usd": holdout_allowance,
              "interval_seconds": interval_seconds,
              "budget_usd": args.budget_usd, "rounds": [], "selected_prompt": instruction}
    write_json(output / "manifest.json", report)
    rows = evaluate(data, models, instruction, "dev", args.repeats, budget, output, "baseline", args.max_output_tokens, caller, holdout_allowance, interval_seconds)
    previous = summarize(rows, models, counts["dev"] * args.repeats)
    report["rounds"].append({"name": "baseline", "prompt": instruction, "prompt_hash": digest(instruction), "dev": previous})
    report["stopped_reason"] = next((row["error"] for row in rows if row.get("fatal")), None)

    for number in range(1, args.rounds):
        # Only complete development runs can justify spending money on a new
        # prompt. API outages are operational failures, not training examples.
        feedback = improvement_payload(data, rows, instruction)
        if report["stopped_reason"] or not feedback["development_failures"] or any(not value["complete"] or value["api_errors"] for value in previous.values()):
            break
        optimizer = min(models.values(), key=lambda model: model.input_usd_per_million + model.output_usd_per_million)
        schema = {"type": "object", "additionalProperties": False,
                  "properties": {"instruction": {"type": "string"}}, "required": ["instruction"]}
        try:
            reply, _ = call_with_budget(
                optimizer,
                "Improve this routing instruction from development failures. Keep the same four operations and target contract. "
                "Write general rules, not case-specific answers or IDs. Maximum 1400 characters. Do not change labels or propose code changes.",
                feedback, schema, budget, f"optimizer:{number}", args.max_output_tokens, caller, holdout_allowance,
            )
            write_json(output / f"optimizer_{number}.json", asdict(reply))
            candidate = json.loads(reply.text)["instruction"].strip()
            if not candidate or len(candidate) > 1400:
                break
        except BudgetExhausted:
            break
        except Exception as error:
            report["optimizer_error"] = f"{type(error).__name__}: {error}"
            if isinstance(error, ModelAccessError):
                report["stopped_reason"] = report["optimizer_error"]
            break
        candidate_rows = evaluate(data, models, candidate, "dev", args.repeats, budget, output, f"candidate_{number}", args.max_output_tokens, caller, holdout_allowance, interval_seconds)
        candidate_summary = summarize(candidate_rows, models, counts["dev"] * args.repeats)
        report["stopped_reason"] = next((row["error"] for row in candidate_rows if row.get("fatal")), None)
        accepted = accept_candidate(previous, candidate_summary, instruction, candidate)
        report["rounds"].append({"name": f"candidate_{number}", "prompt": candidate, "prompt_hash": digest(candidate),
                                 "accepted": accepted, "dev": candidate_summary})
        if not accepted:
            break
        instruction, rows, previous = candidate, candidate_rows, candidate_summary

    report["selected_prompt"] = instruction
    # Freeze the selected instruction before revealing held-out outcomes.
    write_json(output / "selected_prompt.json", {"instruction": instruction, "hash": digest(instruction)})
    holdout = [] if report["stopped_reason"] else evaluate(
        data, models, instruction, "holdout", args.repeats, budget, output, "holdout", args.max_output_tokens, caller,
        interval_seconds=interval_seconds,
    )
    report["stopped_reason"] = report["stopped_reason"] or next((row["error"] for row in holdout if row.get("fatal")), None)
    report["holdout"] = summarize(holdout, models, counts["holdout"] * args.repeats)
    report["accounted_usd"] = budget.used
    write_report(output, report)
    print(f"Report: {output / 'report.md'}", flush=True)
    return output


def main() -> None:
    """Keeps live spending opt-in and output locations explicit."""
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--live", action="store_true")
    mode.add_argument("--dry-run", action="store_true")
    parser.add_argument("--models", nargs="+", choices=MODELS, default=DEFAULT_MODELS)
    parser.add_argument("--cases", type=Path, default=ROOT / "evals/routing_cases.json")
    parser.add_argument("--prompt", type=Path, default=ROOT / "evals/routing_prompt.txt")
    parser.add_argument("--repeats", type=int, choices=range(1, 6), default=1)
    parser.add_argument("--rounds", type=int, choices=range(1, 4), default=2)
    parser.add_argument("--max-output-tokens", type=int, choices=(512, 1024, 2048), default=1024)
    parser.add_argument("--interval-seconds", type=float, default=0)
    parser.add_argument("--budget-usd", type=float)
    parser.add_argument("--ledger", type=Path, default=ROOT / "data/routing_benchmarks/budget.json")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    parser.add_argument("--output", type=Path, default=ROOT / "data/routing_benchmarks" / stamp)
    args = parser.parse_args()
    try:
        run(args)
    except (ValueError, FileExistsError) as error:
        parser.error(str(error))


if __name__ == "__main__":
    main()
