"""Serve the Mini App page and authenticated API for live context documents.

The bot must initialize the database before serving this application in its own
process. Bind it to localhost behind HTTPS; AppRunner lifecycle is supplied by
the bot integration. Only the fixed editor page is public.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import sqlite3
import uuid
from dataclasses import asdict
from pathlib import Path
from threading import Lock

from aiohttp import web
from loguru import logger
import sentry_sdk

from bot.context_editor_auth import (
    DEFAULT_MAX_AGE_SECONDS,
    ContextEditorAuthError,
    validate_init_data,
)
from orchestrator.artifact_writes import create_artifact_write, execute_artifact_write
from orchestrator.review_manager import validate_goals_markdown, validate_weekly_state_markdown
from persistence.context_files import (
    ContextFile,
    list_context_versions,
    read_context_file,
    read_context_version,
)
from persistence.models import ArtifactType, ArtifactWriteSourceType, ArtifactWriteStatus


MAX_DOCUMENT_BYTES = 256 * 1024
_EDITOR_PAGE_PATH = Path(__file__).with_name("context_editor.html")
_DOCUMENT_LABELS = {
    ArtifactType.GOALS: "Goals",
    ArtifactType.WEEKLY_STATE: "This week",
    ArtifactType.DECISION_LOG: "Memory",
}


def _document_payload(document: ContextFile) -> dict:
    return {
        "artifact_type": document.artifact_type.value,
        "label": _DOCUMENT_LABELS[document.artifact_type],
        "content": document.content,
        "revision": document.revision,
        "exists": document.exists,
    }


def _error_response(code: str, message: str, *, status: int, **details) -> web.Response:
    return web.json_response({"error": code, "message": message, **details}, status=status)


def _bad_request(message: str) -> web.HTTPBadRequest:
    return web.HTTPBadRequest(
        text=json.dumps({"error": "invalid_request", "message": message}),
        content_type="application/json",
    )


def _artifact_type(request: web.Request) -> ArtifactType:
    try:
        return ArtifactType(request.match_info["artifact_type"])
    except ValueError:
        raise web.HTTPNotFound() from None


async def _write_payload(request: web.Request, *, restoring: bool) -> dict:
    if request.content_type != "application/json":
        raise web.HTTPUnsupportedMediaType()
    try:
        payload = await request.json()
    except (ValueError, UnicodeError, RecursionError):
        raise _bad_request("Send a JSON object.") from None
    required = {"operation_id", "expected_revision", "version_id" if restoring else "content"}
    if not isinstance(payload, dict) or set(payload) != required:
        raise _bad_request("Send exactly these fields: " + ", ".join(sorted(required)))

    operation_id = payload["operation_id"]
    try:
        if not isinstance(operation_id, str) or str(uuid.UUID(operation_id)) != operation_id:
            raise ValueError
    except ValueError:
        raise _bad_request("operation_id must be a canonical UUID.") from None
    revision = payload["expected_revision"]
    if not isinstance(revision, str) or re.fullmatch(r"missing|[0-9a-f]{64}", revision) is None:
        raise _bad_request("expected_revision must be the revision returned when loading the document.")
    if restoring:
        if not isinstance(payload["version_id"], str) or len(payload["version_id"]) > 128:
            raise _bad_request("version_id must identify a listed previous version.")
    elif not isinstance(payload["content"], str):
        raise _bad_request("content must be Markdown text.")
    return payload


def _validate_document(artifact_type: ArtifactType, content: str) -> None:
    try:
        size = len(content.encode("utf-8"))
    except UnicodeError:
        raise _bad_request("The document must contain valid UTF-8 text.") from None
    if size > MAX_DOCUMENT_BYTES:
        raise web.HTTPRequestEntityTooLarge(max_size=MAX_DOCUMENT_BYTES, actual_size=size)
    if not content.strip() or "\x00" in content:
        raise _bad_request("The document cannot be empty or contain null characters.")
    try:
        if artifact_type == ArtifactType.GOALS:
            validate_goals_markdown(content)
        elif artifact_type == ArtifactType.WEEKLY_STATE:
            validate_weekly_state_markdown(content)
        else:
            # Memory compaction locates these exact lines and requires this order.
            lines = content.splitlines()
            title = lines.index("# Decision Log")
            rolling = lines.index("## Current Rolling Context")
            recent = lines.index("## Recent Decisions (Appended Daily)")
            if not title < rolling < recent:
                raise ValueError
    except ValueError:
        message = (
            "Keep the document's required section headings in place. "
            "Memory requires # Decision Log, ## Current Rolling Context, and "
            "## Recent Decisions (Appended Daily), in that order."
            if artifact_type == ArtifactType.DECISION_LOG
            else "Keep the document's required section headings and avoid headings from other documents."
        )
        raise _bad_request(message) from None


class _ContextEditor:
    def __init__(self, bot_token: str, allowed_user_id: int, max_age_seconds: int) -> None:
        self.bot_token = bot_token
        self.allowed_user_id = allowed_user_id
        self.max_age_seconds = max_age_seconds
        self.write_lock = Lock()

    @web.middleware
    async def private_telemetry(self, request: web.Request, handler) -> web.StreamResponse:
        # SDK integrations can collect request bodies and worker frame locals.
        # Keep editor credentials and private drafts out of exception telemetry.
        with sentry_sdk.new_scope() as scope:
            scope.add_event_processor(lambda event, hint: None)
            return await handler(request)

    @web.middleware
    async def authenticate(self, request: web.Request, handler) -> web.StreamResponse:
        try:
            public_page = (
                request.method in {"GET", "HEAD"}
                and request.match_info.route.name == "context_editor_page"
            )
            if not public_page:
                authorization = request.headers.getall("Authorization", [])
                if len(authorization) != 1 or not authorization[0].startswith("tma "):
                    raise ContextEditorAuthError("Open the editor from David in Telegram.")
                validate_init_data(
                    authorization[0][4:],
                    bot_token=self.bot_token,
                    allowed_user_id=self.allowed_user_id,
                    max_age_seconds=self.max_age_seconds,
                )
            response = await handler(request)
        except ContextEditorAuthError as error:
            response = _error_response("unauthorized", str(error), status=401)
        except web.HTTPException as error:
            if error.content_type == "application/json":
                response = web.Response(
                    text=error.text, status=error.status, headers=error.headers,
                )
            else:
                response = _error_response("invalid_request", error.reason, status=error.status)
        except (OSError, UnicodeError, sqlite3.Error, ValueError, RuntimeError) as error:
            logger.error("Context editor storage request failed: {}", type(error).__name__)
            response = _error_response(
                "storage_unavailable", "Context is temporarily unavailable. Keep your draft and retry.",
                status=503, retryable=True,
            )
        except Exception as error:
            logger.error("Context editor request failed: {}", type(error).__name__)
            response = _error_response("server_error", "The editor could not complete this request.", status=500)
        response.headers["Cache-Control"] = "no-store"
        response.headers["Pragma"] = "no-cache"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    async def page(self, request: web.Request) -> web.Response:
        html = await asyncio.to_thread(_EDITOR_PAGE_PATH.read_text, encoding="utf-8")
        return web.Response(text=html, content_type="text/html")

    async def documents(self, request: web.Request) -> web.Response:
        def load_documents() -> list[dict]:
            return [_document_payload(read_context_file(artifact)) for artifact in _DOCUMENT_LABELS]

        documents = await asyncio.to_thread(load_documents)
        return web.json_response({"documents": documents, "max_document_bytes": MAX_DOCUMENT_BYTES})

    async def document(self, request: web.Request) -> web.Response:
        document = await asyncio.to_thread(read_context_file, _artifact_type(request))
        return web.json_response({"document": _document_payload(document)})

    async def versions(self, request: web.Request) -> web.Response:
        versions = await asyncio.to_thread(list_context_versions, _artifact_type(request))
        return web.json_response({"versions": [asdict(version) for version in versions]})

    async def version(self, request: web.Request) -> web.Response:
        document = await asyncio.to_thread(
            self._read_version, _artifact_type(request), request.match_info["version_id"],
        )
        return web.json_response({
            "document": _document_payload(document), "version_id": request.match_info["version_id"],
        })

    @staticmethod
    def _read_version(artifact_type: ArtifactType, version_id: str) -> ContextFile:
        try:
            return read_context_version(artifact_type, version_id)
        except FileNotFoundError:
            raise web.HTTPNotFound() from None
        except ValueError:
            raise _bad_request("Select a valid previous version for this document.") from None

    def _write(self, artifact_type: ArtifactType, payload: dict, *, restoring: bool) -> web.Response:
        # Serialize ledger creation as well as execution. A thread lock remains
        # held if a disconnected HTTP handler is cancelled while its worker runs.
        with self.write_lock:
            content = (
                self._read_version(artifact_type, payload["version_id"]).content
                if restoring else payload["content"]
            )
            _validate_document(artifact_type, content)
            try:
                record = create_artifact_write(
                    artifact_type=artifact_type,
                    content=content,
                    source_type=ArtifactWriteSourceType.MANUAL_EDIT,
                    source_id=f"context_editor:{self.allowed_user_id}:{payload['operation_id']}",
                    expected_revision=payload["expected_revision"],
                )
            except ValueError:
                return _error_response(
                    "operation_conflict", "Use a new operation_id for a changed draft or revision.", status=409,
                )
            record = execute_artifact_write(record)
            details = {
                "operation_id": payload["operation_id"],
                "write_id": record.id,
                "write_status": record.status.value,
            }
            if record.status == ArtifactWriteStatus.FAILED_RETRYABLE:
                return _error_response(
                    "save_failed", "The save could not finish. Retry the same request with the same operation_id.",
                    status=503, retryable=True, **details,
                )
            current = _document_payload(read_context_file(artifact_type))
            if record.status == ArtifactWriteStatus.FAILED_TERMINAL:
                return _error_response(
                    "revision_conflict", "This document changed. Compare your draft with the current version before saving.",
                    status=409, current=current, retryable=False, **details,
                )
            if record.status != ArtifactWriteStatus.EXECUTED:
                return _error_response(
                    "write_in_progress", "This write is awaiting recovery. Keep your draft and reopen the editor later.",
                    status=503, retryable=True, **details,
                )
            # A replay reports today's document separately from the revision
            # this operation saved, so it never presents a newer edit as its own.
            return web.json_response({
                **details, "document": current,
                "saved_revision": hashlib.sha256(content.encode("utf-8")).hexdigest(),
            })

    async def save(self, request: web.Request) -> web.Response:
        artifact_type = _artifact_type(request)
        payload = await _write_payload(request, restoring=False)
        return await asyncio.to_thread(self._write, artifact_type, payload, restoring=False)

    async def restore(self, request: web.Request) -> web.Response:
        artifact_type = _artifact_type(request)
        payload = await _write_payload(request, restoring=True)
        return await asyncio.to_thread(self._write, artifact_type, payload, restoring=True)


def create_context_editor_app(
    *, bot_token: str, allowed_user_id: int, max_age_seconds: int = DEFAULT_MAX_AGE_SECONDS,
) -> web.Application:
    """Create the editor and API without starting a listener or initializing storage.

    GET and HEAD /context serve only the public editor HTML, with no private
    context embedded. Every API request requires Authorization: tma followed
    by raw Telegram.WebApp.initData.
    POST /api/context/{artifact_type} accepts content, expected_revision, and a
    canonical UUID operation_id. POST /api/context/{artifact_type}/restore uses
    version_id instead of content. Keep the operation ID and payload unchanged
    for transport/storage retries; use a new ID for a revised draft or base.
    Saves return 409 on conflict, 503 for retryable failures, and the current
    document plus the operation's saved_revision after successful execution.
    Only the allowed Telegram user can read documents, history, or save changes.
    """
    if not isinstance(bot_token, str) or not bot_token:
        raise ValueError("The context editor requires a bot token.")
    if type(allowed_user_id) is not int or allowed_user_id <= 0:
        raise ValueError("The context editor requires a positive allowed_user_id.")
    if type(max_age_seconds) is not int or max_age_seconds <= 0:
        raise ValueError("max_age_seconds must be a positive integer.")
    editor = _ContextEditor(bot_token, allowed_user_id, max_age_seconds)
    # Escaped JSON can be larger than its decoded Markdown. Bound both sizes.
    app = web.Application(
        middlewares=[editor.private_telemetry, editor.authenticate],
        client_max_size=2 * 1024 * 1024,
    )
    prefix = "/api/context/{artifact_type}"
    app.add_routes([
        web.get("/context", editor.page, name="context_editor_page"),
        web.get("/api/context", editor.documents),
        web.get(prefix, editor.document),
        web.post(prefix, editor.save),
        web.get(prefix + "/versions", editor.versions),
        web.get(prefix + "/versions/{version_id}", editor.version),
        web.post(prefix + "/restore", editor.restore),
    ])
    return app
