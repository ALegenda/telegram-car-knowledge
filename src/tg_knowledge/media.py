"""Shared limits for downloading and extracting source attachments."""

import re
import json
from pathlib import Path

TEXT_SUFFIXES = {".txt", ".md", ".csv", ".json", ".log", ".yaml", ".yml"}
SOURCE_SUFFIXES = TEXT_SUFFIXES | {".pdf", ".docx"}
MAX_TEXT_BYTES = 1024 * 1024
MAX_DOCUMENT_BYTES = 30 * 1024 * 1024
PROCESSING_VERSION = 1


def media_identity(metadata: dict) -> dict:
    return {key: metadata.get(key) for key in ("id", "size", "mime_type")}


def processed_source(root: Path, message_id: int, metadata: dict) -> dict | None:
    path = root / "processed" / f"{int(message_id)}.json"
    if not path.is_file():
        return None
    result = json.loads(path.read_text(encoding="utf-8"))
    if result.get("version") != PROCESSING_VERSION or result.get("identity") != media_identity(
        metadata
    ):
        return None
    return result


def attachment_suffix(metadata: dict) -> str:
    # Use only the suffix, never a filename/path supplied by Telegram.
    named = Path((metadata.get("name") or "").replace("\\", "/")).suffix.lower()
    if re.fullmatch(r"\.[a-z0-9]+", named):
        return named
    suffix = (metadata.get("ext") or "").lower()
    if re.fullmatch(r"\.[a-z0-9]+", suffix):
        return suffix
    return ".bin"


def media_skip_reason(media_type: str, metadata: dict, policy: str, max_mb=None) -> str | None:
    if policy not in {"source", "all", "none"}:
        raise ValueError("Неизвестный режим вложений.")
    if policy == "none":
        return "disabled"
    size = metadata.get("size")
    if policy == "source":
        if media_type != "MessageMediaDocument":
            return "non_document"
        suffix = attachment_suffix(metadata)
        if suffix not in SOURCE_SUFFIXES:
            return "unsupported_format"
        if not isinstance(size, int) or size <= 0:
            return "unknown_size"
        limit = MAX_TEXT_BYTES if suffix in TEXT_SUFFIXES else MAX_DOCUMENT_BYTES
        if size > limit:
            return "outside_extraction_limit"
    if max_mb is not None and size and size > max_mb * 1024 * 1024:
        return "size_limit"
    return None
