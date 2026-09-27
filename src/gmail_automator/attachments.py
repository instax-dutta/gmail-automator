"""Attachment resolution (PRD 5.2, Phase 3).

Two sources are supported: inline base64 (self-contained, safe by construction) and a filesystem
path. A path is the dangerous one - it turns a network-reachable API into a file-read primitive -
so it is confined to `GMAIL_AUTOMATOR_ATTACHMENT_ALLOWED_DIRS` and re-checked *after* symlink
resolution.
"""

from __future__ import annotations

import base64
import binascii
import mimetypes
from dataclasses import dataclass
from pathlib import Path

from gmail_automator.config import Settings
from gmail_automator.errors import AttachmentPathNotAllowed, AttachmentTooLarge, InvalidRequest
from gmail_automator.gmail.mime import Attachment

#: Refuse to stat anything absurd before opening it; the real limit is enforced after reading.
_PROBE_BYTES = 1


@dataclass(frozen=True)
class ResolvedAttachments:
    attachments: tuple[Attachment, ...]
    total_bytes: int


def resolve_attachments(
    specs: list[tuple[str, str | None, str | None, str]],
    *,
    settings: Settings,
) -> ResolvedAttachments:
    """Resolve `(filename, content_base64, path, mime_type)` tuples into MIME attachments.

    Raises `InvalidRequest` when attachments are disabled at all, `AttachmentPathNotAllowed` for a
    path outside the allow-list, and `AttachmentTooLarge` past the configured cap.
    """
    if not specs:
        return ResolvedAttachments(attachments=(), total_bytes=0)
    if not settings.attachments_enabled:
        raise InvalidRequest(
            "attachments are disabled; set GMAIL_AUTOMATOR_ATTACHMENTS_ENABLED=true to allow them",
            details={"attachments": len(specs)},
        )

    allowed = [
        Path(directory).expanduser().resolve() for directory in settings.attachment_allowed_dirs
    ]
    resolved: list[Attachment] = []
    total = 0
    for filename, content_b64, path, mime_type in specs:
        if content_b64 is not None:
            data = _decode_inline(content_b64, filename=filename)
        elif path is not None:
            data = _read_path(path, allowed=allowed, settings=settings)
        else:  # pragma: no cover - the schema guarantees exactly one source
            raise InvalidRequest(
                "an attachment needs content_base64 or path", details={"filename": filename}
            )
        if len(data) > settings.attachment_max_bytes:
            raise AttachmentTooLarge(
                f"attachment {filename} is {len(data)} bytes, over the "
                f"{settings.attachment_max_bytes} byte limit",
                details={
                    "filename": filename,
                    "bytes": len(data),
                    "max_bytes": settings.attachment_max_bytes,
                },
            )
        total += len(data)
        resolved.append(
            Attachment(
                filename=filename,
                content=data,
                mime_type=mime_type or _guess_mime(filename, data),
            )
        )
    return ResolvedAttachments(attachments=tuple(resolved), total_bytes=total)


def _decode_inline(value: str, *, filename: str) -> bytes:
    try:
        return base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise InvalidRequest(
            f"attachment {filename} is not valid base64",
            details={"filename": filename},
        ) from exc


def _read_path(raw: str, *, allowed: list[Path], settings: Settings) -> bytes:
    candidate = Path(raw).expanduser()
    try:
        # `strict=False` so a missing file is a clean error rather than a traceback; the stat also
        # follows symlinks, which is exactly what must be checked before opening.
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise AttachmentPathNotAllowed(
            f"attachment path {raw} could not be resolved",
            details={"path": raw},
        ) from exc

    if not resolved.is_file():
        raise AttachmentPathNotAllowed(
            f"attachment path {raw} is not a regular file", details={"path": raw}
        )
    if not allowed:
        raise AttachmentPathNotAllowed(
            "path attachments are disabled; set "
            "GMAIL_AUTOMATOR_ATTACHMENT_ALLOWED_DIRS to allow them",
            details={"path": raw, "allowed_dirs": []},
        )
    if not _within(resolved, allowed):
        raise AttachmentPathNotAllowed(
            f"attachment path {raw} is outside GMAIL_AUTOMATOR_ATTACHMENT_ALLOWED_DIRS",
            details={"path": raw, "allowed_dirs": [str(directory) for directory in allowed]},
        )
    try:
        size = resolved.stat().st_size
    except OSError as exc:  # pragma: no cover - race with an external delete
        raise AttachmentPathNotAllowed(
            f"attachment path {raw} could not be read", details={"path": raw}
        ) from exc
    if size > settings.attachment_max_bytes:
        raise AttachmentTooLarge(
            f"attachment {resolved.name} is {size} bytes, over the "
            f"{settings.attachment_max_bytes} byte limit",
            details={
                "filename": resolved.name,
                "bytes": size,
                "max_bytes": settings.attachment_max_bytes,
            },
        )
    try:
        return resolved.read_bytes()
    except OSError as exc:
        raise AttachmentPathNotAllowed(
            f"attachment path {raw} could not be read", details={"path": raw}
        ) from exc


def _within(candidate: Path, allowed: list[Path]) -> bool:
    """Containment check that is not fooled by a sibling directory with a shared prefix."""
    return any(candidate == directory or directory in candidate.parents for directory in allowed)


def _guess_mime(filename: str, data: bytes) -> str:
    guessed, _ = mimetypes.guess_type(filename)
    if guessed:
        return guessed
    if data.startswith(b"%PDF-"):
        return "application/pdf"
    return "application/octet-stream"


__all__ = ["ResolvedAttachments", "resolve_attachments"]
