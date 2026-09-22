"""Run live backend workflow checks without canned model or Calendar responses.

Preflight: .venv/bin/python scripts/eval_assistant_live.py
Run:       .venv/bin/python scripts/eval_assistant_live.py --run --budget-usd 0.95
Compare:   .venv/bin/python scripts/eval_assistant_live.py --run --compare-advice --budget-usd 0.93
Expanded:  .venv/bin/python scripts/eval_assistant_live.py --run --expanded --budget-usd 0.77

The production Telegram handlers run against a local presentation driver. Gemini,
Google Calendar reads, prompts, routing, validation, and SQLite are real. This does
not test Telegram network delivery or Calendar writes. Each run has isolated local
state; the report contains real transcripts and should be treated as private.

Only the scripted workflow checks are automatic. The S15/S16 advice pair also emits
its semantic rubric for human review; a structural pass is not an advice-quality
pass, nor a pass of the complete 50-scenario regression gate.
"""

import argparse
import asyncio
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
from importlib.metadata import version
import json
import os
from pathlib import Path
import random
import shutil
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

# Standard paid text prices and limits verified 2026-09-22. Reserve the entire
# input window, so the guard does not depend on a guessed token/character ratio.
# https://ai.google.dev/gemini-api/docs/pricing
# https://ai.google.dev/gemini-api/docs/models/gemini-3-flash-preview
# https://ai.google.dev/gemini-api/docs/models/gemini-3.5-flash-lite
# https://ai.google.dev/gemini-api/docs/models/gemini-3.8-flash
# The 3.8 rates below expire after 2026-12-31; refresh this dated price table then.
PRICES = {"gemini-3-flash-preview": (0.50, 3.00), "gemini-3.5-flash-lite": (0.30, 2.50),
          "gemini-3.8-flash": (0.75, 3.75)}
INPUT_LIMIT = 1_048_576
OUTPUT_LIMIT = 65_536

# Experimental advice contract, kept out of production prompts. It contains no
# scenario-specific task names, quantities, or expected answers.
ADVICE_APPENDIX = """
Recommend a useful next step and briefly explain why. Follow the user's stated
goal and constraints; don't invent facts. Keep advice concise. Add tradeoffs,
verification, fallbacks, or decision criteria only when they materially help.
""".strip()


def require(condition, message):
    """Keep live assertions enabled even when Python runs with optimization."""
    if not condition:
        raise AssertionError(message)


class LocalChat:
    """Capture presentation only; no model, Calendar, or persistence behavior lives here."""

    def __init__(self):
        self.messages = []

    async def send_message(self, chat_id, text, **kwargs):
        """Store the exact handler output and controls for subsequent checks."""
        message = SimpleNamespace(message_id=len(self.messages) + 1, text=text,
                                  reply_markup=kwargs.get("reply_markup"))
        self.messages.append(message)
        return message

    async def edit_message_text(self, chat_id, message_id, text, **kwargs):
        """Retain revisions of the local presentation without contacting Telegram."""
        message = self.messages[message_id - 1]
        message.text = text
        message.reply_markup = kwargs.get("reply_markup")
        return message


class LiveRun:
    """Persist evidence and reserve spend before any real model request is sent."""

    def __init__(self, report_path, budget):
        self.path = report_path
        self.budget = budget
        self.chat_output_cap = None
        self.report = {
            "started_at": datetime.now(timezone.utc).isoformat(),
            "scope": "live backend; local chat presentation; real Calendar reads; no writes",
            "status": "running", "budget_usd": budget, "prices_verified": "2026-09-22",
            "overall_verdict": "pending_quality_review",
            "calls": [], "calendar_requests": [], "turns": [], "checks": [],
            "proposal_snapshots": [],
            "advice_quality": "requires human review", "full_golden_gate": "not evaluated",
        }
        # Refuse to overwrite earlier evidence or silently reset its budget ledger.
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with os.fdopen(os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as file:
            json.dump(self.report, file)

    def save(self):
        """Keep completed evidence even if a later live request fails."""
        self.path.write_text(json.dumps(self.report, indent=2, ensure_ascii=False) + "\n")

    @contextmanager
    def observe(self):
        """Wrap real calls for accounting; forward their inputs and outputs unchanged."""
        from google.genai.models import Models
        from google.genai.types import GenerateContentConfig
        from googleapiclient.http import HttpRequest
        from observability.context_usage import gemini_token_usage
        import bot.handlers as handlers

        original_generate = Models.generate_content
        original_execute = HttpRequest.execute
        original_route = handlers.route_turn

        def generate(client, *, model, contents, config=None):
            """Admit one actual SDK call only when its worst-case cost fits."""
            require(model in PRICES, f"Unpriced model: {model}")
            settings = GenerateContentConfig.model_validate(config or {})
            if self.chat_output_cap and model != "gemini-3.5-flash-lite":
                # The controlled comparison declares the same output cap for both
                # chat candidates. Normal live workflow runs retain production settings.
                settings.max_output_tokens = self.chat_output_cap
                config = settings
            api_client = client._api_client
            require(not api_client.vertexai, "Vertex AI is outside this runner's verified pricing")
            client_options = api_client._http_options
            request_options = settings.http_options
            base_url = (request_options.base_url if request_options else None) or client_options.base_url
            endpoint = urlsplit(str(base_url))
            require(endpoint.scheme == "https" and endpoint.netloc == "generativelanguage.googleapis.com",
                    "Only the verified Gemini Developer API endpoint is allowed")
            require(isinstance(contents, str), "Only production text prompts are priced")
            require(not settings.tools and not settings.cached_content,
                    "This budget guard supports uncached text requests without native tools only")
            require(settings.candidate_count in (None, 1), "Only one candidate is priced")
            # A per-request HTTP override can inherit the client's retry policy.
            retry = ((request_options.retry_options if request_options else None)
                     or client_options.retry_options)
            require(retry is None or retry.attempts in (0, 1), "Multiple SDK attempts are not budgeted")
            output_limit = settings.max_output_tokens or OUTPUT_LIMIT
            require(0 < output_limit <= OUTPUT_LIMIT, "Unsupported output limit")
            input_price, output_price = PRICES[model]
            reserve = (INPUT_LIMIT * input_price + output_limit * output_price) / 1_000_000
            spent = sum(call["accounted_usd"] for call in self.report["calls"])
            require(spent + reserve <= self.budget, "Budget cannot cover the next worst-case request")
            call = {"model": model, "status": "reserved", "accounted_usd": reserve,
                    "max_output_tokens": settings.max_output_tokens}
            schema = settings.response_schema
            call["request"] = {"contents": contents, "system_instruction": str(settings.system_instruction),
                               "temperature": settings.temperature,
                               "thinking_config": settings.thinking_config.model_dump(mode="json") if settings.thinking_config else None,
                               "response_schema": schema.model_json_schema() if isinstance(schema, type) else settings.response_json_schema}
            self.report["calls"].append(call)
            self.save()
            started = time.monotonic()
            try:
                response = original_generate(client, model=model, contents=contents, config=config)
                usage = gemini_token_usage(response)
                require(usage["input_tokens"] is not None and usage["output_tokens"] is not None,
                        "Missing usage: retain reservation and stop")
                cost = (usage["input_tokens"] * input_price + usage["output_tokens"] * output_price) / 1_000_000
                require(cost <= reserve, "Reported usage exceeded the price reservation")
                finish = response.candidates[0].finish_reason if response.candidates else None
                call.update(status="reported", usage=usage, accounted_usd=cost,
                            model_version=response.model_version, response_id=response.response_id,
                            finish_reason=getattr(finish, "value", finish), raw_response_text=response.text)
                if self.chat_output_cap:
                    require(call["finish_reason"] == "STOP", "Comparison response did not finish; retain as failure")
                return response
            except Exception as error:
                # Unknown billing retains the full reservation; never retry it automatically.
                call.update(status="error", error_type=type(error).__name__)
                raise
            finally:
                call["latency_seconds"] = round(time.monotonic() - started, 3)
                self.save()

        def execute(request, *args, **kwargs):
            """Allow actual reads and fail closed before any Calendar mutation."""
            entry = {"method": request.method, "operation": request.methodId, "status": "started"}
            self.report["calendar_requests"].append(entry)
            try:
                require(request.method == "GET", "Live smoke tests do not permit Calendar writes")
                result = original_execute(request, *args, **kwargs)
                entry["status"] = "success"
                return result
            except Exception as error:
                # Production sometimes swallows Calendar errors. Preserve that evidence so
                # an empty result after a failed read cannot masquerade as a passing test.
                entry.update(status="error", error_type=type(error).__name__)
                raise
            finally:
                self.save()

        def route(*args, **kwargs):
            """Record the real interpretation, including its selected pending target."""
            decision = original_route(*args, **kwargs)
            self.report["turns"][-1]["route"] = decision.model_dump()
            return decision

        Models.generate_content, HttpRequest.execute, handlers.route_turn = generate, execute, route
        try:
            yield
        finally:
            Models.generate_content, HttpRequest.execute, handlers.route_turn = original_generate, original_execute, original_route

    async def turn(self, context, case_id, text, operation):
        """Drive the real authorized handler and detect errors it catches internally."""
        from bot.handlers import handle_message
        from persistence.database import get_db

        entry = {"id": case_id, "input": text, "automated_checks_status": "running",
                 "quality_review": {"status": "not_evaluated", "reviewer": None}}
        self.report["turns"].append(entry)
        before = len(context.bot.messages)
        history_before = len(context.user_data.get("chat_history", []))
        calls_before = len(self.report["calls"])
        started = time.monotonic()

        async def reply_text(text, **kwargs):
            """Send handler replies to the same local presentation stream as proposals."""
            return await context.bot.send_message(1, text, **kwargs)

        update = SimpleNamespace(effective_user=SimpleNamespace(id=1), effective_chat=SimpleNamespace(id=1),
                                 message=SimpleNamespace(text=text, reply_text=reply_text), callback_query=None)
        try:
            await handle_message(update, context)
            entry["outputs"] = [message.text for message in context.bot.messages[before:]]
            entry["controls"] = [message.reply_markup.to_dict() if message.reply_markup else None
                                 for message in context.bot.messages[before:]]
            # Capture actual state before assertions, including unexpected routes;
            # temporary storage is removed after the run even when a check fails.
            entry["proposal_items"] = list(get_db()["proposal_items"].rows)
            entry["calendar_writes"] = list(get_db()["calendar_writes"].rows)
            expected_routes = (operation,) if isinstance(operation, str) else operation
            entry["expected_routes"] = expected_routes
            entry["call_indices"] = list(range(calls_before, len(self.report["calls"])))
            entry["response_words"] = sum(len(output.split()) for output in entry["outputs"])
            require(entry.get("route", {}).get("operation") in expected_routes, f"{case_id}: unexpected route")
            require(len(self.report["calls"]) == calls_before + 2, f"{case_id}: expected real routing and generation")
            require(all(call["status"] == "reported" for call in self.report["calls"]), "A live model call failed")
            require(all(call["status"] == "success" for call in self.report["calendar_requests"]), "A real Calendar call failed")
            require(len(context.user_data.get("chat_history", [])) == history_before + 2,
                    f"{case_id}: handler did not complete the conversational turn")
            require(bool(entry["outputs"]), f"{case_id}: no delivered backend output")
            entry["automated_checks_status"] = "passed"
        except Exception as error:
            entry.update(automated_checks_status="failed", error=str(error))
            raise
        finally:
            entry["latency_seconds"] = round(time.monotonic() - started, 3)
            self.save()

    def check(self, label, condition):
        """Record application-state assertions separately from successful HTTP calls."""
        self.report["checks"].append({"name": label, "passed": bool(condition)})
        self.save()
        require(condition, label)


def write_context(directory, files):
    """Install explicit scenario inputs, one file at a time, in isolated storage."""
    for name, content in files.items():
        # These are controlled test inputs, not fabricated model/tool outputs.
        (directory / name).write_text(content, encoding="utf-8")


async def scenarios(run, context_dir, dataset, *, advice_only=False, workflow_only=False):
    """Exercise a live draft lifecycle and the controlled golden advice pair."""
    from telegram.ext import Application
    from persistence.database import get_db
    from orchestrator.time_utils import USER_TIMEZONE

    # Use the real job queue, left unstarted so a background inactivity synthesis
    # cannot spend money outside a scripted turn. Telegram transport is not started.
    application = Application.builder().token("1:local-presentation-only").build()

    def new_context():
        """Start each independent scenario without another scenario's session cache."""
        return SimpleNamespace(user_data={}, bot_data={"allowed_user_id": 1},
                               bot=LocalChat(), job_queue=application.job_queue)

    def items():
        """Read actual durable proposal records, not an expected in-memory copy."""
        return list(get_db()["proposal_items"].rows)

    def has_controls(message, item_id):
        """Check that both visible actions target this exact durable proposal."""
        if message.reply_markup is None:
            return False
        callbacks = {button.callback_data for row in message.reply_markup.inline_keyboard for button in row}
        return callbacks == {f"confirm_item_{item_id}", f"reject_item_{item_id}"}

    revised = []
    if not advice_only:
        # A failed workflow must stay failed. The advice-only mode runs independent
        # cases afterward without retrying a stochastic failure until it passes.
        day = (datetime.now(USER_TIMEZONE) + timedelta(days=2)).date().isoformat()
        write_context(context_dir, {"goals.md": "# Goals\nComplete a bounded David evaluation.\n",
                                   "weekly_state.md": "# Weekly State\nNo extra commitments supplied.\n",
                                   "decision_log.md": "# Decisions\nNo previous decisions.\n"})
        context = new_context()
        await run.turn(context, "S03-live", f"Draft exactly one event named David evaluation on {day} "
                       "from 09:00 to 09:30 America/Toronto on my primary calendar. Use this exact time; "
                       "this is an unconfirmed test proposal, even if it overlaps something.", "create_draft")
        first = items()
        run.report["proposal_snapshots"].append({"after": "create", "items": first})
        expected_start = datetime.fromisoformat(day + "T09:00:00").replace(tzinfo=USER_TIMEZONE)
        run.check("One active proposal persisted with the requested title and local window",
                  len(first) == 1 and first[0]["status"] == "active" and first[0]["summary"] == "David evaluation"
                  and first[0]["action_type"] == "schedule" and first[0]["calendar_id"] == "primary"
                  and datetime.fromisoformat(first[0]["start_time"]) == expected_start
                  and datetime.fromisoformat(first[0]["end_time"]) == expected_start + timedelta(minutes=30))
        original_id = first[0]["id"]
        old_message = context.bot.messages[-1]
        run.check("Proposal exposes correct Confirm and Reject controls", has_controls(old_message, original_id))

        await run.turn(context, "S07-live", "Before deciding, explain the tradeoff of making this 45 minutes. "
                       "Keep the current draft unchanged; I only want advice.", "discuss")
        run.check("Discussion preserves the entire pending proposal", items() == first)

        await run.turn(context, "S04-live", "I've changed my mind: revise that same unconfirmed David evaluation "
                       "draft to 10:00–10:45 on the same date and calendar. Keep its title.", "revise_draft")
        revised = items()
        run.report["proposal_snapshots"].append({"after": "revise", "items": revised})
        run.check("Revision targets the existing proposal ID",
                  run.report["turns"][-1]["route"].get("target_id") == original_id)
        run.check("Revision updates one durable draft to the new window",
                  len(revised) == 1 and revised[0]["id"] == original_id and revised[0]["status"] == "active"
                  and revised[0]["summary"] == "David evaluation"
                  and revised[0]["action_type"] == "schedule" and revised[0]["calendar_id"] == "primary"
                  and revised[0]["revision_count"] == first[0]["revision_count"] + 1
                  and datetime.fromisoformat(revised[0]["start_time"]) == expected_start + timedelta(hours=1)
                  and datetime.fromisoformat(revised[0]["end_time"]) == expected_start + timedelta(hours=1, minutes=45))
        run.check("Old confirmation controls are retired", old_message.reply_markup is None)
        run.check("Revised proposal exposes correct controls", has_controls(context.bot.messages[-1], original_id))

        await run.turn(context, "S06-live", "yes", "discuss")
        run.check("Text yes does not confirm or mutate the proposal", items() == revised)
        run.check("Text yes does not queue a Calendar write", get_db()["calendar_writes"].count == 0)

    if workflow_only:
        return

    advice_calendar = None
    for case_id in ("S15", "S16"):
        # These cases differ only in the prioritized outcome. Check that real
        # Calendar reads did not change between them; do not inject a fixed list.
        case = next(case for case in dataset["cases"] if case["id"] == case_id)
        deadline = (datetime.now(USER_TIMEZONE) + timedelta(days=5)).date().isoformat()
        write_context(context_dir, {name: content.replace("2026-09-25", deadline)
                                   for name, content in case["fixture"]["context_files"].items()})
        advice_context = new_context()
        await run.turn(advice_context, case_id + "-live", case["steps"][0]["text"], "discuss")
        calendar_digest = hashlib.sha256(json.dumps(advice_context.user_data.get("cached_events"), sort_keys=True).encode()).hexdigest()
        if advice_calendar is not None:
            run.check("Advice pair saw the same real Calendar contents", advice_calendar == calendar_digest)
        advice_calendar = calendar_digest
        run.report["turns"][-1]["live_deadline"] = deadline
        run.report["turns"][-1]["human_review_rubric"] = [
            assertion for assertion in case["assertions"] if assertion["kind"] in {"semantic", "plan"}
        ]
        run.check(case_id + " advice creates no additional proposal or write",
                  items() == revised and get_db()["calendar_writes"].count == 0)
    run.check("Calendar events were actually read successfully", any(
        entry["operation"] == "calendar.events.list" and entry["status"] == "success"
        for entry in run.report["calendar_requests"]))


async def expanded_scenarios(run, context_dir, prompt_dir, dataset):
    """Run independent real-handler cases, retaining failures without retrying them.

    Scenario inputs are synthetic; models, Calendar reads and application state
    are real. Only conversation transport is local. Date fixtures move forward
    with the live clock, and the exact adapted inputs remain in the report.
    """
    from telegram.ext import Application
    from persistence.database import get_db, init_db
    from orchestrator.time_utils import USER_TIMEZONE

    application = Application.builder().token("1:local-presentation-only").build()
    prompt_path = prompt_dir / "system_prompt.txt"
    prompt_path.write_text(prompt_path.read_text() + "\n\n" + ADVICE_APPENDIX)
    run.chat_output_cap = 8192
    run.report["experiment"] = {
        "prompt": prompt_path.read_text(), "output_cap": 8192,
        "quality_policy": "Assess usefulness, goal/constraint fidelity and grounding; no required wording or checklist.",
        "limitations": ["One sample per case; exploratory, not a calibrated regression gate",
                        "Local Telegram presentation; no Calendar writes or injected tool failures",
                        "Synthetic context with real Calendar reads; production clock is unchanged"],
    }
    run.report["case_runs"] = []
    today = datetime.now(USER_TIMEZONE).date()
    next_monday = today + timedelta(days=7 - today.weekday())
    neutral = {name: "# Evaluation context\nNo additional goals or constraints supplied.\n"
               for name in ("goals.md", "weekly_state.md", "decision_log.md")}

    def setup(case_id, files):
        """Isolate each case's real database and conversation without seeding outputs."""
        os.environ["DAVID_DB_PATH"] = str(context_dir.parent / f"{case_id}.db")
        init_db()
        write_context(context_dir, files)
        entry = {"id": case_id, "fixture_files": files, "status": "running"}
        run.report["case_runs"].append(entry)
        run.save()
        return SimpleNamespace(user_data={}, bot_data={"allowed_user_id": 1},
                               bot=LocalChat(), job_queue=application.job_queue), entry

    def finish(entry, error=None):
        """Keep application failures and proceed only if all real calls are accounted for."""
        entry["status"] = "failed" if error else "passed"
        if error:
            entry["error"] = str(error)
        run.save()
        print(json.dumps({"case": entry["id"], "status": entry["status"]}), flush=True)
        require(all(call["status"] == "reported" for call in run.report["calls"]),
                "Stop after any model transport/accounting failure; do not retry")
        require(all(call["status"] == "success" for call in run.report["calendar_requests"]),
                "Stop after an unexpected real Calendar failure")
        if error and "Budget" in str(error):
            raise error

    for case_id in ("S10", "S11", "S12", "S13", "S14", "S15", "S16", "S21", "S25"):
        # Shift dated goals instead of mocking time; preserve each case's intended
        # horizon and record the complete adapted context before model execution.
        case = next(case for case in dataset["cases"] if case["id"] == case_id)
        files = dict(case["fixture"].get("context_files", neutral))
        replacements = {"2026-09-25": (today + timedelta(days=5)).isoformat()}
        if case_id in {"S13", "S14"}:
            replacements["2026-09-22" if case_id == "S13" else "2026-11-16"] = (
                today + timedelta(days=1 if case_id == "S13" else 56)).isoformat()
        if case_id == "S25":
            replacements.update({"September 21": next_monday.strftime("%B %d").replace(" 0", " "),
                                 "September 25": (next_monday + timedelta(days=4)).strftime("%B %d").replace(" 0", " ")})
        for old, new in replacements.items():
            # Only fixture inputs change; no model or Calendar response is supplied.
            files = {name: content.replace(old, new) for name, content in files.items()}
        context, entry = setup(case_id, files)
        entry["source_criteria"] = case["assertions"]
        error = None
        try:
            await run.turn(context, case_id + "-expanded", case["steps"][0]["text"], ("discuss", "clarify"))
            if case_id == "S10":
                # This extension always supplies a follow-up, even if the first
                # answer was useful without asking a question. Label that adaptation.
                entry["adaptation"] = "Unconditional user follow-up tests new information in conversation."
                await run.turn(context, "S10-followup", case["steps"][1]["text"], ("discuss", "clarify"))
            if case_id == "S15":
                # An explicit new goal must override older fixture context while
                # preserving the same time budget and read-only advice behavior.
                await run.turn(context, "S15-mind-change", "I've changed my mind: evaluating the framework is now my priority. "
                               "There is no mandatory release deadline. I still have only two hours. What should I do?",
                               ("discuss", "clarify"))
            run.check(case_id + " advice leaves proposals and Calendar writes empty",
                      get_db()["proposal_items"].count == 0 and get_db()["calendar_writes"].count == 0)
        except Exception as caught:
            error = caught
        finish(entry, error)

    for case_id in ("S31", "S33"):
        # Drive both sides of clarification through the handler; a failed first
        # step stops only its dependent continuation, never rerolls the answer.
        case = next(case for case in dataset["cases"] if case["id"] == case_id)
        context, entry = setup(case_id, neutral)
        entry["adaptation"] = "Live clock; original future event dates retained."
        error = None
        try:
            await run.turn(context, case_id + "-ask", case["steps"][0]["text"], ("clarify", "discuss", "create_draft"))
            run.check(case_id + " does not create a confirmable proposal before clarification",
                      get_db()["proposal_items"].count == 0)
            await run.turn(context, case_id + "-resolve", case["steps"][1]["text"], "create_draft")
            expected = next(item["equals"] for item in case["assertions"] if item["kind"] == "proposal")
            items = list(get_db()["proposal_items"].rows)
            run.check(case_id + " produces one active draft at the clarified time",
                      len(items) == 1 and items[0]["status"] == "active"
                      and items[0]["action_type"] == "schedule" and items[0]["calendar_id"] == "primary"
                      and items[0]["start_time"] == expected["start_time"]
                      and items[0]["end_time"] == expected["end_time"])
            run.check(case_id + " leaves Calendar writes empty", get_db()["calendar_writes"].count == 0)
        except Exception as caught:
            error = caught
        finish(entry, error)

    _, entry = setup("draft-lifecycle", neutral)
    error = None
    try:
        await scenarios(run, context_dir, dataset, workflow_only=True)
    except Exception as caught:
        error = caught
    finish(entry, error)
    run.check("Expanded suite actually read Calendar events", any(
        request["operation"] == "calendar.events.list" for request in run.report["calendar_requests"]))
    run.check("All independent expanded cases passed automated checks", all(
        case["status"] == "passed" for case in run.report["case_runs"]))


def compare_advice(run, context_dir, prompt_dir, dataset):
    """Compare real chat generations against frozen, actually observed context.

    This isolates chat model/prompt effects; it is a component experiment, not a
    second claim of full handler E2E coverage. Routing runs once per case and its
    actual decision is held constant for every generation of that case.
    """
    from orchestrator.context_builder import build_context
    from orchestrator.time_utils import USER_TIMEZONE
    from reasoning.flash_client import generate_flash_response
    from reasoning.routing import route_turn

    original_prompt = (prompt_dir / "system_prompt.txt").read_text()
    variants = {"original": original_prompt, "advice_contract": original_prompt + "\n\n" + ADVICE_APPENDIX}
    state = {"drafts": [], "active_target_id": None, "can_create_draft": True}
    calendar_context = SimpleNamespace(user_data={})
    inputs = {}
    run.chat_output_cap = 8192
    run.report.update(scope="live chat component comparison; frozen real Calendar context; actual routing once per case",
                      experiment={"models": ["gemini-3.8-flash", "gemini-3-flash-preview"],
                                  "prompts": variants, "thinking": "HIGH", "output_cap": 8192,
                                  "temperature": 1, "shuffle_seed": 20260922,
                                  "model_order": "candidate first for conservative budget admission",
                                  "limitations": ["Small exploratory sample; not a calibrated quality gate",
                                                  "Model order is not randomized; latency can reflect service load",
                                                  "Both case pairs were inspected; no held-out claim"],
                                  "repeat_counts": {"S15": 2, "S16": 2, "S11": 1, "S12": 1}},
                      case_inputs=inputs)
    deadline = (datetime.now(USER_TIMEZONE) + timedelta(days=5)).date().isoformat()
    for case_id in ("S15", "S16", "S11", "S12"):
        # Capture full production context once, including real Calendar reads.
        # Reusing that observed snapshot removes calendar/time drift between cells.
        case = next(case for case in dataset["cases"] if case["id"] == case_id)
        files = {name: content.replace("2026-09-25", deadline)
                 for name, content in case["fixture"]["context_files"].items()}
        write_context(context_dir, files)
        text = case["steps"][0]["text"]
        decision = route_turn(text, [], state)
        require(decision.operation == "discuss", f"Cannot hold routing fixed: {case_id} did not route to discussion")
        block = build_context(calendar_context, profile="full")
        block += f"\n\n<TURN_ROUTING>\n{decision.model_dump_json()}\n</TURN_ROUTING>"
        block += f"\n\n<PENDING_DRAFTS>\n{json.dumps(state, ensure_ascii=False)}\n</PENDING_DRAFTS>"
        inputs[case_id] = {"text": text, "context_block": block, "route": decision.model_dump(),
                           "fixture_files": files,
                           "quality_criteria": [item for item in case["assertions"] if item["kind"] in {"semantic", "plan"}]}
        run.save()

    original_model = os.environ.get("GEMINI_CHAT_MODEL")
    try:
        for model in run.report["experiment"]["models"]:
            # Pair cells in a reproducible shuffled order within each model. The
            # candidate runs first because its full-input budget reserve is larger.
            jobs = [(case_id, variant, repeat) for case_id, repeats in run.report["experiment"]["repeat_counts"].items()
                    for variant in variants for repeat in range(1, repeats + 1)]
            random.Random(20260922).shuffle(jobs)
            os.environ["GEMINI_CHAT_MODEL"] = model
            for case_id, variant, repeat in jobs:
                (prompt_dir / "system_prompt.txt").write_text(variants[variant])
                entry = {"id": f"{model}/{variant}/{case_id}/{repeat}", "case_id": case_id,
                         "model": model, "prompt_variant": variant, "repeat": repeat,
                         "automated_checks_status": "running",
                         "quality_review": {"status": "not_evaluated", "reviewer": None}}
                run.report["turns"].append(entry)
                started = time.monotonic()
                try:
                    response = generate_flash_response(inputs[case_id]["text"], inputs[case_id]["context_block"],
                                                       chat_history=[], thinking_level="HIGH", allow_calendar_proposals=False)
                    entry["outputs"] = [response.message]
                    require(bool(response.message.strip()), "Chat response was empty")
                    require(response.proposal_thread is None, "Discussion unexpectedly produced a proposal")
                    entry["automated_checks_status"] = "passed"
                except Exception as error:
                    entry.update(automated_checks_status="failed", error=str(error))
                    raise
                finally:
                    entry["latency_seconds"] = round(time.monotonic() - started, 3)
                    run.save()
    finally:
        (prompt_dir / "system_prompt.txt").write_text(original_prompt)
        if original_model is None:
            os.environ.pop("GEMINI_CHAT_MODEL", None)
        else:
            os.environ["GEMINI_CHAT_MODEL"] = original_model

    run.check("All 24 planned chat comparisons completed", len(run.report["turns"]) == 24)
    run.check("All Calendar reads succeeded", bool(run.report["calendar_requests"]) and all(
        request["method"] == "GET" and request["status"] == "success" for request in run.report["calendar_requests"]))


def main():
    """Require opt-in, known providers, isolated state, and a private evidence file."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true", help="Make real paid model calls and Calendar reads")
    parser.add_argument("--advice-only", action="store_true", help="Run the two independent advice cases only")
    parser.add_argument("--compare-advice", action="store_true", help="Run the 24-generation model/prompt comparison")
    parser.add_argument("--expanded", action="store_true", help="Run more real handler cases with concise advice instructions")
    parser.add_argument("--budget-usd", type=float, default=0.95)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    require(sum((args.advice_only, args.compare_advice, args.expanded)) <= 1, "Choose one live suite")
    require(0 < args.budget_usd <= 1, "This smoke runner supports a maximum $1 per invocation")

    from config import get_model_name, get_routing_model
    from runtime_paths import get_google_token_path, get_prompt_path
    from google.oauth2.credentials import Credentials
    from loguru import logger

    require(version("google-genai") == "1.68.0", "Re-audit SDK retry semantics before running a different version")
    route_config = get_routing_model()
    require(route_config.provider == "gemini" and route_config.model in PRICES, "Routing model is not priced")
    require(get_model_name("chat") in PRICES, "Chat model is not priced")
    require(bool(os.getenv("GEMINI_API_KEY")), "GEMINI_API_KEY is required")
    token_path = get_google_token_path()
    require(token_path.exists(), "Existing Google Calendar authorization is required")
    credentials = Credentials.from_authorized_user_file(str(token_path))
    require(credentials.valid or bool(credentials.refresh_token),
            "Existing refreshable Google Calendar authorization is required")
    prompts = {name: get_prompt_path(name) for name in ("routing.txt", "system_prompt.txt")}
    dataset_path = ROOT / "evals/assistant_scenarios.json"
    dataset = json.loads(dataset_path.read_text())
    print("Local preflight passed: real model configuration, refreshable Calendar token, prompts, golden cases.")
    if not args.run:
        print("No network calls made. Add --run to execute the selected live backend turns.")
        return 0

    report_path = args.report or Path(tempfile.gettempdir()) / f"david-live-{time.time_ns()}.json"
    run = LiveRun(report_path.resolve(), args.budget_usd)
    run.report["selected_cases"] = ["S15-live", "S16-live"] if args.advice_only else [
        "S03-live", "S07-live", "S04-live", "S06-live", "S15-live", "S16-live"
    ]
    if args.compare_advice:
        run.report["selected_cases"] = ["S15", "S16", "S11", "S12"]
    if args.expanded:
        run.report["selected_cases"] = ["S10", "S11", "S12", "S13", "S14", "S15", "S16", "S21", "S25",
                                        "S31", "S33", "S03", "S07", "S04", "S06"]
    run.report.update(models={"routing": route_config.model, "chat": get_model_name("chat")},
                      dataset_sha256=hashlib.sha256(dataset_path.read_bytes()).hexdigest(),
                      python_version=sys.version, sdk_version=version("google-genai"),
                      source_sha256={str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
                                     for folder in ("bot", "config.py", "integrations", "observability", "orchestrator",
                                                    "persistence", "reasoning", "runtime_paths.py", "scripts/eval_assistant_live.py")
                                     for path in ([ROOT / folder] if (ROOT / folder).is_file() else sorted((ROOT / folder).rglob("*.py")))},
                      prompts={name: {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
                               for name, path in prompts.items()},
                      git_head=subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
                      tracked_diff_sha256=hashlib.sha256(subprocess.check_output(["git", "diff", "HEAD"], cwd=ROOT)).hexdigest())
    # Avoid logging personal Calendar titles or raw provider errors to the console.
    logger.remove()
    try:
        with tempfile.TemporaryDirectory(prefix="david-live-state-") as directory:
            root = Path(directory)
            context_dir = root / "context"
            context_dir.mkdir()
            prompt_dir = root / "reasoning" / "prompts"
            prompt_dir.mkdir(parents=True)
            for name, path in prompts.items():
                # Preserve the exact resolved production prompt even if deployment
                # uses a prompt directory outside the repository.
                shutil.copyfile(path, prompt_dir / name)
            private_token = root / "google-token.json"
            shutil.copyfile(token_path, private_token)
            private_token.chmod(0o600)
            if not credentials.valid:
                # Refresh actual authorization before spending on models. Failure
                # ends the run instead of opening an interactive OAuth flow.
                from google.auth.transport.requests import Request
                credentials.refresh(Request())
                private_token.write_text(credentials.to_json())
            run.report["calendar_auth"] = "valid"
            os.environ.update(DAVID_DB_PATH=str(root / "assistant.db"), DAVID_CONTEXT_DIR=str(context_dir),
                              GOOGLE_TOKEN_PATH=str(private_token), DAVID_TELEGRAM_PERSISTENCE_PATH=str(root / "telegram.pkl"))
            from persistence.database import init_db
            init_db()
            with run.observe():
                if args.expanded:
                    asyncio.run(expanded_scenarios(run, context_dir, prompt_dir, dataset))
                elif args.compare_advice:
                    compare_advice(run, context_dir, prompt_dir, dataset)
                else:
                    asyncio.run(scenarios(run, context_dir, dataset, advice_only=args.advice_only))
            run.report["status"] = "automated_checks_passed"
    except Exception as error:
        run.report.update(status="failed", overall_verdict="automated_checks_failed",
                          error_type=type(error).__name__, error=str(error))
    finally:
        run.report["ended_at"] = datetime.now(timezone.utc).isoformat()
        run.report["accounted_usd"] = sum(call["accounted_usd"] for call in run.report["calls"])
        run.save()
    print(json.dumps({"status": run.report["status"], "turns": len(run.report["turns"]),
                      "calls": len(run.report["calls"]), "accounted_usd": run.report["accounted_usd"],
                      "report": str(run.path), "advice_quality": run.report["advice_quality"]}))
    return 0 if run.report["status"] == "automated_checks_passed" else 1


if __name__ == "__main__":
    sys.exit(main())
