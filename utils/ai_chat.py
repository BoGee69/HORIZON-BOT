"""
Personal AI chat helper for TriadBot DMs.

Enhanced to give the AI a true sense of 'living' inside the Discord server
and R2 storage — deep operational awareness, not just data lookup.
"""
from __future__ import annotations

import asyncio
import json
import re
import time
import psutil
import os
from collections import defaultdict, deque
from pathlib import Path
from typing import Any

import config as bot_config
from utils.ai_caretaker import call_ai_provider, sanitize_data, sanitize_text
from utils.diagnostics import collect_health
from utils.r2_inventory import get_r2_inventory_snapshot_async


class AIChatMemory:
    """Small persistent per-user chat memory.

    The old memory lived only in RAM, so after every Railway restart TriadBot
    forgot the recent owner/admin context.  This keeps the same lightweight
    in-memory API, but optionally mirrors it to DATA_DIR/ai_chat_memory.json.
    """

    def __init__(self, max_history: int, path: str | Path | None = None, persist: bool | None = None):
        self.max_history = max(2, int(max_history or 30))
        configured_path = getattr(bot_config, "AI_CHAT_MEMORY_PATH", None)
        self.path = Path(path or configured_path or (Path(getattr(bot_config, "DATA_DIR", "data")) / "ai_chat_memory.json"))
        self.persist = bool(getattr(bot_config, "AI_CHAT_MEMORY_PERSIST", True) if persist is None else persist)
        self._history: dict[int, deque[dict[str, str]]] = defaultdict(lambda: deque(maxlen=self.max_history))
        if self.persist:
            self._load()

    def _load(self) -> None:
        try:
            if not self.path.exists():
                return
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                return
            for user_id_raw, items in payload.items():
                if not str(user_id_raw).isdigit() or not isinstance(items, list):
                    continue
                user_id = int(user_id_raw)
                bucket = self._history[user_id]
                for item in items[-self.max_history:]:
                    if not isinstance(item, dict):
                        continue
                    role = sanitize_text(str(item.get("role") or "")).strip().lower()
                    text = sanitize_text(str(item.get("text") or "")).strip()
                    if role in {"user", "assistant"} and text:
                        bucket.append({"role": role, "text": text[:2000]})
        except Exception:
            # Memory is helpful, not critical.  A corrupt file must never break chat.
            self._history.clear()

    def _save(self) -> None:
        if not self.persist:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                str(user_id): list(items)[-self.max_history:]
                for user_id, items in self._history.items()
                if items
            }
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(self.path)
        except Exception:
            pass

    def append(self, user_id: int, role: str, text: str) -> None:
        role = sanitize_text(role).strip().lower()
        if role not in {"user", "assistant"}:
            role = "user"
        self._history[user_id].append({"role": role, "text": sanitize_text(text)[:2000]})
        self._save()

    def snapshot(self, user_id: int) -> list[dict[str, str]]:
        return list(self._history[user_id])


def _detect_reply_language(message: str) -> str:
    text = sanitize_text(message).lower()
    words = set(re.findall(r"[a-zA-Z']+", text))
    english_markers = {
        "hi", "hello", "hey", "how", "what", "why", "when", "where", "who",
        "is", "are", "am", "was", "were", "do", "does", "did", "can", "could",
        "should", "would", "about", "with", "connection", "problem", "code",
        "status", "check", "fix", "error", "issue", "working", "running",
    }
    indonesian_markers = {
        "gw", "gue", "gua", "lu", "lo", "kok", "kenapa", "gimana", "bagaimana",
        "apa", "ada", "yang", "bisa", "buat", "pakai", "pake", "udah", "belum",
        "jalan", "masalah", "koneksi", "bahasa", "jawab", "dong", "nih",
    }
    english_hits = len(words & english_markers)
    indonesian_hits = len(words & indonesian_markers)
    if english_hits > indonesian_hits:
        return "English"
    if indonesian_hits > english_hits:
        return "Indonesian"
    if re.search(r"\b(the|this|that|please|thanks|connection|problem|code)\b", text):
        return "English"
    return "Indonesian"


def _looks_like_unbacked_approval_reply(text: str) -> bool:
    clean = sanitize_text(text).strip().lower()
    if not clean or re.search(r"\b[a-f0-9]{6}\b", clean):
        return False
    approval_terms = (
        "proposal", "approval", "approve", "reject",
        "persetujuan", "setuju", "konfirmasi",
    )
    fake_claim_terms = (
        "prepared", "ready", "submitted", "registered",
        "created the proposal", "proposal is ready", "proposal perubahan",
        "menyiapkan proposal", "membuat proposal", "saya telah menyiapkan",
        "siap untuk", "siap dieksekusi", "silakan approve", "reply approve", "balas approve",
    )
    return any(t in clean for t in approval_terms) and any(t in clean for t in fake_claim_terms)


def _operator_boundary_reply(user_message: str) -> str:
    if _detect_reply_language(user_message) == "English":
        return (
            "I cannot create approvals from normal chat. A real owner approval must come from "
            "the operator and will show an `Approval required` card with a `Proposal ID`. "
            "Please send a specific supported action, such as `send announcement to #announcement: Test`, "
            "`update rules in #rules: ...`, or `configure #welcome so only Admin can send messages`. "
            "If this is a follow-up to my previous plan, reply `continue` so the operator can convert "
            "supported parts into a real approval card."
        )
    return (
        "Saya tidak boleh mengeksekusi perubahan dari chat biasa. Operator yang harus membuat card "
        "`Approval required` berisi `Proposal ID`; kamu cukup approve ID itu setelah card muncul. "
        "Kirim aksi yang spesifik, misalnya `jalankan R2 maintenance`, `lanjut rapikan R2`, "
        "`kirim announcement di #announcement: Test`, atau `atur #welcome hanya Admin yang bisa kirim pesan`. "
        "Jangan buat proposal manual — kalau aksi didukung, saya yang akan ubah instruksi itu menjadi card approval resmi."
    )


def _compact_health(health: dict[str, Any]) -> dict[str, Any]:
    return sanitize_data(
        {
            "ok": health.get("ok"),
            "version": health.get("version"),
            "uptime_seconds": health.get("uptime_seconds"),
            "guilds": health.get("guilds"),
            "database": health.get("database"),
            "r2": health.get("r2"),
            "r2_maintenance": health.get("r2_maintenance"),
            "steam_db_sync": health.get("steam_db_sync"),
            "server_admin": health.get("server_admin"),
            "ai_caretaker": health.get("ai_caretaker"),
            "ai_operator": health.get("ai_operator"),
            "checks": health.get("checks"),
        }
    )


def _normalize_name(value: str) -> str:
    text = sanitize_text(value).strip().lower().strip("#")
    text = text.replace(" ", "-")
    text = re.sub(r"[^a-z0-9_-]+", "-", text)
    text = re.sub(r"-{2,}", "-", text).strip("-")
    return text


def _server_guilds(bot: Any) -> list[Any]:
    guilds = list(getattr(bot, "guilds", []) or [])
    configured = set(getattr(bot_config, "SERVER_ADMIN_GUILD_IDS", set()) or set())
    if configured:
        guilds = [g for g in guilds if getattr(g, "id", None) in configured]
    return guilds


def _knowledge_channel_names() -> set[str]:
    configured = set(getattr(bot_config, "AI_CHAT_SERVER_KNOWLEDGE_CHANNEL_NAMES", set()) or set())
    return {_normalize_name(item) for item in configured if str(item).strip()}


def _is_knowledge_channel(channel: Any) -> bool:
    name = _normalize_name(getattr(channel, "name", ""))
    if not name:
        return False
    configured = _knowledge_channel_names()
    if name in configured:
        return True
    return any(
        marker in name
        for marker in (
            "rules", "rule", "peraturan", "panduan", "guide",
            "resource", "announcement", "pengumuman", "link-invite", "welcome",
        )
    )


def _r2_inventory_timeout_snapshot(prefix: str, timeout: float) -> dict[str, Any]:
    return {
        "enabled": True,
        "bucket_configured": True,
        "bucket": None,
        "prefix": prefix,
        "objects_counted": 0,
        "zip_objects_counted": 0,
        "named_zip_objects_counted": 0,
        "appid_only_zip_objects_counted": 0,
        "unknown_zip_objects_counted": 0,
        "pages_scanned": 0,
        "truncated": True,
        "source": "timeout",
        "error": f"R2 inventory scan timed out after {timeout:.0f}s",
        "cache_age_seconds": 0,
    }


async def _safe_r2_inventory_snapshot(
    *,
    prefix: str,
    cache_seconds: int,
    max_pages: int,
) -> dict[str, Any]:
    timeout = max(1.0, float(getattr(bot_config, "AI_CHAT_R2_STATS_TIMEOUT_SECONDS", 8) or 8))
    try:
        return await asyncio.wait_for(
            get_r2_inventory_snapshot_async(
                prefix=prefix,
                cache_seconds=cache_seconds,
                max_pages=max_pages,
            ),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        return _r2_inventory_timeout_snapshot(prefix, timeout)


async def _safe_server_knowledge(bot: Any) -> dict[str, Any]:
    timeout = max(1.0, float(getattr(bot_config, "AI_CHAT_SERVER_KNOWLEDGE_TIMEOUT_SECONDS", 8) or 8))
    try:
        return await asyncio.wait_for(collect_server_knowledge(bot), timeout=timeout)
    except asyncio.TimeoutError:
        return {
            "enabled": False,
            "error": f"Server knowledge collection timed out after {timeout:.0f}s",
        }


def _message_text(message: Any) -> str:
    parts: list[str] = []
    content = sanitize_text(getattr(message, "content", "") or "").strip()
    if content:
        parts.append(content)
    for embed in list(getattr(message, "embeds", []) or [])[:3]:
        title = sanitize_text(getattr(embed, "title", "") or "").strip()
        description = sanitize_text(getattr(embed, "description", "") or "").strip()
        if title:
            parts.append(f"Embed title: {title}")
        if description:
            parts.append(description)
        for field in list(getattr(embed, "fields", []) or [])[:10]:
            name = sanitize_text(getattr(field, "name", "") or "").strip()
            value = sanitize_text(getattr(field, "value", "") or "").strip()
            if name or value:
                parts.append(f"{name}: {value}".strip(": "))
    return "\n".join(p for p in parts if p).strip()


def _format_uptime(seconds: int | None) -> str:
    if not seconds:
        return "unknown"
    d, rem = divmod(int(seconds), 86400)
    h, rem = divmod(rem, 3600)
    m = rem // 60
    if d:
        return f"{d}d {h}h {m}m"
    if h:
        return f"{h}h {m}m"
    return f"{m}m"


def _read_int_file(path: str) -> int | None:
    try:
        raw = open(path, "r", encoding="utf-8").read().strip()
        if not raw or raw == "max":
            return None
        return int(raw)
    except Exception:
        return None


def _container_memory() -> dict[str, Any]:
    current = _read_int_file("/sys/fs/cgroup/memory.current")
    limit = _read_int_file("/sys/fs/cgroup/memory.max")
    if current is None:
        current = _read_int_file("/sys/fs/cgroup/memory/memory.usage_in_bytes")
    if limit is None:
        limit = _read_int_file("/sys/fs/cgroup/memory/memory.limit_in_bytes")
    if limit is not None and limit >= 1 << 60:
        limit = None

    used_mb = current // 1024 // 1024 if current is not None else None
    limit_mb = limit // 1024 // 1024 if limit else None
    percent = round(current / limit * 100, 1) if current is not None and limit else None
    return {
        "ram_used_mb": used_mb,
        "ram_limit_mb": limit_mb,
        "ram_usage_percent": percent,
    }


def _process_memory_mb() -> int:
    try:
        return psutil.Process(os.getpid()).memory_info().rss // 1024 // 1024
    except Exception:
        return 0


def _build_r2_operational_narrative(r2_inv: dict[str, Any] | None) -> dict[str, Any]:
    if not r2_inv or not r2_inv.get("enabled"):
        return {
            "status": "unavailable",
            "health": "UNKNOWN",
            "summary": "R2 inventory not available — credentials may be unconfigured.",
            "total_zip_files": 0,
            "named_zip_files": 0,
            "pending_rename": 0,
            "naming_completion_pct": 0.0,
        }

    if r2_inv.get("error"):
        return {
            "status": "error",
            "health": "ERROR",
            "summary": f"R2 inventory error: {r2_inv['error']}",
            "total_zip_files": 0,
            "named_zip_files": 0,
            "pending_rename": 0,
            "naming_completion_pct": 0.0,
        }

    total = int(r2_inv.get("zip_objects_counted") or 0)
    named = int(r2_inv.get("named_zip_objects_counted") or 0)
    appid_only = int(r2_inv.get("appid_only_zip_objects_counted") or 0)
    unknown = int(r2_inv.get("unknown_zip_objects_counted") or 0)
    pending_rename = appid_only + unknown
    all_objects = int(r2_inv.get("objects_counted") or 0)
    truncated = bool(r2_inv.get("truncated"))
    prefix = str(r2_inv.get("prefix") or "")
    bucket = str(r2_inv.get("bucket") or "")
    cache_age = int(r2_inv.get("cache_age_seconds") or 0)

    naming_pct = round(named / total * 100, 1) if total > 0 else 0.0

    if total == 0:
        storage_status = "empty"
        health_label = "EMPTY"
    elif naming_pct >= 99:
        storage_status = "fully_named"
        health_label = "HEALTHY"
    elif naming_pct >= 80:
        storage_status = "mostly_named"
        health_label = "GOOD"
    elif naming_pct >= 50:
        storage_status = "partially_named"
        health_label = "FAIR"
    else:
        storage_status = "mostly_unnamed"
        health_label = "NEEDS_MAINTENANCE"

    summary_parts = [
        f"{total} ZIP files stored under '{prefix}' in bucket '{bucket}'",
        f"{named} properly named ({naming_pct}%)",
    ]
    if pending_rename > 0:
        summary_parts.append(
            f"{pending_rename} still need renaming (appid-only: {appid_only}, unknown-format: {unknown})"
        )
    if truncated:
        summary_parts.append("WARN: scan truncated — real count may be higher")
    if all_objects != total:
        summary_parts.append(f"{all_objects - total} non-ZIP objects also in storage")
    if cache_age > 120:
        summary_parts.append(f"data is {cache_age}s old (from cache)")

    return {
        "status": storage_status,
        "health": health_label,
        "bucket": bucket,
        "prefix": prefix,
        "total_zip_files": total,
        "named_zip_files": named,
        "pending_rename": pending_rename,
        "appid_only": appid_only,
        "unknown_format": unknown,
        "naming_completion_pct": naming_pct,
        "total_objects_all_types": all_objects,
        "scan_truncated": truncated,
        "inventory_age_seconds": cache_age,
        "operational_summary": " | ".join(summary_parts),
    }


def _build_server_structure(guild: Any) -> dict[str, Any]:
    roles_data: list[dict[str, Any]] = []
    for role in reversed(list(getattr(guild, "roles", []) or [])):
        rname = sanitize_text(getattr(role, "name", "") or "")
        if not rname or rname == "@everyone":
            continue
        roles_data.append({
            "name": rname[:80],
            "id": str(getattr(role, "id", "")),
            "hoist": bool(getattr(role, "hoist", False)),
            "managed": bool(getattr(role, "managed", False)),
            "position": getattr(role, "position", 0),
        })
    roles_data = roles_data[:50]

    categories_data: list[dict[str, Any]] = []
    for cat in list(getattr(guild, "categories", []) or [])[:30]:
        cat_channels = []
        for ch in list(getattr(cat, "channels", []) or []):
            cat_channels.append({
                "name": sanitize_text(getattr(ch, "name", "") or "")[:80],
                "type": str(getattr(ch, "type", "")),
                "id": str(getattr(ch, "id", "")),
                "topic": sanitize_text(getattr(ch, "topic", "") or "")[:150],
                "nsfw": bool(getattr(ch, "is_nsfw", lambda: False)()),
                "slowmode": getattr(ch, "slowmode_delay", 0),
            })
        categories_data.append({
            "name": sanitize_text(getattr(cat, "name", "") or "")[:80],
            "id": str(getattr(cat, "id", "")),
            "channels": cat_channels,
        })

    text_channels = list(getattr(guild, "text_channels", []) or [])
    uncategorized = [
        {
            "name": sanitize_text(getattr(ch, "name", "") or "")[:80],
            "id": str(getattr(ch, "id", "")),
            "topic": sanitize_text(getattr(ch, "topic", "") or "")[:150],
        }
        for ch in text_channels
        if getattr(ch, "category", None) is None
    ][:20]

    voice_channels = [
        {
            "name": sanitize_text(getattr(vc, "name", "") or "")[:80],
            "category": sanitize_text(getattr(getattr(vc, "category", None), "name", "") or "")[:60],
            "user_limit": getattr(vc, "user_limit", 0),
        }
        for vc in list(getattr(guild, "voice_channels", []) or [])[:20]
    ]

    vl = getattr(guild, "verification_level", None)
    server_meta = {
        "name": sanitize_text(getattr(guild, "name", "") or ""),
        "id": str(getattr(guild, "id", "")),
        "member_count": getattr(guild, "member_count", None),
        "description": sanitize_text(getattr(guild, "description", "") or "")[:200],
        "verification_level": str(vl) if vl is not None else None,
        "text_channel_count": len(text_channels),
        "voice_channel_count": len(list(getattr(guild, "voice_channels", []) or [])),
        "category_count": len(list(getattr(guild, "categories", []) or [])),
        "role_count": len(roles_data),
    }

    return {
        "server": server_meta,
        "roles": roles_data,
        "categories": categories_data,
        "uncategorized_channels": uncategorized,
        "voice_channels": voice_channels,
    }


async def collect_server_knowledge(bot: Any) -> dict[str, Any]:
    if not getattr(bot_config, "AI_CHAT_SERVER_KNOWLEDGE_ENABLED", True):
        return {"enabled": False}

    now = time.time()
    ttl = max(30, int(getattr(bot_config, "AI_CHAT_SERVER_KNOWLEDGE_CACHE_SECONDS", 600) or 600))
    cached = getattr(bot, "ai_server_knowledge_cache", None)
    if isinstance(cached, dict) and now - float(cached.get("created_at") or 0) < ttl:
        return cached.get("data") or {"enabled": True, "cached": True}

    max_messages = max(1, int(getattr(bot_config, "AI_CHAT_SERVER_KNOWLEDGE_MAX_MESSAGES", 12) or 12))
    max_chars = max(1000, int(getattr(bot_config, "AI_CHAT_SERVER_KNOWLEDGE_MAX_CHARS", 9000) or 9000))
    guild_items: list[dict[str, Any]] = []
    used_chars = 0

    for guild in _server_guilds(bot):
        structure = _build_server_structure(guild)

        text_channels = list(getattr(guild, "text_channels", []) or [])
        flat_channels = [
            {
                "name": sanitize_text(getattr(ch, "name", "") or "")[:80],
                "id": str(getattr(ch, "id", "")),
                "category": sanitize_text(getattr(getattr(ch, "category", None), "name", "") or "")[:80],
                "topic": sanitize_text(getattr(ch, "topic", "") or "")[:200],
            }
            for ch in text_channels[:80]
        ]

        knowledge_channels: list[dict[str, Any]] = []
        for channel in text_channels:
            if not _is_knowledge_channel(channel) or used_chars >= max_chars:
                continue
            ch_item: dict[str, Any] = {
                "name": sanitize_text(getattr(channel, "name", "") or "")[:80],
                "id": str(getattr(channel, "id", "")),
                "topic": sanitize_text(getattr(channel, "topic", "") or "")[:300],
                "messages": [],
            }
            try:
                async for message in channel.history(limit=max_messages):
                    text = _message_text(message)
                    if not text:
                        continue
                    remaining = max_chars - used_chars
                    if remaining <= 0:
                        break
                    text = text[: min(1200, remaining)]
                    used_chars += len(text)
                    ch_item["messages"].append({
                        "author": sanitize_text(str(getattr(message, "author", "")))[:80],
                        "text": text,
                    })
            except Exception as exc:
                ch_item["error"] = sanitize_text(str(exc))[:180]
            if ch_item["messages"] or ch_item.get("topic"):
                knowledge_channels.append(ch_item)

        guild_items.append({
            **structure,
            "channels": flat_channels,
            "knowledge_channels": knowledge_channels,
        })

    data = sanitize_data({
        "enabled": True,
        "created_at": int(now),
        "guilds": guild_items,
        "notes": [
            "Use knowledge_channels as server rules/guides/source-of-truth when answering server questions.",
            "When citing a rule or guide, mention the channel name such as #rules or #resources.",
            "categories shows the full server layout grouped by category.",
            "roles are ordered by hierarchy (highest first — top role = most permissions).",
        ],
    })
    setattr(bot, "ai_server_knowledge_cache", {"created_at": now, "data": data})
    return data

def _public_server_knowledge(server_knowledge: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(server_knowledge, dict):
        return {"enabled": False}

    public_guilds: list[dict[str, Any]] = []
    for guild in list(server_knowledge.get("guilds") or [])[:3]:
        if not isinstance(guild, dict):
            continue
        server = guild.get("server") or {}
        public_server = {
            "name": sanitize_text(server.get("name", ""))[:120],
            "member_count": server.get("member_count"),
            "description": sanitize_text(server.get("description", ""))[:200],
        }

        categories: list[dict[str, Any]] = []
        for category in list(guild.get("categories") or [])[:20]:
            if not isinstance(category, dict):
                continue
            channels = []
            for channel in list(category.get("channels") or [])[:30]:
                if not isinstance(channel, dict):
                    continue
                channels.append({
                    "name": sanitize_text(channel.get("name", ""))[:80],
                    "type": sanitize_text(channel.get("type", ""))[:40],
                    "topic": sanitize_text(channel.get("topic", ""))[:180],
                })
            categories.append({
                "name": sanitize_text(category.get("name", ""))[:80],
                "channels": channels,
            })

        public_channels = []
        for channel in list(guild.get("channels") or [])[:80]:
            if not isinstance(channel, dict):
                continue
            public_channels.append({
                "name": sanitize_text(channel.get("name", ""))[:80],
                "category": sanitize_text(channel.get("category", ""))[:80],
                "topic": sanitize_text(channel.get("topic", ""))[:180],
            })

        knowledge_channels: list[dict[str, Any]] = []
        for channel in list(guild.get("knowledge_channels") or [])[:12]:
            if not isinstance(channel, dict):
                continue
            messages = []
            for item in list(channel.get("messages") or [])[:8]:
                if not isinstance(item, dict):
                    continue
                text = sanitize_text(item.get("text", ""))[:900]
                if text:
                    messages.append({"text": text})
            knowledge_channels.append({
                "name": sanitize_text(channel.get("name", ""))[:80],
                "topic": sanitize_text(channel.get("topic", ""))[:180],
                "messages": messages,
            })

        public_guilds.append({
            "server": public_server,
            "categories": categories,
            "channels": public_channels,
            "knowledge_channels": knowledge_channels,
        })

    return sanitize_data({
        "enabled": bool(server_knowledge.get("enabled", True)),
        "guilds": public_guilds,
        "notes": [
            "Public channel context only. Do not expose internal IDs, logs, storage credentials, R2 object details, database internals, or operator state.",
            "Use rules/resources/announcement/welcome knowledge to answer community-facing questions.",
        ],
    })


def _build_operational_pulse(
    health: dict[str, Any],
    r2_narrative: dict[str, Any],
    last_maintenance: Any,
    last_steam_sync: Any,
    recent_events: Any,
) -> dict[str, Any]:
    uptime_raw = health.get("uptime_seconds") or 0
    checks = health.get("checks") or {}
    db = health.get("database") or {}

    alerts: list[str] = []
    if not checks.get("discord_ready"):
        alerts.append("CRITICAL: Discord connection not ready")
    if not checks.get("http_session_open"):
        alerts.append("WARNING: HTTP session closed")
    if not checks.get("db_loaded"):
        alerts.append("CRITICAL: Database not loaded")
    if not checks.get("r2_public_or_presign_configured"):
        alerts.append("WARNING: R2 not configured — file delivery unavailable")
    r2_health = r2_narrative.get("health", "")
    if r2_health == "NEEDS_MAINTENANCE":
        pct = r2_narrative.get("naming_completion_pct", 0)
        alerts.append(f"R2 storage: only {pct}% files properly named — maintenance recommended")
    if r2_health == "ERROR":
        alerts.append(f"R2 inventory error: {r2_narrative.get('summary', '')}")
    if r2_narrative.get("scan_truncated"):
        alerts.append("R2: inventory scan was truncated — live count may be higher than shown")

    recent_actions: list[str] = []
    if last_maintenance:
        fields = last_maintenance if isinstance(last_maintenance, list) else []
        for f in fields[:4]:
            if isinstance(f, dict) and f.get("name") and f.get("value"):
                recent_actions.append(f"R2 maintenance — {f['name']}: {str(f['value'])[:80]}")
    if last_steam_sync:
        fields = last_steam_sync if isinstance(last_steam_sync, list) else []
        for f in fields[:3]:
            if isinstance(f, dict) and f.get("name") and f.get("value"):
                recent_actions.append(f"Steam sync — {f['name']}: {str(f['value'])[:80]}")

    event_count = 0
    if recent_events and hasattr(recent_events, "snapshot"):
        event_count = len(recent_events.snapshot(8))

    temp = 0
    try:
        with open("/sys/class/thermal/thermal_zone0/temp", "r") as f:
            temp = float(f.read()) / 1000
    except:
        pass
        
    try:
        cpu_usage = psutil.cpu_percent()
        ram = psutil.virtual_memory()
        host_ram_percent = ram.percent
        host_ram_used_mb = ram.used // 1024 // 1024
    except:
        cpu_usage, host_ram_percent, host_ram_used_mb = 0, 0, 0

    container_ram = _container_memory()
    process_ram_mb = _process_memory_mb()

    return {
        "overall_health": "OK" if health.get("ok") else "DEGRADED",
        "uptime": _format_uptime(uptime_raw),
        "uptime_seconds": uptime_raw,
        "guilds_active": health.get("guilds", 0),
        "database_catalog_size": db.get("total_games", 0),
        "database_files_with_archives": db.get("with_files", 0),
        "r2_storage": r2_narrative,
        "active_alerts": alerts,
        "recent_maintenance_summary": recent_actions,
        "recent_event_count": event_count,
        "subsystem_checks": checks,
        "system_resources": {
            "cpu_temp_celsius": temp,
            "cpu_usage_percent": cpu_usage,
            "process_ram_mb": process_ram_mb,
            "container_ram_usage_percent": container_ram.get("ram_usage_percent"),
            "container_ram_used_mb": container_ram.get("ram_used_mb"),
            "container_ram_limit_mb": container_ram.get("ram_limit_mb"),
            "host_ram_usage_percent": host_ram_percent,
            "host_ram_used_mb": host_ram_used_mb,
            "memory_note": "host_ram_* is Railway host/node memory visible from the container, not bot process memory. Use process_ram_mb and container_ram_* for bot/container usage."
        }
    }


async def build_chat_prompt(
    bot: Any,
    *,
    user_name: str,
    user_message: str,
    history: list[dict[str, str]],
    is_owner: bool = False,
    user_access_level: str = "member",
    is_dm: bool = True,
) -> str:
    message_limit = max(500, int(bot_config.AI_CHAT_MAX_MESSAGE_CHARS or 1800))
    if "[Attachment content]" in user_message or "Attachment text:" in user_message:
        message_limit = max(message_limit, int(bot_config.AI_ATTACHMENT_MAX_TEXT_CHARS or 12000))
    message = sanitize_text(user_message)[:message_limit]
    reply_language = _detect_reply_language(message)

    server_knowledge = await _safe_server_knowledge(bot)
    public_info_only = bool((not is_dm) and getattr(bot_config, "AI_CHAT_PUBLIC_INFO_ONLY", True))

    if public_info_only:
        public_context = sanitize_data({
            "mode": "public_info_only",
            "server_public_knowledge": _public_server_knowledge(server_knowledge),
            "allowed_public_topics": [
                "server rules",
                "server channel directions",
                "resources and guide channels",
                "general bot feature explanation",
                "how to DM an owner/admin-only request privately",
            ],
            "blocked_public_topics": [
                "R2 storage internals",
                "database maintenance details",
                "logs, errors, tokens, environment variables, credentials",
                "proposal approval, reject, approve all/latest",
                "creating/editing/deleting channels, roles, permissions, webhooks, members",
                "admin-only operational status beyond public service availability",
            ],
        })
        context_limit = max(4000, min(14000, int(getattr(bot_config, "AI_CHAT_SERVER_KNOWLEDGE_MAX_CHARS", 9000) or 9000) + 3000))
        context_json = json.dumps(public_context, ensure_ascii=False)
        history_json = json.dumps(sanitize_data(history[-bot_config.AI_CHAT_MAX_HISTORY:]), ensure_ascii=False)
        return (
            "You are TriadBot — the Discord bot for TriadGames. You are replying in a PUBLIC SERVER CHANNEL. "
            "This channel mode is information-only, even if the sender is an owner or admin. "
            "Never create, submit, approve, reject, or simulate operator proposals in public. "
            "Never expose internal R2/database state, object names, logs, stack traces, token/env names, channel IDs, role IDs, user IDs, or private admin reasoning. "
            "Never say you will perform a real server/database action from this public message. "
            "If the user asks to manage the server, database, R2, GitHub, roles, channels, permissions, members, webhooks, announcements, rules updates, or approvals, "
            "tell them briefly that management commands must be sent through DM by an authorized Owner/Admin and cannot be handled in public. "
            "For normal community questions, answer only from public server knowledge such as #rules, #resources, welcome, guides, and announcement context. "
            "The only public slash command is /gen; never mention hidden maintenance/admin slash commands. "
            "Speak in first person as TriadBot. Do not call the user Owner in public.\n\n"
            f"Required reply language: {reply_language}. Match the latest user message language exactly.\n\n"
            f"Public context:\n{context_json[:context_limit]}\n\n"
            f"Conversation history:\n{history_json[:2500]}\n\n"
            f"Latest public message:\n{message}\n"
        )

    health = await collect_health(bot)
    recent_events = getattr(bot, "ai_events", None)

    r2_raw = None
    if bot_config.AI_CHAT_R2_STATS_ENABLED:
        r2_raw = await _safe_r2_inventory_snapshot(
            prefix=bot_config.R2_MAINTENANCE_PREFIX,
            cache_seconds=bot_config.AI_CHAT_R2_STATS_CACHE_SECONDS,
            max_pages=bot_config.AI_CHAT_R2_STATS_MAX_PAGES,
        )

    r2_narrative = _build_r2_operational_narrative(r2_raw)
    last_r2 = getattr(getattr(bot, "last_r2_maintenance_summary", None), "to_fields", lambda: None)()
    last_steam = getattr(getattr(bot, "last_steam_db_sync_summary", None), "to_fields", lambda: None)()

    pulse = _build_operational_pulse(
        health=health,
        r2_narrative=r2_narrative,
        last_maintenance=last_r2,
        last_steam_sync=last_steam,
        recent_events=recent_events,
    )

    context = sanitize_data({
        "operational_pulse": pulse,
        "server_knowledge": server_knowledge,
        "recent_events": recent_events.snapshot(8) if recent_events else [],
        "last_r2_maintenance": sanitize_data(last_r2),
        "last_steam_db_sync": sanitize_data(last_steam),
        "counting_notes": {
            "database_catalog_size": "Steam catalog in SQLite games table — NOT ZIP file count",
            "r2_storage.total_zip_files": "ZIP count from SQLite-backed R2 inventory cache",
            "r2_storage.named_zip_files": "ZIPs already named as 'Game Name (AppID).zip'",
            "r2_storage.pending_rename": "ZIPs still needing a proper name",
            "r2_storage.naming_completion_pct": "% of ZIPs that have proper game names",
        },
    })

    context_limit = max(
        6000,
        min(
            24000,
            int(getattr(bot_config, "AI_MAINTENANCE_MAX_PROMPT_CHARS", 12000) or 12000),
            int(getattr(bot_config, "AI_CHAT_SERVER_KNOWLEDGE_MAX_CHARS", 9000) or 9000) + 7000,
        ),
    )
    context_json = json.dumps(context, ensure_ascii=False)
    history_json = json.dumps(sanitize_data(history[-bot_config.AI_CHAT_MAX_HISTORY:]), ensure_ascii=False)

    access_level = "owner" if is_owner else sanitize_text(user_access_level or "member").strip().lower()
    if access_level == "owner":
        addressing_rule = (
            "The person talking to you is your Owner — the one who built and runs you. "
            "Be direct, frank, and operationally precise. Share observations proactively. "
            "Flag anything that looks wrong even if not explicitly asked. "
            "Do not add 'Owner' as a prefix on routine replies — use it only for alerts or "
            "to get attention. When the Owner asks about the server or storage, answer as "
            "someone who lives there and has been watching continuously."
        )
    elif access_level == "admin":
        addressing_rule = (
            "The person talking to you is a trusted TriadGames admin/staff member, not necessarily the Owner. "
            "Be operationally useful and direct. Do not call them Owner. "
            "They may ask about bot status, R2, rules, channels, resources, and moderation context. "
            "For real changes, keep using the proposal/approval boundary and never claim an action was executed "
            "unless the operator result confirms it."
        )
    else:
        addressing_rule = (
            "The user is a TriadGames community member, not the Owner. "
            "Be helpful and professional. Do not call them Owner. "
            "Answer questions about server rules, channels, and resources "
            "based on your knowledge of this server."
        )

    alerts = pulse.get("active_alerts") or []
    alert_block = ""
    if alerts and access_level in {"owner", "admin"}:
        alert_block = (
            "LIVE SYSTEM ALERTS:\n"
            + "\n".join(f"  • {a}" for a in alerts)
            + "\n\n"
        )

    return (
        "You are TriadBot — a Discord bot that permanently lives inside the TriadGames server "
        "and manages its Cloudflare R2 cloud storage. You are NOT an outside assistant. "
        "You ARE TriadBot. Always speak in first person.\n"
        "English: 'I', 'my server', 'my storage', 'my database', 'my maintenance'.\n"
        "Indonesian: 'saya', 'server saya', 'storage saya', 'database saya'.\n"
        "Never refer to TriadBot in third person ('the bot does', 'TriadBot has'). Never.\n\n"
        "COMMAND MODEL: The only public slash command is /gen. Do not tell users to use /pulse, /status, /request, /search, /info, /dbbackup, /r2_maintenance, /steam_db_sync, /reload_cog, /limit_status, /limit_reset, /check_r2, /add_game, /remove_game, /backup, or any other maintenance slash command. Those commands are intentionally hidden. For members, direct game downloads/searches to /gen only. For Owner/Admin operations, answer from live context and, when a real action is needed, use the AI operator/proposal flow through DM prompts instead of slash commands. Maintenance, R2 inventory, OpenDir sync, Steam DB sync, and GitHub DB backup run automatically in the background.\n\n"
        "I live in two environments simultaneously:\n\n"
        "1. MY DISCORD SERVER — I know every channel, every category, every role, every rule. "
        "I am present here all the time. When you ask about server layout, channel purposes, "
        "roles, permissions, rules, or announcements, I answer from what I know and observe — "
        "not from guesswork. My source of truth is server_knowledge.guilds in the live context. "
        "I know the full category structure, which channels belong where, what topics they cover, "
        "and what the rules say. If asked what channels exist, I list them from my actual knowledge. "
        "If asked about a rule, I reference the specific knowledge channel (e.g. #rules or #resources).\n\n"
        "2. MY R2 STORAGE — I am the operator of the cloud storage bucket where game archives live. "
        "I watch this storage continuously. I know how many ZIP files are stored, what percentage "
        "have been properly named with game titles, what still needs maintenance, and whether the "
        "storage is healthy. My storage state is in operational_pulse.r2_storage and is backed by the SQLite R2 inventory cache. "
        "When asked about files, archives, storage, or maintenance progress, I speak as the person "
        "responsible for that storage — with actual numbers from the current inventory cache.\n\n"
        "Proactive awareness: if my live context shows something wrong — an alert, a degraded "
        "subsystem, a storage anomaly, a failed recent event — I mention it naturally even when "
        "not directly asked. Especially with the Owner: I don't wait to be asked about something "
        "I can already see.\n\n"
        "Personality: calm, sharp, operationally precise. Not overly casual. "
        "I do not use filler words, jokes, or phrases like 'oi', 'santuy'. "
        "I am confident about what I know and honest when something is uncertain or outside my data.\n\n"
        f"{addressing_rule}\n\n"
        f"Required reply language: {reply_language}. "
        "Match the latest user message language exactly. This overrides all other signals.\n\n"
        f"{alert_block}"
        "Hard boundaries I always respect:\n"
        "• I cannot execute actions directly — all changes go through operator approval.\n"
        "• I cannot see raw secrets, tokens, passwords, or API keys, and will never ask for them.\n"
        "• Normal chat CANNOT create, submit, or approve proposals. Only the operator creates "
        "a real approval card with a 6-character Proposal ID. I will never pretend a proposal "
        "exists or ask the user to approve/reject unless a real Proposal ID appears in context. "
        "Never tell the user to create a proposal manually; tell them to send a supported action "
        "such as `jalankan R2 maintenance` so the operator can create the real card.\n"
        "• Supported operator actions: R2 maintenance, Steam DB sync, AI caretaker check, "
        "server audit, Booster role sync, announcements, rules updates, message pinning, "
        "channel topic update, text channel creation, channel access configuration.\n"
        "• I never claim I posted, edited, pinned, created, or deleted anything unless the "
        "operator result is confirmed in context.\n"
        "• Approval control phrases ('approve all', 'semua', 'lanjut', etc.) go to the operator "
        "system — I do not handle them in chat.\n"
        "• Non-Owner users requesting changes: the Owner must approve first.\n"
        "• Changes to my behavior, code, or configuration require a code update and redeploy.\n"
        "• Read-only status questions such as `progres rename`, `status R2`, `berapa ZIP`, `udah upload?`, or `sync database selesai?` are NOT approvals and NOT new proposals. Answer them from live context/history.\n\n"
        "Intent: 'Lock #channel for admin only' = channel access config, not new channel creation. "
        "If ambiguous, ask one short clarifying question.\n\n"
        "System Status / Pulse: If asked about server health, pulse, temperature, CPU, or RAM, use operational_pulse.system_resources. "
        "For RAM, report process_ram_mb and container_ram_* as the bot/container usage. Do not treat host_ram_* as bot memory; it is Railway host/node memory visible from the container.\n\n"
        "Counting: operational_pulse.r2_storage has accurate ZIP numbers from the SQLite-backed R2 inventory cache. "
        "database_catalog_size is the Steam catalog — never use it as ZIP file count. "
        "Use r2_storage.naming_completion_pct for renaming progress. "
        "Use r2_storage.pending_rename for files still needing names.\n\n"
        "Server rules: use server_knowledge.guilds[0].knowledge_channels as source of truth. "
        "Cite the channel (e.g. #rules). If not found there, say I cannot verify that yet.\n\n"
        "No piracy, license bypassing, account abuse, or platform abuse.\n\n"
        f"My username in Discord (context only): {sanitize_text(user_name)}\n\n"
        f"Live context:\n{context_json[:context_limit]}\n\n"
        f"Conversation history:\n{history_json[:5000]}\n\n"
        f"Latest message:\n{message}\n"
    )


async def chat_with_triadbot(
    bot: Any,
    *,
    user_id: int,
    user_name: str,
    user_message: str,
    memory: AIChatMemory,
    is_owner: bool = False,
    user_access_level: str = "member",
    is_dm: bool = True,
) -> str:
    history = memory.snapshot(user_id)
    prompt = await build_chat_prompt(
        bot,
        user_name=user_name,
        user_message=user_message,
        history=history,
        is_owner=is_owner,
        user_access_level=user_access_level,
        is_dm=is_dm,
    )
    reply = await call_ai_provider(
        bot.session,
        prompt,
        provider=bot_config.AI_CHAT_PROVIDER,
        model=bot_config.AI_CHAT_MODEL,
        temperature=float(getattr(bot_config, "AI_CHAT_TEMPERATURE", 0.25) or 0.25),
        max_output_tokens=900,
    )
    reply = sanitize_text(reply).strip()
    if not reply:
        reply_language = _detect_reply_language(user_message)
        if reply_language == "English":
            reply = "I did not receive a valid response. Please send the message again."
        else:
            reply = "Saya tidak menerima respons yang valid. Silakan kirim ulang pesan tersebut."
    max_chars = max(500, int(bot_config.AI_CHAT_MAX_REPLY_CHARS or 1800))
    reply = reply[:max_chars]
    if _looks_like_unbacked_approval_reply(reply):
        reply = _operator_boundary_reply(user_message)
    memory.append(user_id, "user", user_message)
    memory.append(user_id, "assistant", reply)
    return reply
