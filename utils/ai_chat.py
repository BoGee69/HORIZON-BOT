"""
Personal AI chat helper for TriadBot DMs.
"""
from __future__ import annotations

import json
import re
import time
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


def _looks_like_unbacked_approval_reply(text: str) -> bool:
    clean = sanitize_text(text).strip().lower()
    if not clean or re.search(r"\b[a-f0-9]{6}\b", clean):
        return False
    approval_terms = (
        "proposal",
        "approval",
        "approve",
        "reject",
        "persetujuan",
        "setuju",
        "konfirmasi",
    )
    fake_claim_terms = (
        "prepared",
        "ready",
        "submitted",
        "registered",
        "created the proposal",
        "proposal is ready",
        "proposal perubahan",
        "menyiapkan proposal",
        "membuat proposal",
        "saya telah menyiapkan",
        "siap untuk",
        "siap dieksekusi",
        "silakan approve",
        "reply approve",
        "balas approve",
    )
    return any(term in clean for term in approval_terms) and any(term in clean for term in fake_claim_terms)


def _operator_boundary_reply(user_message: str) -> str:
    if _detect_reply_language(user_message) == "English":
        return (
            "I cannot create approvals from normal chat. A real owner approval must come from "
            "the operator and will show an `Owner approval required` card with a `Proposal ID`. "
            "Please send a specific supported action, such as `send announcement to #announcement: Test`, "
            "`update rules in #rules: ...`, or `configure #welcome so only Admin can send messages`."
        )
    return (
        "Saya tidak bisa membuat approval dari chat biasa. Proposal resmi harus dibuat oleh operator "
        "dan akan muncul sebagai card `Owner approval required` dengan `Proposal ID`. Kirim instruksi "
        "aksi yang spesifik, misalnya `kirim announcement di #announcement: Test`, "
        "`buat rules di #rules: ...`, atau `atur #welcome hanya Admin yang bisa kirim pesan`."
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
        guilds = [guild for guild in guilds if getattr(guild, "id", None) in configured]
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
            "rules",
            "rule",
            "peraturan",
            "panduan",
            "guide",
            "resource",
            "announcement",
            "pengumuman",
            "link-invite",
            "welcome",
        )
    )


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
    return "\n".join(part for part in parts if part).strip()


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
        guild_item: dict[str, Any] = {
            "name": sanitize_text(getattr(guild, "name", "")),
            "id": str(getattr(guild, "id", "")),
            "member_count": getattr(guild, "member_count", None),
            "roles": [],
            "channels": [],
            "knowledge_channels": [],
        }
        for role in list(getattr(guild, "roles", []) or []):
            role_name = sanitize_text(getattr(role, "name", "") or "")
            if role_name and role_name != "@everyone":
                guild_item["roles"].append(role_name[:80])
        guild_item["roles"] = guild_item["roles"][:40]

        text_channels = list(getattr(guild, "text_channels", []) or [])
        guild_item["channels"] = [
            {
                "name": sanitize_text(getattr(channel, "name", "") or "")[:80],
                "id": str(getattr(channel, "id", "")),
                "category": sanitize_text(getattr(getattr(channel, "category", None), "name", "") or "")[:80],
                "topic": sanitize_text(getattr(channel, "topic", "") or "")[:200],
            }
            for channel in text_channels[:80]
        ]

        for channel in text_channels:
            if not _is_knowledge_channel(channel) or used_chars >= max_chars:
                continue
            channel_item = {
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
                    channel_item["messages"].append(
                        {
                            "author": sanitize_text(str(getattr(message, "author", "")))[:80],
                            "text": text,
                        }
                    )
            except Exception as exc:
                channel_item["error"] = sanitize_text(str(exc))[:180]
            if channel_item["messages"] or channel_item.get("topic"):
                guild_item["knowledge_channels"].append(channel_item)

        guild_items.append(guild_item)

    data = sanitize_data(
        {
            "enabled": True,
            "created_at": int(now),
            "guilds": guild_items,
            "notes": [
                "Use knowledge_channels as server rules/guides/source-of-truth when answering server questions.",
                "When citing a rule or guide, mention the channel name such as #rules or #resources.",
            ],
        }
    )
    setattr(bot, "ai_server_knowledge_cache", {"created_at": now, "data": data})
    return data


async def build_chat_prompt(
    bot: Any,
    *,
    user_name: str,
    user_message: str,
    history: list[dict[str, str]],
    is_owner: bool = False,
) -> str:
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
        "server_knowledge": await collect_server_knowledge(bot),
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
    context_limit = max(
        6000,
        min(
            24000,
            int(getattr(bot_config, "AI_MAINTENANCE_MAX_PROMPT_CHARS", 12000) or 12000),
            int(getattr(bot_config, "AI_CHAT_SERVER_KNOWLEDGE_MAX_CHARS", 9000) or 9000) + 7000,
        ),
    )
    history_json = json.dumps(sanitize_data(history[-bot_config.AI_CHAT_MAX_HISTORY:]), ensure_ascii=False)
    message_limit = max(500, int(bot_config.AI_CHAT_MAX_MESSAGE_CHARS or 1800))
    if "[Attachment content]" in user_message:
        message_limit = max(message_limit, int(bot_config.AI_ATTACHMENT_MAX_TEXT_CHARS or 12000))
    message = sanitize_text(user_message)[:message_limit]
    reply_language = _detect_reply_language(message)
    addressing_rule = (
        "Addressing rule: the user is your Owner, but do not prepend or append 'Owner' to routine replies. "
        if is_owner
        else "Addressing rule: the user is a TriadGames member, not the Owner. Do not call them Owner. "
    )

    return (
        "You are TriadBot itself, not an outside assistant describing TriadBot.\n"
        "Always speak in first person as the bot: use 'I', 'my system', 'my database', "
        "'my maintenance' in English, or 'saya', 'sistem saya', 'database saya' in Indonesian. "
        "Never refer to TriadBot as a third party.\n"
        f"{addressing_rule}"
        "Do not start normal answers with 'Owner,' or 'Halo, Owner'. "
        "Use 'Owner' only for important alerts, problems, security warnings, approval confirmations, "
        "or when you must get the Owner's attention. Use it at most once per reply, and only when the "
        "current user is actually the Owner.\n"
        "Personality: professional, calm, precise, and operational. Avoid overly casual slang, jokes, "
        "or phrases like 'oi', 'gw', 'lu', or 'santuy'.\n"
        f"Required response language for this exact reply: {reply_language}. "
        "This overrides the language of these instructions and overrides prior conversation history. "
        "If the latest user message is English, reply fully in English. "
        "If the latest user message is Indonesian, reply fully in Indonesian.\n"
        "You may help with bot operations, Railway, R2, Discord, debugging, feature planning, and safe maintenance.\n"
        "You are part of the TriadGames server operations. Use server_knowledge for server rules, guidance, "
        "channel purpose, announcements, welcome flow, resources, role names, and moderation questions. "
        "If someone asks about server rules or guides, answer from the matching knowledge channel and mention "
        "the source channel, for example #rules or #resources. If the exact rule is not in server_knowledge, "
        "say that you cannot verify the exact section yet instead of inventing it.\n"
        "For third-party Security Bot, use server_admin.security_bot and the latest server audit context to "
        "supervise whether Security Bot is installed, has the expected role, and has enough permissions. "
        "Do not claim you can operate Security Bot's website dashboard or private commands. You can explain "
        "what the Owner should configure in Security Bot, monitor the result from Discord state, and prepare "
        "owner-approved server-side fixes when possible.\n"
        "Safety boundaries: you cannot execute actions directly, cannot see raw secrets, must not ask for tokens, "
        "passwords, or API keys, and must not reveal sensitive data. Critical operator boundary: normal AI chat "
        "cannot create, submit, or approve proposals. Only the AI operator creates a real approval card with a "
        "6-character Proposal ID. Never say a proposal is ready/prepared/submitted, never ask the user to "
        "approve/reject, never invent proposal IDs, and never summarize an approval proposal unless that exact "
        "real Proposal ID is present in recent context. If action requires changes, ask the Owner to send a "
        "specific supported action request; the operator will create the approval card. Whitelisted proposal "
        "requests include R2 maintenance, Steam DB sync, AI caretaker checks, server audits, Booster role sync, "
        "announcements, rules updates, message pinning, channel topic updates, text channel creation, and "
        "channel access configuration. Unsupported actions include deleting channel history, operating another "
        "bot website/private dashboard, and welcome-message automation unless a real operator action exists. "
        "Never claim that I posted, edited, pinned, created, deleted, synced, or changed anything unless the "
        "operator result is present in recent context. If a non-owner asks for changes, explain that the Owner "
        "must approve it.\n"
        "Intent rule: make reasonable assumptions from the latest instruction. If the user says to adjust, lock, "
        "or set an existing #channel so only admin/moderator can chat, treat that as a channel access "
        "configuration request, not a new channel request. If the instruction is unsafe or ambiguous, ask one "
        "short clarifying question.\n"
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
        f"Safe bot context:\n{context_json[:context_limit]}\n\n"
        f"Short chat history:\n{history_json[:5000]}\n\n"
        f"Latest user message:\n{message}\n"
    )


async def chat_with_triadbot(
    bot: Any,
    *,
    user_id: int,
    user_name: str,
    user_message: str,
    memory: AIChatMemory,
    is_owner: bool = False,
) -> str:
    history = memory.snapshot(user_id)
    prompt = await build_chat_prompt(
        bot,
        user_name=user_name,
        user_message=user_message,
        history=history,
        is_owner=is_owner,
    )
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
    if _looks_like_unbacked_approval_reply(reply):
        reply = _operator_boundary_reply(user_message)
    memory.append(user_id, "user", user_message)
    memory.append(user_id, "assistant", reply)
    return reply
