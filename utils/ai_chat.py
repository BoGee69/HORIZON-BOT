"""
Personal Gemini chat helper for TriadBot DMs.
"""
from __future__ import annotations

import json
from collections import defaultdict, deque
from typing import Any

import config as bot_config
from utils.ai_caretaker import AICaretakerUnavailable, call_gemini, sanitize_data, sanitize_text
from utils.diagnostics import collect_health


class AIChatMemory:
    def __init__(self, max_history: int):
        self.max_history = max(2, int(max_history or 12))
        self._history: dict[int, deque[dict[str, str]]] = defaultdict(lambda: deque(maxlen=self.max_history))

    def append(self, user_id: int, role: str, text: str) -> None:
        self._history[user_id].append({"role": role, "text": sanitize_text(text)[:2000]})

    def snapshot(self, user_id: int) -> list[dict[str, str]]:
        return list(self._history[user_id])


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


async def build_chat_prompt(bot: Any, *, user_name: str, user_message: str, history: list[dict[str, str]]) -> str:
    health = await collect_health(bot)
    recent_events = getattr(bot, "ai_events", None)
    context = {
        "bot_health": _compact_health(health),
        "recent_events": recent_events.snapshot(8) if recent_events else [],
        "last_r2_maintenance": sanitize_data(
            getattr(getattr(bot, "last_r2_maintenance_summary", None), "to_fields", lambda: None)()
        ),
        "last_steam_db_sync": sanitize_data(
            getattr(getattr(bot, "last_steam_db_sync_summary", None), "to_fields", lambda: None)()
        ),
    }
    context_json = json.dumps(context, ensure_ascii=False, sort_keys=True)
    history_json = json.dumps(sanitize_data(history[-bot_config.AI_CHAT_MAX_HISTORY:]), ensure_ascii=False)
    message = sanitize_text(user_message)[: bot_config.AI_CHAT_MAX_MESSAGE_CHARS]

    return (
        "Kamu adalah TriadBot, personal AI companion dan ops assistant untuk owner bot Discord ini.\n"
        "Persona: santai, hangat, sedikit witty, ngobrol pakai bahasa Indonesia natural. "
        "Kalau user pakai 'gw/lu', kamu boleh pakai 'gw/lu'. Jangan kaku seperti dokumentasi.\n"
        "Kamu boleh bantu konsultasi bot, Railway, R2, Discord, debugging, ide fitur, dan planning.\n"
        "Batasan penting: kamu tidak bisa menjalankan aksi langsung, tidak bisa melihat secret mentah, "
        "tidak boleh meminta token/password/API key, dan tidak boleh membocorkan data sensitif. "
        "Kalau user minta tindakan berisiko, arahkan ke langkah aman dan manual.\n"
        "Jangan membantu pembajakan, bypass lisensi, atau penyalahgunaan akun/platform. "
        "Fokus pada keamanan, reliability, maintenance, dan penggunaan yang sah.\n"
        "Jawab singkat, jelas, dan terasa seperti TriadBot yang hidup di DM, bukan laporan robot.\n\n"
        f"Nama user: {sanitize_text(user_name)}\n"
        f"Konteks bot aman:\n{context_json[:6000]}\n\n"
        f"Riwayat chat singkat:\n{history_json[:5000]}\n\n"
        f"Pesan user sekarang:\n{message}\n"
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
        reply = "Gw kebaca blank barusan. Coba ulang bentar ya."
    max_chars = max(500, int(bot_config.AI_CHAT_MAX_REPLY_CHARS or 1800))
    reply = reply[:max_chars]
    memory.append(user_id, "user", user_message)
    memory.append(user_id, "assistant", reply)
    return reply
