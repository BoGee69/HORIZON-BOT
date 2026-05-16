"""
AI caretaker helpers for safe operational analysis.

The caretaker only receives sanitized health data, summaries, and recent error
events. It must never receive raw secrets or perform actions on its own.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
import traceback
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Optional

import aiohttp

import config as bot_config
from utils.diagnostics import collect_health

log = logging.getLogger(__name__)

GEMINI_API_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
REDACTED = "[REDACTED]"
MAX_EVENT_CHARS = 1600

_SENSITIVE_NAME_PARTS = (
    "token",
    "secret",
    "password",
    "webhook",
    "authorization",
    "cookie",
    "jwt",
)
_SENSITIVE_EXACT_OR_SUFFIXES = (
    "api_key",
    "_api_key",
    "access_key_id",
    "_access_key_id",
    "secret_access_key",
    "_secret_access_key",
)
_TEXT_REDACTIONS = [
    (re.compile(r"https://(?:canary\.|ptb\.)?discord(?:app)?\.com/api/webhooks/[^\s)>\"]+", re.I), "[REDACTED_WEBHOOK]"),
    (re.compile(r"([?&](?:token|jwt|X-Amz-Signature|X-Amz-Credential|X-Amz-Security-Token)=)[^&\s]+", re.I), r"\1[REDACTED]"),
    (re.compile(r"\b[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{20,}\b"), "[REDACTED_TOKEN]"),
    (re.compile(r"\b[a-f0-9]{48,}\b", re.I), "[REDACTED_HEX_SECRET]"),
    (re.compile(r"(?i)\b(token|secret|password|webhook|authorization|jwt|api[_-]?key|access[_-]?key)\s*[:=]\s*['\"]?[^,\s'\"]{8,}"), r"\1=[REDACTED]"),
]


@dataclass
class AICaretakerResult:
    status: str
    title: str
    summary: str
    causes: list[str] = field(default_factory=list)
    actions: list[str] = field(default_factory=list)
    env_to_check: list[str] = field(default_factory=list)
    raw_text: str = ""

    @property
    def level(self) -> str:
        if self.status == "CRITICAL":
            return "error"
        if self.status == "WARNING":
            return "warning"
        return "info"


class AICaretakerUnavailable(RuntimeError):
    """Raised when the AI provider cannot return an analysis."""


class SafeEventRingBuffer:
    def __init__(self, maxlen: int = 50):
        self._items: deque[dict[str, Any]] = deque(maxlen=maxlen)

    def append(
        self,
        *,
        level: str,
        source: str,
        message: str,
        fields: Optional[dict[str, Any]] = None,
    ) -> None:
        self._items.append(
            sanitize_data(
                {
                    "at": int(time.time()),
                    "level": str(level).upper(),
                    "source": source,
                    "message": str(message)[:MAX_EVENT_CHARS],
                    "fields": fields or {},
                }
            )
        )

    def snapshot(self, limit: int = 25) -> list[dict[str, Any]]:
        return list(self._items)[-limit:]


class CaretakerLogHandler(logging.Handler):
    def __init__(self, event_buffer: SafeEventRingBuffer):
        super().__init__(level=logging.ERROR)
        self.event_buffer = event_buffer

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = self.format(record)
            if record.exc_info:
                message = f"{message}\n{''.join(traceback.format_exception(*record.exc_info))}"
            self.event_buffer.append(
                level=record.levelname,
                source=f"log:{record.name}",
                message=message,
            )
        except Exception:
            pass


def _is_sensitive_key(key: str) -> bool:
    lower = str(key).strip().lower().replace("-", "_")
    if lower.endswith("_configured"):
        return False
    return any(part in lower for part in _SENSITIVE_NAME_PARTS) or any(
        lower == item or lower.endswith(item) for item in _SENSITIVE_EXACT_OR_SUFFIXES
    )


def _known_secret_values() -> set[str]:
    values = {
        bot_config.DISCORD_TOKEN,
        bot_config.JWT_SECRET,
        bot_config.R2_ACCESS_KEY_ID,
        bot_config.R2_SECRET_ACCESS_KEY,
        bot_config.STEAM_API_KEY,
        bot_config.ADMIN_WEBHOOK,
        bot_config.GEMINI_API_KEY,
    }
    for name, value in os.environ.items():
        if _is_sensitive_key(name) and value:
            values.add(value)
    return {str(value) for value in values if value and len(str(value)) >= 8}


def sanitize_text(value: Any) -> str:
    text = str(value or "")
    for secret in sorted(_known_secret_values(), key=len, reverse=True):
        text = text.replace(secret, REDACTED)
    for pattern, replacement in _TEXT_REDACTIONS:
        text = pattern.sub(replacement, text)
    return text


def sanitize_data(value: Any) -> Any:
    if isinstance(value, dict):
        clean: dict[str, Any] = {}
        for key, item in value.items():
            if _is_sensitive_key(str(key)) and not isinstance(item, (bool, int, float, type(None))):
                clean[str(key)] = REDACTED
            else:
                clean[str(key)] = sanitize_data(item)
        return clean
    if isinstance(value, (list, tuple, set)):
        return [sanitize_data(item) for item in value]
    if isinstance(value, str):
        return sanitize_text(value)
    return value


def _summary_to_dict(summary: Any) -> Optional[dict[str, Any]]:
    if summary is None:
        return None
    data: dict[str, Any] = {}
    to_fields = getattr(summary, "to_fields", None)
    if callable(to_fields):
        data["fields"] = to_fields()
    for name in ("errors", "samples", "applied_samples"):
        if hasattr(summary, name):
            data[name] = list(getattr(summary, name) or [])
    data["has_changes"] = bool(getattr(summary, "has_changes", False))
    data["dry_run"] = bool(getattr(summary, "dry_run", False))
    return sanitize_data(data)


async def build_operational_snapshot(bot: Any, *, reason: str, context: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    health = await collect_health(bot)
    events = getattr(bot, "ai_events", None)
    snapshot = {
        "reason": reason,
        "context": context or {},
        "bot": {
            "version": getattr(bot, "version", None),
            "guilds": len(getattr(bot, "guilds", []) or []),
            "ready": bool(getattr(bot, "is_ready", lambda: False)()),
        },
        "health": health,
        "last_r2_maintenance": _summary_to_dict(getattr(bot, "last_r2_maintenance_summary", None)),
        "last_steam_db_sync": _summary_to_dict(getattr(bot, "last_steam_db_sync_summary", None)),
        "recent_events": events.snapshot() if events else [],
    }
    return sanitize_data(snapshot)


def build_prompt(snapshot: dict[str, Any]) -> str:
    body = json.dumps(snapshot, ensure_ascii=False, indent=2, sort_keys=True)
    max_chars = max(2000, int(bot_config.AI_MAINTENANCE_MAX_PROMPT_CHARS or 12000))
    if len(body) > max_chars:
        body = body[:max_chars] + "\n...TRUNCATED..."

    return (
        "Kamu adalah AI ops caretaker untuk Discord bot bernama triadbot.\n"
        "Tugasmu hanya menganalisis snapshot operasional yang sudah disanitasi. "
        "Jangan menyarankan tindakan ilegal, jangan meminta secret, dan jangan mengarang nilai env.\n"
        "Jika masalahnya bisa diperbaiki owner, beri langkah manual yang singkat dan aman.\n"
        "Status harus salah satu: OK, WARNING, CRITICAL.\n"
        "Balas hanya JSON valid tanpa markdown dengan bentuk:\n"
        "{\n"
        '  "status": "OK|WARNING|CRITICAL",\n'
        '  "title": "judul pendek",\n'
        '  "summary": "ringkasan singkat",\n'
        '  "causes": ["kemungkinan penyebab"],\n'
        '  "actions": ["langkah manual"],\n'
        '  "env_to_check": ["NAMA_ENV jika perlu"]\n'
        "}\n\n"
        "Snapshot:\n"
        f"{body}"
    )


async def call_gemini(
    session: aiohttp.ClientSession,
    prompt: str,
    *,
    model: Optional[str] = None,
    temperature: float = 0.2,
    max_output_tokens: int = 900,
) -> str:
    if not bot_config.GEMINI_API_KEY:
        raise AICaretakerUnavailable("GEMINI_API_KEY is not configured")
    if not session or session.closed:
        raise AICaretakerUnavailable("HTTP session is not available")

    selected_model = model or bot_config.AI_MAINTENANCE_MODEL
    url = GEMINI_API_URL.format(model=selected_model)
    payload = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": temperature,
            "maxOutputTokens": max_output_tokens,
        },
    }
    timeout = aiohttp.ClientTimeout(total=35)
    async with session.post(
        url,
        headers={
            "Content-Type": "application/json",
            "x-goog-api-key": bot_config.GEMINI_API_KEY,
        },
        json=payload,
        timeout=timeout,
    ) as response:
        response_text = await response.text()
        if response.status >= 400:
            raise AICaretakerUnavailable(
                f"Gemini API HTTP {response.status}: {sanitize_text(response_text[:600])}"
            )

    try:
        data = json.loads(response_text)
        candidates = data.get("candidates") or []
        parts = candidates[0].get("content", {}).get("parts", []) if candidates else []
        text = "\n".join(part.get("text", "") for part in parts if part.get("text"))
        if not text:
            raise KeyError("empty response text")
        return sanitize_text(text)
    except Exception as exc:
        raise AICaretakerUnavailable(f"Could not parse Gemini response: {exc}") from exc


def _strip_json_fence(text: str) -> str:
    clean = text.strip()
    if clean.startswith("```"):
        clean = re.sub(r"^```(?:json)?\s*", "", clean, flags=re.I)
        clean = re.sub(r"\s*```$", "", clean)
    return clean.strip()


def parse_ai_result(text: str) -> AICaretakerResult:
    safe_text = sanitize_text(text)
    try:
        payload = json.loads(_strip_json_fence(safe_text))
    except json.JSONDecodeError:
        return AICaretakerResult(
            status="WARNING",
            title="AI caretaker report",
            summary=safe_text[:1500] or "Gemini returned a non-JSON report.",
            raw_text=safe_text,
        )

    status = str(payload.get("status", "WARNING")).strip().upper()
    if status not in {"OK", "WARNING", "CRITICAL"}:
        status = "WARNING"

    def listify(name: str) -> list[str]:
        value = payload.get(name, [])
        if isinstance(value, str):
            return [sanitize_text(value)[:300]]
        if isinstance(value, list):
            return [sanitize_text(item)[:300] for item in value if str(item).strip()][:8]
        return []

    return AICaretakerResult(
        status=status,
        title=sanitize_text(payload.get("title", "AI caretaker report"))[:120],
        summary=sanitize_text(payload.get("summary", ""))[:1500],
        causes=listify("causes"),
        actions=listify("actions"),
        env_to_check=listify("env_to_check"),
        raw_text=safe_text,
    )


async def analyze_bot(bot: Any, *, reason: str, context: Optional[dict[str, Any]] = None) -> AICaretakerResult:
    if bot_config.AI_MAINTENANCE_PROVIDER != "gemini":
        raise AICaretakerUnavailable(f"Unsupported AI provider: {bot_config.AI_MAINTENANCE_PROVIDER}")

    snapshot = await build_operational_snapshot(bot, reason=reason, context=context)
    prompt = build_prompt(snapshot)
    raw_text = await call_gemini(bot.session, prompt)
    return parse_ai_result(raw_text)
