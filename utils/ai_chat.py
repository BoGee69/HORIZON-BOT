"""
Personal Gemini chat helper for TriadBot DMs.
"""
from __future__ import annotations

import json
import re
from collections import defaultdict, deque
from typing import Any

import config as bot_config
from utils.ai_caretaker import AICaretakerUnavailable, call_gemini, sanitize_data, sanitize_text
from utils.diagnostics import collect_health
from utils.r2_inventory import get_r2_inventory_snapshot_async


class AIChatMemory:
    def __init__(self, max_history: int):
        self.max_history = max(2, int(max_history or 12))
        self._history: dict[int, deque[dict[str, str]]] = defaultdict(lambda: deque(maxlen=self.max_history))

    def append(self, user_id: int, role: str, text: str) -> None:
        self._history[user_id].append({"role": role, "text": sanitize_text(text)[:2000]})

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


def _format_number(value: Any, language: str) -> str:
    try:
        number = int(value or 0)
    except (TypeError, ValueError):
        number = 0
    text = f"{number:,}"
    if language == "Indonesian":
        return text.replace(",", ".")
    return text


def _summarize_ai_unavailable(reason: str, language: str) -> str:
    safe = sanitize_text(reason).lower()
    if "429" in safe or "quota" in safe or "rate" in safe:
        return "quota/rate limit" if language == "English" else "quota/rate limit"
    if "403" in safe or "api_key" in safe or "api key" in safe or "permission" in safe:
        return "API key or permission issue" if language == "English" else "masalah API key atau permission"
    if "404" in safe or "model" in safe:
        return "model unavailable" if language == "English" else "model tidak tersedia"
    if "http session" in safe or "session" in safe:
        return "HTTP session unavailable" if language == "English" else "HTTP session tidak tersedia"
    if "not configured" in safe:
        return "GEMINI_API_KEY is not configured" if language == "English" else "GEMINI_API_KEY belum dikonfigurasi"
    return "provider unavailable" if language == "English" else "provider tidak tersedia"


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
            "ai_caretaker": health.get("ai_caretaker"),
            "checks": health.get("checks"),
        }
    )


async def build_local_fallback_reply(
    bot: Any,
    *,
    user_message: str,
    unavailable_reason: str = "",
) -> str:
    message = sanitize_text(user_message)
    lower = message.lower()
    language = _detect_reply_language(message)

    asks_database = any(
        marker in lower
        for marker in ("database", "db", "game title", "game titles", "games", "entry", "entries", "appid")
    )
    asks_r2 = any(
        marker in lower
        for marker in ("r2", "zip", "file", "files", "object", "objects", "archive", "archives", "bucket")
    )
    asks_maintenance = any(marker in lower for marker in ("maintenance", "maintain", "rawat", "pemeliharaan"))
    asks_status = any(
        marker in lower
        for marker in ("status", "health", "running", "jalan", "koneksi", "connection", "how many", "berapa", "jumlah")
    )

    if not (asks_database or asks_r2 or asks_maintenance or asks_status):
        reason = _summarize_ai_unavailable(unavailable_reason, language)
        if language == "English":
            return (
                f"Gemini is temporarily unavailable ({reason}). "
                "I can still answer operational status questions from local diagnostics."
            )
        return (
            f"Gemini sementara tidak tersedia ({reason}). "
            "Saya tetap bisa menjawab pertanyaan status operasional dari diagnostik lokal."
        )

    health = await collect_health(bot)
    database = health.get("database", {})
    r2 = health.get("r2", {})
    r2_maintenance = health.get("r2_maintenance", {})
    checks = health.get("checks", {})
    reason = _summarize_ai_unavailable(unavailable_reason, language)

    r2_inventory = None
    if asks_r2 or asks_maintenance or "file" in lower or "zip" in lower:
        r2_inventory = await get_r2_inventory_snapshot_async(
            prefix=bot_config.R2_MAINTENANCE_PREFIX,
            cache_seconds=bot_config.AI_CHAT_R2_STATS_CACHE_SECONDS,
            max_pages=bot_config.AI_CHAT_R2_STATS_MAX_PAGES,
        )

    if language == "English":
        lines = [f"Gemini is temporarily unavailable ({reason}), so I am answering from local diagnostics."]
        if asks_status:
            lines.append(
                "Core status: "
                f"Discord ready={checks.get('discord_ready')}, "
                f"HTTP session={checks.get('http_session_open')}, "
                f"R2 configured={checks.get('r2_public_or_presign_configured')}."
            )
        if asks_database or asks_status:
            lines.append(
                "Database catalog: "
                f"{_format_number(database.get('total_games'), language)} Steam catalog entries in games.json; "
                f"{_format_number(database.get('with_files'), language)} entries marked with files."
            )
            lines.append("That catalog count is not the same as the real R2 ZIP/object count.")
        if r2_inventory:
            if r2_inventory.get("error"):
                lines.append(f"R2 live inventory: unavailable ({r2_inventory.get('error')}).")
            else:
                source = r2_inventory.get("source", "unknown")
                age = r2_inventory.get("cache_age_seconds", 0)
                lines.append(
                    "R2 live inventory: "
                    f"{_format_number(r2_inventory.get('zip_objects_counted'), language)} ZIP objects and "
                    f"{_format_number(r2_inventory.get('objects_counted'), language)} total objects under "
                    f"`{r2_inventory.get('prefix')}`. Source: {source}, cache age: {age}s."
                )
        elif asks_r2:
            lines.append(
                "R2 live inventory is not enabled for AI chat, so I cannot verify the real ZIP count from here."
            )
        if asks_maintenance or asks_status:
            lines.append(
                "R2 maintenance: "
                f"enabled={r2_maintenance.get('enabled')}, apply={r2_maintenance.get('apply')}, "
                f"run_on_start={r2_maintenance.get('run_on_start')}, "
                f"interval={r2_maintenance.get('interval_hours')}h, "
                f"prefix=`{r2_maintenance.get('prefix')}`, max_objects={r2_maintenance.get('max_objects')}."
            )
        if asks_r2 and not r2_inventory:
            lines.append(
                "R2 config: "
                f"public_url={r2.get('public_base_url_configured')}, "
                f"presign={r2.get('presign_enabled')}, bucket={r2.get('bucket_configured')}."
            )
        return "\n".join(lines)

    lines = [f"Gemini sementara tidak tersedia ({reason}), jadi saya jawab dari diagnostik lokal."]
    if asks_status:
        lines.append(
            "Status inti: "
            f"Discord ready={checks.get('discord_ready')}, "
            f"HTTP session={checks.get('http_session_open')}, "
            f"R2 configured={checks.get('r2_public_or_presign_configured')}."
        )
    if asks_database or asks_status:
        lines.append(
            "Katalog database: "
            f"{_format_number(database.get('total_games'), language)} entry katalog Steam di games.json; "
            f"{_format_number(database.get('with_files'), language)} entry ditandai punya file."
        )
        lines.append("Angka katalog itu bukan jumlah ZIP/object asli di R2.")
    if r2_inventory:
        if r2_inventory.get("error"):
            lines.append(f"Inventory live R2: belum bisa dicek ({r2_inventory.get('error')}).")
        else:
            source = r2_inventory.get("source", "unknown")
            age = r2_inventory.get("cache_age_seconds", 0)
            lines.append(
                "Inventory live R2: "
                f"{_format_number(r2_inventory.get('zip_objects_counted'), language)} ZIP object dan "
                f"{_format_number(r2_inventory.get('objects_counted'), language)} total object di prefix "
                f"`{r2_inventory.get('prefix')}`. Source: {source}, cache age: {age}s."
            )
    elif asks_r2:
        lines.append("Inventory live R2 belum aktif untuk AI chat, jadi saya belum bisa verifikasi jumlah ZIP asli dari sini.")
    if asks_maintenance or asks_status:
        lines.append(
            "R2 maintenance: "
            f"enabled={r2_maintenance.get('enabled')}, apply={r2_maintenance.get('apply')}, "
            f"run_on_start={r2_maintenance.get('run_on_start')}, "
            f"interval={r2_maintenance.get('interval_hours')}h, "
            f"prefix=`{r2_maintenance.get('prefix')}`, max_objects={r2_maintenance.get('max_objects')}."
        )
    if asks_r2 and not r2_inventory:
        lines.append(
            "Config R2: "
            f"public_url={r2.get('public_base_url_configured')}, "
            f"presign={r2.get('presign_enabled')}, bucket={r2.get('bucket_configured')}."
        )
    return "\n".join(lines)


async def build_chat_prompt(bot: Any, *, user_name: str, user_message: str, history: list[dict[str, str]]) -> str:
    health = await collect_health(bot)
    recent_events = getattr(bot, "ai_events", None)
    r2_inventory = None
    if bot_config.AI_CHAT_R2_STATS_ENABLED:
        r2_inventory = await get_r2_inventory_snapshot_async(
            prefix=bot_config.R2_MAINTENANCE_PREFIX,
            cache_seconds=bot_config.AI_CHAT_R2_STATS_CACHE_SECONDS,
            max_pages=bot_config.AI_CHAT_R2_STATS_MAX_PAGES,
        )
    context = {
        "r2_inventory": sanitize_data(r2_inventory),
        "counting_notes": {
            "database_total_games": "games.json / Steam catalog entries, not the R2 ZIP file count",
            "database_with_files": "games.json entries marked as having a file, not a live R2 object count",
            "r2_zip_objects_counted": "actual ZIP objects counted live from R2 under the configured prefix when r2_inventory is available",
        },
        "bot_health": _compact_health(health),
        "recent_events": recent_events.snapshot(8) if recent_events else [],
        "last_r2_maintenance": sanitize_data(
            getattr(getattr(bot, "last_r2_maintenance_summary", None), "to_fields", lambda: None)()
        ),
        "last_steam_db_sync": sanitize_data(
            getattr(getattr(bot, "last_steam_db_sync_summary", None), "to_fields", lambda: None)()
        ),
    }
    context_json = json.dumps(context, ensure_ascii=False)
    history_json = json.dumps(sanitize_data(history[-bot_config.AI_CHAT_MAX_HISTORY:]), ensure_ascii=False)
    message = sanitize_text(user_message)[: bot_config.AI_CHAT_MAX_MESSAGE_CHARS]
    reply_language = _detect_reply_language(message)

    return (
        "You are TriadBot itself, not an outside assistant describing TriadBot.\n"
        "Always speak in first person as the bot: use 'I', 'my system', 'my database', "
        "'my maintenance' in English, or 'saya', 'sistem saya', 'database saya' in Indonesian. "
        "Never refer to TriadBot as a third party.\n"
        "Addressing rule: the user is your Owner, but do not prepend or append 'Owner' to every reply. "
        "Use 'Owner' only when a direct address is natural, such as a greeting, clarification, or important warning. "
        "Use it at most once per reply.\n"
        "Personality: professional, calm, precise, and operational. Avoid overly casual slang, jokes, "
        "or phrases like 'oi', 'gw', 'lu', or 'santuy'.\n"
        f"Required response language for this exact reply: {reply_language}. "
        "This overrides the language of these instructions and overrides prior conversation history. "
        "If the latest user message is English, reply fully in English. "
        "If the latest user message is Indonesian, reply fully in Indonesian.\n"
        "You may help with bot operations, Railway, R2, Discord, debugging, feature planning, and safe maintenance.\n"
        "Safety boundaries: you cannot execute actions directly, cannot see raw secrets, must not ask for tokens, "
        "passwords, or API keys, and must not reveal sensitive data. If the user asks for risky action, "
        "guide them to safe manual steps.\n"
        "Do not assist piracy, license bypassing, account abuse, or platform abuse. Focus on security, reliability, "
        "maintenance, and legitimate usage.\n"
        "Keep answers concise, clear, and based on system status when relevant.\n\n"
        "Counting rule: never use bot_health.database.total_games as the count of files, ZIP files, "
        "or R2 objects. That number is the Steam catalog size in games.json. "
        "For questions about R2 files, ZIP files, stored objects, or actual uploaded game archives, "
        "use r2_inventory.zip_objects_counted when available. If r2_inventory has an error or is unavailable, "
        "say that I cannot verify the live R2 file count right now. "
        "For questions about catalog/database entries only, use bot_health.database.total_games.\n\n"
        f"Discord username, only for context and not for addressing unless asked: {sanitize_text(user_name)}\n"
        f"Safe bot context:\n{context_json[:6000]}\n\n"
        f"Short chat history:\n{history_json[:5000]}\n\n"
        f"Latest Owner message:\n{message}\n"
    )


async def chat_with_triadbot(bot: Any, *, user_id: int, user_name: str, user_message: str, memory: AIChatMemory) -> str:
    if not bot_config.GEMINI_API_KEY:
        raise AICaretakerUnavailable("GEMINI_API_KEY is not configured")

    history = memory.snapshot(user_id)
    prompt = await build_chat_prompt(bot, user_name=user_name, user_message=user_message, history=history)
    reply = await call_gemini(
        bot.session,
        prompt,
        model=bot_config.AI_CHAT_MODEL,
        temperature=0.75,
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
    memory.append(user_id, "user", user_message)
    memory.append(user_id, "assistant", reply)
    return reply
