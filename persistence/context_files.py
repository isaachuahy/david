"""Manage live Markdown context and its previous versions on the VPS.

The bot and Mini App will use this module for every context write. One
process-local lock keeps revision checks, session appends, and replacements
together; it does not coordinate external editors or separate processes.
"""

from __future__ import annotations

import hashlib
import os
import re
import tempfile
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock

from loguru import logger

from persistence.models import ArtifactType
from runtime_paths import get_context_dir


_FILENAMES = {
    ArtifactType.GOALS: "goals.md",
    ArtifactType.WEEKLY_STATE: "weekly_state.md",
    ArtifactType.DECISION_LOG: "decision_log.md",
}
_CONTEXT_LOCK = RLock()
_MISSING_REVISION = "missing"


@dataclass(frozen=True)
class ContextFile:
    """Pair exact document text with the revision an editor must submit to save."""

    artifact_type: ArtifactType
    content: str
    revision: str
    exists: bool


@dataclass(frozen=True)
class ContextVersion:
    """Describe a previous version without loading its potentially private text."""

    version_id: str
    saved_at: str


class ContextConflictError(RuntimeError):
    """Carry the current file when a draft was based on an outdated revision."""

    def __init__(self, current: ContextFile) -> None:
        self.current = current
        super().__init__(f"{current.artifact_type.value} changed since it was loaded.")


def _artifact_path(artifact_type: ArtifactType) -> Path:
    """Resolve only the three known artifacts, never a caller-supplied path."""
    return get_context_dir() / _FILENAMES[ArtifactType(artifact_type)]


def _read_path(artifact_type: ArtifactType, path: Path) -> ContextFile:
    """Preserve exact UTF-8 bytes so whitespace changes also change revisions."""
    if path.is_symlink():
        raise ValueError("Context files and previous versions cannot be symlinks.")
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return ContextFile(artifact_type, "", _MISSING_REVISION, False)
    return ContextFile(
        artifact_type=artifact_type,
        content=data.decode("utf-8"),
        revision=hashlib.sha256(data).hexdigest(),
        exists=True,
    )


def read_context_file(artifact_type: ArtifactType) -> ContextFile:
    """Read a live document and its revision as one consistent snapshot."""
    artifact_type = ArtifactType(artifact_type)
    with _CONTEXT_LOCK:
        return _read_path(artifact_type, _artifact_path(artifact_type))


def _sync_directory(directory: Path) -> None:
    """Flush filename changes when supported, matching existing write behavior."""
    try:
        directory_fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except OSError as error:
        logger.debug("Could not flush context directory {}: {}", directory, error)


def _backup_file(path: Path, content: str) -> None:
    """Save a private, unique backup before replacing the live document."""
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    backup = path.with_name(f"{path.stem}_backup_{timestamp}_{uuid.uuid4().hex}.md")
    descriptor = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as backup_file:
            backup_file.write(content.encode("utf-8"))
            backup_file.flush()
            os.fsync(backup_file.fileno())
        _sync_directory(path.parent)
    except Exception:
        # An incomplete backup must not appear as a usable restore target.
        backup.unlink(missing_ok=True)
        raise


def replace_context_file(
    artifact_type: ArtifactType,
    content: str,
    *,
    expected_revision: str,
) -> ContextFile:
    """Back up and atomically save exact text only if its base is still current.

    Markdown section validation belongs to the editor/review caller. This
    storage operation propagates I/O errors so callers cannot report a failed
    write as successful. Weekly snapshots and durable write records remain
    the responsibility of the existing artifact-write orchestrator.
    """
    artifact_type = ArtifactType(artifact_type)
    data = content.encode("utf-8")
    with _CONTEXT_LOCK:
        path = _artifact_path(artifact_type)
        current = _read_path(artifact_type, path)
        if current.revision != expected_revision:
            raise ContextConflictError(current)
        if current.exists and current.content == content:
            return current

        path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path: Path | None = None
        try:
            # Prepare a complete file on the same filesystem before publishing
            # it, so model context never sees a half-written editor save.
            with tempfile.NamedTemporaryFile(
                "wb", dir=path.parent, prefix=f".{path.name}.", suffix=".tmp",
                delete=False,
            ) as temporary_file:
                temporary_path = Path(temporary_file.name)
                temporary_file.write(data)
                temporary_file.flush()
                os.fsync(temporary_file.fileno())

            if current.exists:
                _backup_file(path, current.content)
            os.replace(temporary_path, path)
            temporary_path = None
            _sync_directory(path.parent)
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)

        return ContextFile(artifact_type, content, hashlib.sha256(data).hexdigest(), True)


def append_to_decision_log(content: str) -> ContextFile:
    """Append session synthesis without racing a Mini App document replacement."""
    with _CONTEXT_LOCK:
        current = read_context_file(ArtifactType.DECISION_LOG)
        note = content.strip()
        if not note:
            return current
        # Keep the existing session-note spacing; the reentrant lock covers
        # both the read and replacement, preserving simultaneous session notes.
        return replace_context_file(
            ArtifactType.DECISION_LOG,
            f"{current.content}\n\n{note}\n",
            expected_revision=current.revision,
        )


def _version_path(artifact_type: ArtifactType, version_id: str) -> Path:
    """Accept current and legacy backup IDs while rejecting arbitrary filenames."""
    path = _artifact_path(artifact_type)
    pattern = rf"{path.stem}_backup_\d{{8}}_\d{{6}}(?:_[0-9a-f]{{32}})?\.md"
    if re.fullmatch(pattern, version_id) is None:
        raise ValueError("Invalid context version ID.")
    backup = path.with_name(version_id)
    if backup.is_symlink():
        raise ValueError("Previous context versions cannot be symlinks.")
    return backup


def list_context_versions(artifact_type: ArtifactType) -> list[ContextVersion]:
    """List available backups newest first, including existing timestamp-only IDs."""
    artifact_type = ArtifactType(artifact_type)
    with _CONTEXT_LOCK:
        path = _artifact_path(artifact_type)
        versions: list[tuple[int, ContextVersion]] = []
        for backup in path.parent.glob(f"{path.stem}_backup_*.md"):
            # Other runtime files are not restore targets. In particular,
            # reject symlinks rather than exposing files outside this folder.
            try:
                valid_path = _version_path(artifact_type, backup.name)
            except ValueError:
                continue
            if not valid_path.is_file():
                continue
            metadata = valid_path.stat()
            version = ContextVersion(
                version_id=backup.name,
                saved_at=datetime.fromtimestamp(metadata.st_mtime, timezone.utc).isoformat(),
            )
            versions.append((metadata.st_mtime_ns, version))
        versions.sort(key=lambda entry: (entry[0], entry[1].version_id), reverse=True)
        return [version for _, version in versions]


def read_context_version(artifact_type: ArtifactType, version_id: str) -> ContextFile:
    """Load one previous version for preview without changing live context."""
    artifact_type = ArtifactType(artifact_type)
    with _CONTEXT_LOCK:
        path = _version_path(artifact_type, version_id)
        version = _read_path(artifact_type, path)
        if not version.exists:
            raise FileNotFoundError(f"Context version not found: {version_id}")
        return version


def restore_context_version(
    artifact_type: ArtifactType,
    version_id: str,
    *,
    expected_revision: str,
) -> ContextFile:
    """Restore exact older text while backing up the live version it replaces."""
    with _CONTEXT_LOCK:
        version = read_context_version(artifact_type, version_id)
        return replace_context_file(
            artifact_type, version.content, expected_revision=expected_revision,
        )
