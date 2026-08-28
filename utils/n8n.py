"""Helpers for HORIZON BOT <-> n8n integration."""
from __future__ import annotations

import logging
from dataclasses import asdict, is_dataclass
from typing import Any

import aiohttp

import config as bot_config
from utils.ai_caretaker import sanitize_data, sanitize_text

log = logging.getLogger(__name__)


def summary_to_payload(summary: Any) -> dict[str, Any] | None:
    """Convert local summary/result objects into safe JSON for n8n."""
    if summary is None:
        return None

    payload: dict[str, Any] = {"type": type(summary).__name__}

    to_fields = getattr(summary, "to_fields", None)
    if callable(to_fields):
        try:
            payload["fields"] = to_fields()
        except Exception as exc:
            payload["fields_error"] = sanitize_text(repr(exc))[:300]

    to_dict = getattr(summary, "to_dict", None)
    if callable(to_dict):
        try:
            payload["dict"] = to_dict()
        except Exception as exc:
            payload["dict_error"] = sanitize_text(repr(exc))[:300]

    if is_dataclass(summary):
        try:
            payload["data"] = asdict(summary)
        except Exception as exc:
            payload["data_error"] = sanitize_text(repr(exc))[:300]
    else:
        try:
            values = vars(summary)
        except TypeError:
            values = {}
        if values:
            payload["data"] = {
                key: value
                for key, value in values.items()
                if not key.startswith("_")
            }

    for name in ("errors", "samples", "applied_samples"):
        if hasattr(summary, name):
            try:
                payload[name] = list(getattr(summary, name) or [])
            except Exception:
                payload[name] = []

    for name in ("ok", "uploaded", "has_changes", "dry_run", "has_errors"):
        if hasattr(summary, name):
            try:
                payload[name] = bool(getattr(summary, name))
            except Exception:
                pass

    return sanitize_data(payload)


async def post_n8n_event(
    session: aiohttp.ClientSession | None,
    event_type: str,
    payload: dict[str, Any],
) -> bool:
    """Send a sanitized event payload to the configured n8n webhook."""
    if not bot_config.N8N_ENABLED or not bot_config.N8N_WEBHOOK_URL:
        return False
    if session is None or session.closed:
        return False

    body = sanitize_data(
        {
            "event_type": event_type,
            "source": "horizon",
            "bot_version": bot_config.BOT_VERSION,
            "payload": payload,
        }
    )
    headers = {
        "Content-Type": "application/json",
        "X-HORIZON BOT-Source": "horizon",
    }
    if bot_config.N8N_WEBHOOK_SECRET:
        headers["X-HORIZON BOT-N8N-Secret"] = bot_config.N8N_WEBHOOK_SECRET

    try:
        async with session.post(
            bot_config.N8N_WEBHOOK_URL,
            json=body,
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=bot_config.N8N_REQUEST_TIMEOUT_SECONDS),
        ) as response:
            if response.status >= 400:
                text = await response.text()
                log.warning(
                    "n8n webhook returned HTTP %s: %s",
                    response.status,
                    sanitize_text(text)[:300],
                )
                return False
            return True
    except Exception as exc:
        log.warning("Failed to post n8n event %s: %r", event_type, exc)
        return False
