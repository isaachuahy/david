"""Exercise the Mini App's HTTP boundary using synthetic context and credentials."""

import asyncio
import hashlib
import hmac
import json
import time
import uuid
from pathlib import Path
from unittest.mock import Mock
from urllib.parse import urlencode

from aiohttp.test_utils import TestClient, TestServer
import pytest
import pytest_asyncio
import sentry_sdk

from bot.context_editor_server import MAX_DOCUMENT_BYTES, create_context_editor_app
from persistence.artifact_writes import load_artifact_write_sync
from persistence.context_files import (
    list_context_versions,
    read_context_file,
    replace_context_file,
)
from persistence.database import get_db, init_db
from persistence.models import ArtifactType, ArtifactWriteSourceType, ArtifactWriteStatus


BOT_TOKEN = "12345:synthetic-http-test-token"
USER_ID = 123
DOCUMENTS = {
    ArtifactType.GOALS: (
        "# Goals\r\n\r\n## Long-Term\r\nCafé\r\n"
        "## Medium-Term\r\nPractice\r\n## Operating Principles\r\nFocus\r\n"
    ),
    ArtifactType.WEEKLY_STATE: (
        "# Weekly State\n\n## This Week\n\n### Top Priorities\nPractice\n"
        "### Carryover\nNone\n### Constraints\nTime\n### Execution Focus\nFocus\n"
    ),
    ArtifactType.DECISION_LOG: (
        "# Decision Log\n\n## Current Rolling Context\n- Practice daily.\n\n"
        "## Recent Decisions (Appended Daily)\n- Keep a weekly plan.\n"
    ),
}


def _headers(*, user_id=USER_ID, age=0, token=BOT_TOKEN):
    fields = {"auth_date": str(int(time.time()) - age), "user": json.dumps({"id": user_id})}
    secret = hmac.digest(b"WebAppData", token.encode(), "sha256")
    message = "\n".join(f"{key}={value}" for key, value in sorted(fields.items()))
    fields["hash"] = hmac.digest(secret, message.encode(), "sha256").hex()
    return {"Authorization": "tma " + urlencode(fields)}


def _save_payload(document, **changes):
    return {
        "operation_id": str(uuid.uuid4()),
        "expected_revision": document.revision,
        "content": document.content + "\n- Edited in Telegram.\n",
        **changes,
    }


def _endpoint(document):
    return f"/api/context/{document.artifact_type.value}"


@pytest.fixture
def context_runtime(tmp_path, monkeypatch):
    """Keep all requests away from private files, the live database, and telemetry."""
    directory = tmp_path / "context"
    directory.mkdir()
    monkeypatch.setenv("DAVID_CONTEXT_DIR", str(directory))
    monkeypatch.setenv("DAVID_DB_PATH", str(tmp_path / "assistant.db"))
    monkeypatch.setattr("orchestrator.artifact_writes.capture_sentry_exception", lambda *args, **kwargs: None)
    init_db()
    for artifact_type, content in DOCUMENTS.items():
        (directory / f"{artifact_type.value}.md").write_bytes(content.encode())
    return directory


@pytest.fixture(params=list(DOCUMENTS), ids=lambda artifact: artifact.value)
def document(context_runtime, request):
    return read_context_file(request.param)


@pytest_asyncio.fixture
async def client(context_runtime):
    app = create_context_editor_app(bot_token=BOT_TOKEN, allowed_user_id=USER_ID)
    client = TestClient(TestServer(app))
    try:
        await client.start_server()
        yield client
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_editor_telemetry_drops_private_events_in_worker_and_preserves_other_events(
    client, monkeypatch,
):
    events = []

    class RecordingTransport(sentry_sdk.transport.Transport):
        def capture_envelope(self, envelope):
            events.extend(item.get_event() for item in envelope.items if item.type == "event")

    def load_private_document(artifact):
        sentry_sdk.capture_message("synthetic private draft")
        return read_context_file(artifact)

    monkeypatch.setattr("bot.context_editor_server.read_context_file", load_private_document)
    telemetry_client = sentry_sdk.Client(
        dsn="https://test@example.com/1", transport=RecordingTransport,
        default_integrations=False, auto_enabling_integrations=False,
    )
    global_scope = sentry_sdk.get_global_scope()
    previous_client = global_scope.client
    global_scope.set_client(telemetry_client)
    try:
        response = await client.get("/api/context", headers=_headers())
        assert response.status == 200
        assert events == []
        sentry_sdk.capture_message("unrelated bot event")
        sentry_sdk.flush()
        assert len(events) == 1
        assert events[0]["message"] == "unrelated bot event"
    finally:
        global_scope.set_client(previous_client)
        telemetry_client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("headers", [{}, {"Authorization": "invalid-launch"}])
async def test_public_page_contains_only_editor_html(client, context_runtime, monkeypatch, headers):
    storage = Mock(side_effect=AssertionError("The page must not read private context."))
    for name in ("read_context_file", "read_context_version", "list_context_versions", "create_artifact_write"):
        monkeypatch.setattr(f"bot.context_editor_server.{name}", storage)
    response = await client.get("/context?file=../../.env", headers=headers)
    expected = (Path(__file__).parents[1] / "bot" / "context_editor.html").read_bytes()
    assert response.status == 200
    assert await response.read() == expected
    assert response.content_type == "text/html"
    assert response.charset == "utf-8"
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["Referrer-Policy"] == "no-referrer"
    storage.assert_not_called()
    assert get_db()["artifact_writes"].count == 0


@pytest.mark.asyncio
async def test_public_page_supports_head_without_returning_html(client):
    response = await client.head("/context")
    assert response.status == 200
    assert response.content_type == "text/html"
    assert await response.read() == b""


@pytest.mark.asyncio
@pytest.mark.parametrize("method, path", [
    ("POST", "/context"),
    ("GET", "/context/context_editor_server.py"),
    ("GET", "/context_editor.html"),
    ("GET", "/api/context?route=context_editor_page"),
])
async def test_public_page_does_not_bypass_other_routes(client, method, path):
    response = await client.request(method, path)
    assert response.status == 401
    assert (await response.json())["error"] == "unauthorized"


@pytest.mark.asyncio
async def test_missing_page_returns_a_controlled_error(client, tmp_path, monkeypatch):
    missing = tmp_path / "missing.html"
    monkeypatch.setattr("bot.context_editor_server._EDITOR_PAGE_PATH", missing)
    response = await client.get("/context")
    assert response.status == 503
    body = await response.json()
    assert body["error"] == "storage_unavailable"
    assert str(missing) not in json.dumps(body)


@pytest.mark.asyncio
@pytest.mark.parametrize("method, endpoint", [
    ("GET", "/api/context"),
    ("GET", "/api/context/goals"),
    ("POST", "/api/context/goals"),
    ("GET", "/api/context/goals/versions"),
    ("GET", "/api/context/goals/versions/goals_backup_20261008_120000.md"),
    ("POST", "/api/context/goals/restore"),
])
@pytest.mark.parametrize("launch", ["missing", "wrong_scheme", "wrong_signature", "expired", "other_user", "duplicate"])
async def test_every_private_endpoint_authenticates_before_storage(client, context_runtime, monkeypatch, method, endpoint, launch):
    headers = {
        "missing": {},
        "wrong_scheme": {"Authorization": "Bearer " + _headers()["Authorization"][4:]},
        "wrong_signature": _headers(token="another-synthetic-token"),
        "expired": _headers(age=3601),
        "other_user": _headers(user_id=USER_ID + 1),
        "duplicate": [("Authorization", value) for value in [_headers()["Authorization"]] * 2],
    }[launch]
    storage = Mock(side_effect=AssertionError("Unauthenticated requests must not reach storage."))
    for name in ("read_context_file", "read_context_version", "list_context_versions", "create_artifact_write"):
        monkeypatch.setattr(f"bot.context_editor_server.{name}", storage)

    response = await client.request(method, endpoint, headers=headers, json={"user_id": USER_ID})

    assert response.status == 401
    body = await response.json()
    assert body["error"] == "unauthorized"
    assert BOT_TOKEN not in json.dumps(body)
    assert response.headers["Cache-Control"] == "no-store"
    storage.assert_not_called()
    assert get_db()["artifact_writes"].count == 0
    assert not list(context_runtime.glob("*_backup_*.md"))


@pytest.mark.asyncio
async def test_reads_preserve_exact_text_and_expose_only_managed_documents(client, document):
    response = await client.get("/api/context", headers=_headers())
    assert response.status == 200
    body = await response.json()
    assert {entry["artifact_type"] for entry in body["documents"]} == {artifact.value for artifact in DOCUMENTS}
    assert body["max_document_bytes"] == MAX_DOCUMENT_BYTES

    response = await client.get(_endpoint(document), headers=_headers())
    current = (await response.json())["document"]
    assert response.status == 200
    assert current["content"] == document.content
    assert current["revision"] == hashlib.sha256(document.content.encode()).hexdigest()
    assert current["exists"] is True
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert get_db()["artifact_writes"].count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("exists", [False, True], ids=["missing", "empty"])
async def test_reads_distinguish_missing_and_empty_files(client, context_runtime, exists):
    path = context_runtime / "goals.md"
    if exists:
        path.write_bytes(b"")
    else:
        path.unlink()
    response = await client.get("/api/context/goals", headers=_headers())
    current = (await response.json())["document"]
    assert current["content"] == ""
    assert current["exists"] is exists
    assert current["revision"] == (hashlib.sha256(b"").hexdigest() if exists else "missing")


@pytest.mark.asyncio
async def test_save_backs_up_exact_text_and_replay_does_not_repeat_writes(client, context_runtime, document):
    payload = _save_payload(document)
    response = await client.post(_endpoint(document), headers=_headers(), json=payload)
    assert response.status == 200
    saved = await response.json()
    assert saved["write_status"] == "executed"
    assert saved["document"]["content"] == payload["content"]
    assert saved["saved_revision"] == hashlib.sha256(payload["content"].encode()).hexdigest()
    record = load_artifact_write_sync(saved["write_id"])
    assert record.expected_revision == document.revision
    assert record.source_type == ArtifactWriteSourceType.MANUAL_EDIT
    assert record.content == payload["content"]
    assert record.attempts == 1

    response = await client.post(_endpoint(document), headers=_headers(), json=payload)
    assert response.status == 200
    assert (await response.json())["write_id"] == saved["write_id"]
    backups = list(context_runtime.glob(f"{document.artifact_type.value}_backup_*.md"))
    assert len(backups) == 1
    assert backups[0].read_bytes() == document.content.encode()
    assert (context_runtime / f"{document.artifact_type.value}.md").read_bytes() == payload["content"].encode()
    assert get_db()["artifact_writes"].count == 1
    assert get_db()["weekly_snapshots"].count == int(document.artifact_type == ArtifactType.WEEKLY_STATE)


@pytest.mark.asyncio
async def test_completed_operation_reports_newer_context_without_replaying(client, document):
    payload = _save_payload(document)
    saved = await (await client.post(_endpoint(document), headers=_headers(), json=payload)).json()
    newer = replace_context_file(document.artifact_type, document.content + "\nNewer note.\n", expected_revision=saved["saved_revision"])

    response = await client.post(_endpoint(document), headers=_headers(), json=payload)

    assert response.status == 200
    replay = await response.json()
    assert replay["saved_revision"] == saved["saved_revision"]
    assert replay["document"]["revision"] == newer.revision
    assert replay["document"]["content"] == newer.content
    assert read_context_file(document.artifact_type) == newer
    assert load_artifact_write_sync(saved["write_id"]).attempts == 1
    assert len(list_context_versions(document.artifact_type)) == 2


@pytest.mark.asyncio
async def test_simultaneous_identical_requests_create_one_durable_write(client, document):
    payload = _save_payload(document)
    responses = await asyncio.gather(*[
        client.post(_endpoint(document), headers=_headers(), json=payload) for _ in range(4)
    ])
    assert all(response.status == 200 for response in responses)
    bodies = [await response.json() for response in responses]
    assert len({body["write_id"] for body in bodies}) == 1
    assert get_db()["artifact_writes"].count == 1
    assert len(list_context_versions(document.artifact_type)) == 1
    assert get_db()["weekly_snapshots"].count == int(document.artifact_type == ArtifactType.WEEKLY_STATE)


@pytest.mark.asyncio
async def test_simultaneous_different_drafts_cannot_overwrite_each_other(client, document):
    payloads = [_save_payload(document, content=document.content + f"\nDraft {number}.\n") for number in range(2)]
    responses = await asyncio.gather(*[
        client.post(_endpoint(document), headers=_headers(), json=payload) for payload in payloads
    ])
    assert sorted(response.status for response in responses) == [200, 409]
    bodies = [await response.json() for response in responses]
    saved = next(body for body in bodies if "saved_revision" in body)
    conflict = next(body for body in bodies if "error" in body)
    assert conflict["error"] == "revision_conflict"
    assert conflict["current"] == saved["document"]
    assert read_context_file(document.artifact_type).revision == saved["saved_revision"]
    assert len(list_context_versions(document.artifact_type)) == 1
    assert get_db()["weekly_snapshots"].count == int(document.artifact_type == ArtifactType.WEEKLY_STATE)


@pytest.mark.asyncio
@pytest.mark.parametrize("changed_field", ["content", "expected_revision"])
async def test_operation_id_cannot_be_reused_for_a_changed_draft(client, document, changed_field):
    payload = _save_payload(document)
    saved = await (await client.post(_endpoint(document), headers=_headers(), json=payload)).json()
    changed = {**payload, changed_field: payload["content"] + "Changed" if changed_field == "content" else saved["saved_revision"]}
    response = await client.post(_endpoint(document), headers=_headers(), json=changed)
    assert response.status == 409
    assert (await response.json())["error"] == "operation_conflict"
    assert read_context_file(document.artifact_type).content == payload["content"]
    assert get_db()["artifact_writes"].count == 1
    assert len(list_context_versions(document.artifact_type)) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("intervening_edit", [False, True], ids=["retry-succeeds", "retry-conflicts"])
async def test_storage_retry_preserves_its_original_base(client, document, monkeypatch, intervening_edit):
    payload = _save_payload(document)
    with monkeypatch.context() as failure:
        failure.setattr("orchestrator.artifact_writes.replace_context_file", Mock(side_effect=OSError("Synthetic private storage error.")))
        response = await client.post(_endpoint(document), headers=_headers(), json=payload)
    assert response.status == 503
    failed = await response.json()
    assert failed["error"] == "save_failed"
    assert failed["retryable"] is True
    assert failed["operation_id"] == payload["operation_id"]
    assert "Synthetic private" not in json.dumps(failed)
    record = load_artifact_write_sync(failed["write_id"])
    assert record.expected_revision == document.revision
    assert record.status == ArtifactWriteStatus.FAILED_RETRYABLE
    assert read_context_file(document.artifact_type) == document
    assert not list_context_versions(document.artifact_type)
    if intervening_edit:
        newer = replace_context_file(document.artifact_type, document.content + "\nIntervening edit.\n", expected_revision=document.revision)

    response = await client.post(_endpoint(document), headers=_headers(), json=payload)
    body = await response.json()
    assert body["write_id"] == failed["write_id"]
    assert load_artifact_write_sync(failed["write_id"]).attempts == 2
    if intervening_edit:
        assert response.status == 409
        assert body["error"] == "revision_conflict"
        assert body["retryable"] is False
        assert read_context_file(document.artifact_type) == newer
        assert get_db()["weekly_snapshots"].count == 0
        replay = await client.post(_endpoint(document), headers=_headers(), json=payload)
        assert replay.status == 409
        assert load_artifact_write_sync(failed["write_id"]).attempts == 2
    else:
        assert response.status == 200
        assert read_context_file(document.artifact_type).content == payload["content"]
        assert get_db()["weekly_snapshots"].count == int(document.artifact_type == ArtifactType.WEEKLY_STATE)
    assert len(list_context_versions(document.artifact_type)) == 1


@pytest.mark.asyncio
async def test_retry_repairs_a_weekly_snapshot_failure_without_rewriting_the_file(client, monkeypatch):
    document = read_context_file(ArtifactType.WEEKLY_STATE)
    payload = _save_payload(document)
    with monkeypatch.context() as failure:
        failure.setattr("orchestrator.artifact_writes._persist_weekly_snapshot", Mock(side_effect=OSError("Synthetic snapshot failure.")))
        response = await client.post(_endpoint(document), headers=_headers(), json=payload)
    assert response.status == 503
    failed = await response.json()
    assert failed["write_status"] == "failed_retryable"
    assert read_context_file(ArtifactType.WEEKLY_STATE).content == payload["content"]
    assert get_db()["weekly_snapshots"].count == 0
    assert len(list_context_versions(ArtifactType.WEEKLY_STATE)) == 1

    response = await client.post(_endpoint(document), headers=_headers(), json=payload)

    assert response.status == 200
    assert (await response.json())["write_id"] == failed["write_id"]
    assert load_artifact_write_sync(failed["write_id"]).attempts == 2
    assert get_db()["weekly_snapshots"].count == 1
    snapshot = next(get_db()["weekly_snapshots"].rows)
    assert snapshot["weekly_state_content"] == payload["content"]
    assert len(list_context_versions(ArtifactType.WEEKLY_STATE)) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("intervening_edit", [False, True], ids=["restore-succeeds", "restore-conflicts"])
async def test_history_preview_and_restore_protect_the_current_revision(client, document, intervening_edit):
    edited = replace_context_file(document.artifact_type, document.content + "\nFirst edit.\n", expected_revision=document.revision)
    response = await client.get(_endpoint(document) + "/versions", headers=_headers())
    assert response.status == 200
    versions = (await response.json())["versions"]
    assert len(versions) == 1
    assert set(versions[0]) == {"version_id", "saved_at"}
    version_id = versions[0]["version_id"]
    response = await client.get(_endpoint(document) + "/versions/" + version_id, headers=_headers())
    assert response.status == 200
    assert (await response.json())["document"]["content"] == document.content
    assert read_context_file(document.artifact_type) == edited
    payload = {"operation_id": str(uuid.uuid4()), "expected_revision": edited.revision, "version_id": version_id}
    if intervening_edit:
        newer = replace_context_file(document.artifact_type, edited.content + "\nSecond edit.\n", expected_revision=edited.revision)

    response = await client.post(_endpoint(document) + "/restore", headers=_headers(), json=payload)
    body = await response.json()
    if intervening_edit:
        assert response.status == 409
        assert body["error"] == "revision_conflict"
        assert body["current"]["revision"] == newer.revision
        assert read_context_file(document.artifact_type) == newer
        assert get_db()["weekly_snapshots"].count == 0
    else:
        assert response.status == 200
        assert body["saved_revision"] == document.revision
        assert read_context_file(document.artifact_type) == document
        replay = await client.post(_endpoint(document) + "/restore", headers=_headers(), json=payload)
        assert replay.status == 200
        assert (await replay.json())["write_id"] == body["write_id"]
        assert get_db()["weekly_snapshots"].count == int(document.artifact_type == ArtifactType.WEEKLY_STATE)
    assert len(list_context_versions(document.artifact_type)) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("version_id, status", [
    ("goals.md", 400),
    ("decision_log_backup_20261008_120000.md", 400),
    ("goals_backup_20261008_120000.md", 404),
])
async def test_invalid_history_targets_cannot_be_read_or_restored(client, context_runtime, version_id, status):
    response = await client.get("/api/context/goals/versions/" + version_id, headers=_headers())
    assert response.status == status
    payload = {"operation_id": str(uuid.uuid4()), "expected_revision": read_context_file(ArtifactType.GOALS).revision, "version_id": version_id}
    response = await client.post("/api/context/goals/restore", headers=_headers(), json=payload)
    assert response.status == status
    assert get_db()["artifact_writes"].count == 0
    assert not list(context_runtime.glob("*_backup_*.md"))


@pytest.mark.asyncio
async def test_history_symlinks_cannot_expose_other_files(client, context_runtime):
    outside = context_runtime.parent / "private.md"
    outside.write_text("Synthetic private text.")
    version_id = "goals_backup_20261008_120000.md"
    (context_runtime / version_id).symlink_to(outside)
    response = await client.get("/api/context/goals/versions", headers=_headers())
    assert (await response.json())["versions"] == []
    response = await client.get("/api/context/goals/versions/" + version_id, headers=_headers())
    assert response.status == 400
    assert "Synthetic private text" not in await response.text()
    assert outside.read_text() == "Synthetic private text."


@pytest.mark.asyncio
@pytest.mark.parametrize("changes", [
    {"operation_id": None},
    {"operation_id": "not-a-uuid"},
    {"operation_id": "AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA"},
    {"expected_revision": None},
    {"expected_revision": ""},
    {"content": 123},
    {"content": ""},
    {"content": "   \n"},
    {"content": "\ud800"},
    {"content": DOCUMENTS[ArtifactType.GOALS] + "\x00"},
    {"content": "# Goals\nMissing required sections.\n"},
    {"content": DOCUMENTS[ArtifactType.GOALS] + "\n# Decision Log\n"},
    {"extra_field": True},
])
async def test_invalid_saves_do_not_create_writes(client, context_runtime, changes):
    document = read_context_file(ArtifactType.GOALS)
    response = await client.post(_endpoint(document), headers=_headers(), json=_save_payload(document, **changes))
    assert response.status == 400
    assert (await response.json())["error"] == "invalid_request"
    assert read_context_file(ArtifactType.GOALS) == document
    assert get_db()["artifact_writes"].count == 0
    assert not list_context_versions(ArtifactType.GOALS)


@pytest.mark.asyncio
async def test_each_document_requires_its_section_headings(client, document):
    payload = _save_payload(document, content=document.content.replace("# ", "", 1))
    response = await client.post(_endpoint(document), headers=_headers(), json=payload)
    assert response.status == 400
    assert read_context_file(document.artifact_type) == document
    assert get_db()["artifact_writes"].count == 0


@pytest.mark.asyncio
async def test_memory_requires_rolling_context_before_recent_decisions(client):
    document = read_context_file(ArtifactType.DECISION_LOG)
    content = "# Decision Log\n\n## Recent Decisions (Appended Daily)\n- Note.\n\n## Current Rolling Context\n- Memory.\n"
    response = await client.post(_endpoint(document), headers=_headers(), json=_save_payload(document, content=content))
    assert response.status == 400
    assert read_context_file(ArtifactType.DECISION_LOG) == document
    assert get_db()["artifact_writes"].count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("data, content_type, status", [
    ("{}", "text/plain", 415),
    ("not-json", "application/json", 400),
    ("[]", "application/json", 400),
    ("{}", "application/json", 400),
    (" " * (2 * 1024 * 1024 + 1), "application/json", 413),
])
async def test_invalid_or_oversized_request_bodies_are_rejected(client, data, content_type, status):
    response = await client.post("/api/context/goals", headers={**_headers(), "Content-Type": content_type}, data=data)
    assert response.status == status
    assert (await response.json())["error"] == "invalid_request"
    assert get_db()["artifact_writes"].count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("extra_bytes, status", [(0, 200), (1, 413)])
async def test_document_size_limit_is_measured_in_utf8_bytes(client, extra_bytes, status):
    document = read_context_file(ArtifactType.GOALS)
    remaining = MAX_DOCUMENT_BYTES - len(document.content.encode())
    content = document.content + "é" * (remaining // 2) + "x" * (remaining % 2 + extra_bytes)
    assert len(content.encode()) == MAX_DOCUMENT_BYTES + extra_bytes
    response = await client.post(_endpoint(document), headers=_headers(), json=_save_payload(document, content=content))
    assert response.status == status
    assert get_db()["artifact_writes"].count == int(status == 200)


@pytest.mark.asyncio
async def test_unknown_artifacts_and_wrong_methods_cannot_change_context(client, context_runtime):
    response = await client.get("/api/context/unknown", headers=_headers())
    assert response.status == 404
    response = await client.delete("/api/context/goals", headers=_headers())
    assert response.status == 405
    assert get_db()["artifact_writes"].count == 0
    assert (context_runtime / "goals.md").read_bytes() == DOCUMENTS[ArtifactType.GOALS].encode()


@pytest.mark.asyncio
async def test_storage_errors_do_not_expose_private_details(client, monkeypatch):
    monkeypatch.setattr("bot.context_editor_server.read_context_file", Mock(side_effect=OSError(f"Private path and token: {BOT_TOKEN}")))
    response = await client.get("/api/context/goals", headers=_headers())
    assert response.status == 503
    body = await response.json()
    assert body["error"] == "storage_unavailable"
    assert body["retryable"] is True
    assert BOT_TOKEN not in json.dumps(body)
    assert "Private path" not in json.dumps(body)
