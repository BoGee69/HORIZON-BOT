"""
Attachment readers for owner DM workflows.

The text returned by this module is sanitized before it is used in prompts or
server-action proposals. Image OCR can only be as private as the configured AI
vision provider, so keep it owner-only.
"""
from __future__ import annotations

import base64
import io
import logging
import time
import zipfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import aiohttp

import config as bot_config
from utils.ai_caretaker import AICaretakerUnavailable, call_ai_provider, sanitize_text

log = logging.getLogger(__name__)

TEXT_EXTENSIONS = {
    ".txt",
    ".md",
    ".markdown",
    ".csv",
    ".json",
    ".log",
    ".ini",
    ".cfg",
    ".conf",
    ".yaml",
    ".yml",
}
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp"}


@dataclass
class AttachmentReadResult:
    text: str = ""
    notes: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _get_attachment_cache(bot: Any) -> dict[int, dict[str, Any]]:
    cache = getattr(bot, "ai_attachment_cache", None)
    if not isinstance(cache, dict):
        cache = {}
        setattr(bot, "ai_attachment_cache", cache)
    return cache


def store_attachment_text(
    bot: Any,
    user_id: int,
    result: AttachmentReadResult,
    *,
    source: str = "owner-dm",
) -> None:
    text = sanitize_text(result.text).strip()
    if not text:
        return
    cache = _get_attachment_cache(bot)
    cache[int(user_id)] = {
        "text": text,
        "notes": list(result.notes or [])[:5],
        "warnings": list(result.warnings or [])[:5],
        "source": sanitize_text(source)[:80],
        "created_at": time.time(),
    }


def get_recent_attachment_text(bot: Any, user_id: int) -> dict[str, Any] | None:
    cache = _get_attachment_cache(bot)
    item = cache.get(int(user_id))
    if not isinstance(item, dict):
        return None
    ttl = max(30, int(getattr(bot_config, "AI_ATTACHMENT_CACHE_SECONDS", 900) or 900))
    if time.time() - float(item.get("created_at") or 0) > ttl:
        cache.pop(int(user_id), None)
        return None
    text = sanitize_text(str(item.get("text") or "")).strip()
    if not text:
        cache.pop(int(user_id), None)
        return None
    return item


def clear_recent_attachment_text(bot: Any, user_id: int) -> None:
    _get_attachment_cache(bot).pop(int(user_id), None)


def _attachment_name(attachment: Any) -> str:
    return str(getattr(attachment, "filename", "") or "attachment").strip() or "attachment"


def _attachment_size(attachment: Any) -> int:
    try:
        return int(getattr(attachment, "size", 0) or 0)
    except (TypeError, ValueError):
        return 0


def _extension(filename: str) -> str:
    return Path(filename).suffix.lower()


def _content_type(attachment: Any, extension: str) -> str:
    raw = str(getattr(attachment, "content_type", "") or "").split(";", 1)[0].strip().lower()
    if raw:
        return raw
    if extension == ".png":
        return "image/png"
    if extension in {".jpg", ".jpeg"}:
        return "image/jpeg"
    if extension == ".webp":
        return "image/webp"
    if extension == ".pdf":
        return "application/pdf"
    if extension == ".docx":
        return "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    return "text/plain"


def _truncate(text: str, limit: int) -> tuple[str, bool]:
    limit = max(500, int(limit or 12000))
    if len(text) <= limit:
        return text, False
    return text[:limit].rstrip() + "\n...[TRUNCATED]...", True


def _decode_text(data: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _extract_docx(data: bytes) -> str:
    namespaces = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        document_xml = archive.read("word/document.xml")
    root = ET.fromstring(document_xml)
    paragraphs: list[str] = []
    for paragraph in root.findall(".//w:p", namespaces):
        pieces: list[str] = []
        for node in paragraph.iter():
            tag = node.tag.rsplit("}", 1)[-1]
            if tag == "t" and node.text:
                pieces.append(node.text)
            elif tag == "tab":
                pieces.append("\t")
            elif tag in {"br", "cr"}:
                pieces.append("\n")
        line = "".join(pieces).strip()
        if line:
            paragraphs.append(line)
    return "\n".join(paragraphs)


def _extract_pdf(data: bytes) -> str:
    try:
        from pypdf import PdfReader
    except Exception as exc:  # pragma: no cover - depends on optional package
        raise RuntimeError("PDF support requires the pypdf package.") from exc

    reader = PdfReader(io.BytesIO(data))
    pages: list[str] = []
    for index, page in enumerate(reader.pages):
        if index >= 60:
            break
        pages.append(page.extract_text() or "")
    return "\n\n".join(page.strip() for page in pages if page.strip())


async def _download_attachment(session: aiohttp.ClientSession, attachment: Any) -> bytes:
    url = str(getattr(attachment, "url", "") or "").strip()
    if not url.startswith(("http://", "https://")):
        raise RuntimeError("Attachment URL is not available.")
    timeout = aiohttp.ClientTimeout(total=30)
    async with session.get(url, timeout=timeout) as response:
        if response.status >= 400:
            raise RuntimeError(f"Discord attachment download failed with HTTP {response.status}.")
        return await response.read()


async def _ocr_image(
    session: aiohttp.ClientSession,
    *,
    data: bytes,
    mime_type: str,
    filename: str,
    purpose: str,
) -> str:
    if not bot_config.AI_ATTACHMENT_OCR_ENABLED:
        raise RuntimeError("Image OCR is disabled.")
    prompt = (
        "Extract readable text from this image for a Discord bot owner workflow.\n"
        "Return plain text only. Preserve numbering, bullet points, headings, and line breaks. "
        "Do not summarize. If the image contains no readable text, return an empty string.\n"
        f"Purpose: {sanitize_text(purpose)[:120]}\n"
        f"Filename: {sanitize_text(filename)[:120]}"
    )
    image_payload = {
        "mime_type": mime_type,
        "data": base64.b64encode(data).decode("ascii"),
    }
    return await call_ai_provider(
        session,
        prompt,
        provider=bot_config.AI_ATTACHMENT_VISION_PROVIDER,
        model=bot_config.AI_ATTACHMENT_VISION_MODEL,
        temperature=0.0,
        max_output_tokens=4096,
        images=[image_payload],
    )


async def read_message_attachments(
    session: aiohttp.ClientSession,
    attachments: list[Any],
    *,
    purpose: str = "owner message",
) -> AttachmentReadResult:
    result = AttachmentReadResult()
    if not bot_config.AI_ATTACHMENT_ENABLED:
        result.warnings.append("Attachment reading is disabled by AI_ATTACHMENT_ENABLED=false.")
        return result
    if not attachments:
        return result
    if not session or session.closed:
        result.warnings.append("HTTP session is unavailable, so attachments cannot be read.")
        return result

    max_bytes = max(1024, int(bot_config.AI_ATTACHMENT_MAX_BYTES or 0))
    max_chars = max(500, int(bot_config.AI_ATTACHMENT_MAX_TEXT_CHARS or 12000))
    chunks: list[str] = []

    for attachment in attachments[:5]:
        filename = _attachment_name(attachment)
        extension = _extension(filename)
        content_type = _content_type(attachment, extension)
        size = _attachment_size(attachment)
        if size and size > max_bytes:
            result.warnings.append(f"{filename} skipped because it is larger than {max_bytes} bytes.")
            continue

        try:
            data = await _download_attachment(session, attachment)
            if len(data) > max_bytes:
                result.warnings.append(f"{filename} skipped because it is larger than {max_bytes} bytes.")
                continue

            text = ""
            if extension in TEXT_EXTENSIONS or content_type.startswith("text/"):
                text = _decode_text(data)
            elif extension == ".docx":
                text = _extract_docx(data)
            elif extension == ".pdf":
                text = _extract_pdf(data)
            elif extension in IMAGE_EXTENSIONS or content_type.startswith("image/"):
                text = await _ocr_image(
                    session,
                    data=data,
                    mime_type=content_type,
                    filename=filename,
                    purpose=purpose,
                )
            else:
                result.warnings.append(f"{filename} has unsupported file type `{extension or content_type}`.")
                continue

            text = sanitize_text(text).strip()
            if not text:
                result.warnings.append(f"{filename} did not contain readable text.")
                continue
            chunks.append(text)
            result.notes.append(f"Read {filename}")
        except AICaretakerUnavailable as exc:
            result.warnings.append(
                f"{filename} could not be processed by the vision/text provider: {sanitize_text(str(exc))[:240]}"
            )
        except Exception as exc:
            log.warning("Could not read attachment %s", filename, exc_info=True)
            result.warnings.append(f"{filename} could not be read: {sanitize_text(str(exc))[:240]}")

    combined = "\n\n".join(chunks).strip()
    result.text, truncated = _truncate(combined, max_chars)
    if truncated:
        result.warnings.append(f"Attachment text was truncated to {max_chars} characters.")
    return result
