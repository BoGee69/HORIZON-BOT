"""
Personal AI chat helper for TriadBot DMs.
"""
from __future__ import annotations

import json
import re
from collections import defaultdict, deque
from typing import Any

import config as bot_config
from utils.ai_caretaker import call_ai_provider, sanitize_data, sanitize_text
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
            "r2_named_zip_objects_counted": "actual ZIP objects in R2 already named as Game Name (AppID).zip",
            "r2_appid_only_zip_objects_counted": "actual ZIP objects in R2 still named as AppID.zip or placeholder AppID format",
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
        "passwords, or API keys, and must not reveal sensitive data. If the Owner asks for a bot-changing action, "
        "explain that I can prepare an owner-approval proposal. Never claim that I posted, edited, pinned, created, "
        "deleted, synced, or changed anything unless the operator result is present in recent context. Whitelisted "
        "proposal requests include R2 maintenance, Steam DB sync, AI caretaker checks, server audits, Booster role "
        "sync, announcements, rules updates, message pinning, channel topic updates, and text channel creation. "
        "The Owner must approve the proposal before anything changes.\n"
        "Do not assist piracy, license bypassing, account abuse, or platform abuse. Focus on security, reliability, "
        "maintenance, and legitimate usage.\n"
        "Keep answers concise, clear, and based on system status when relevant.\n\n"
        "Counting rule: never use bot_health.database.total_games as the count of files, ZIP files, "
        "or R2 objects. That number is the Steam catalog size in games.json. "
        "For questions about R2 files, ZIP files, stored objects, or actual uploaded game archives, "
        "use r2_inventory.zip_objects_counted when available. If r2_inventory has an error or is unavailable, "
        "say that I cannot verify the live R2 file count right now. "
        "For questions about how many ZIP filenames have been updated/renamed to include game names, "
        "use r2_inventory.named_zip_objects_counted. For ZIP files still needing name updates, use "
        "r2_inventory.appid_only_zip_objects_counted plus r2_inventory.unknown_zip_objects_counted. "
        "For the latest maintenance batch only, use last_r2_maintenance.Rename applied. "
        "For questions about catalog/database entries only, use bot_health.database.total_games.\n\n"
        f"Discord username, only for context and not for addressing unless asked: {sanitize_text(user_name)}\n"
        f"Safe bot context:\n{context_json[:6000]}\n\n"
        f"Short chat history:\n{history_json[:5000]}\n\n"
        f"Latest Owner message:\n{message}\n"
    )


async def chat_with_triadbot(bot: Any, *, user_id: int, user_name: str, user_message: str, memory: AIChatMemory) -> str:
    history = memory.snapshot(user_id)
    prompt = await build_chat_prompt(bot, user_name=user_name, user_message=user_message, history=history)
    reply = await call_ai_provider(
        bot.session,
        prompt,
        provider=bot_config.AI_CHAT_PROVIDER,
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
