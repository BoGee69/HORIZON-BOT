"""
Personal DM chat with TriadBot through the configured AI provider.
"""
from __future__ import annotations

import asyncio
import logging
import time

import discord
from discord.ext import commands

import config as bot_config
from utils.ai_caretaker import AICaretakerUnavailable, sanitize_text
from utils.ai_chat import AIChatMemory, chat_with_triadbot
from utils.attachments import read_message_attachments
from utils.r2_inventory import get_r2_inventory_snapshot_async

log = logging.getLogger(__name__)


class AIChat(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.memory = AIChatMemory(bot_config.AI_CHAT_MAX_HISTORY)
        self._locks: dict[int, asyncio.Lock] = {}
        self._last_reply_at: dict[int, float] = {}

    def _allowed(self, user_id: int) -> bool:
        return bool(bot_config.AI_CHAT_ALLOWED_IDS and user_id in bot_config.AI_CHAT_ALLOWED_IDS)

    def _cooling_down(self, user_id: int) -> bool:
        cooldown = max(0.0, float(bot_config.AI_CHAT_COOLDOWN_SECONDS or 0))
        if cooldown <= 0:
            return False
        last = self._last_reply_at.get(user_id, 0.0)
        now = time.time()
        if now - last < cooldown:
            return True
        self._last_reply_at[user_id] = now
        return False

    async def _reply_chunks(self, message: discord.Message, text: str) -> None:
        clean = sanitize_text(text).strip()
        if not clean:
            clean = "Saya belum bisa menyusun respons yang valid. Silakan kirim ulang pesan tersebut."
        chunks = [clean[i : i + 1900] for i in range(0, len(clean), 1900)]
        for chunk in chunks[:3]:
            await message.channel.send(chunk)

    def _wants_zip_name_stats(self, text: str, user_id: int) -> bool:
        lower = sanitize_text(text).lower()
        has_count = any(word in lower for word in ("berapa", "total", "how many", "count", "jumlah"))
        has_name_update = any(
            word in lower
            for word in ("nama", "name", "update", "updated", "rename", "renamed", "ganti")
        )
        if "zip" in lower and has_count and has_name_update:
            return True
        if has_count and any(word in lower for word in ("sekarang", "current", "now", "saat ini")):
            recent = " ".join(item["text"] for item in self.memory.snapshot(user_id)[-4:]).lower()
            if any(marker in recent for marker in ("zip", "r2", "rename", "nama game", "appid")):
                return True
            # In owner DM, short follow-ups like "totalnya sekarang berapa?"
            # usually refer to the active R2/ZIP maintenance topic.
            return "total" in lower or "berapa" in lower
        return False

    async def _zip_name_stats_reply(self) -> str:
        inventory = await get_r2_inventory_snapshot_async(
            prefix=bot_config.R2_MAINTENANCE_PREFIX,
            cache_seconds=0,
            max_pages=bot_config.AI_CHAT_R2_STATS_MAX_PAGES,
        )
        if inventory.get("error"):
            return (
                "Saya belum bisa memverifikasi jumlah nama ZIP langsung dari R2 saat ini.\n"
                f"Error: `{sanitize_text(inventory.get('error'))[:400]}`"
            )

        total_zip = int(inventory.get("zip_objects_counted") or 0)
        named_zip = int(inventory.get("named_zip_objects_counted") or 0)
        appid_only = int(inventory.get("appid_only_zip_objects_counted") or 0)
        unknown = int(inventory.get("unknown_zip_objects_counted") or 0)
        remaining = appid_only + unknown
        last_summary = getattr(self.bot, "last_r2_maintenance_summary", None)
        last_renamed = getattr(last_summary, "rename_applied", None)
        total_renamed_by_bot = getattr(last_summary, "total_rename_applied", None)

        lines = [
            "Saya hitung langsung dari R2, bukan dari total katalog games.json:",
            "",
            f"- Total ZIP di R2: `{total_zip:,}`",
            f"- ZIP yang sudah format `Nama Game (AppID).zip`: `{named_zip:,}`",
            f"- ZIP yang masih AppID-only: `{appid_only:,}`",
            f"- ZIP dengan format belum dikenali: `{unknown:,}`",
            f"- Estimasi ZIP yang masih perlu dirapikan: `{remaining:,}`",
        ]
        if last_renamed is not None:
            lines.append(f"- Rename applied pada run maintenance terakhir: `{int(last_renamed):,}`")
        if total_renamed_by_bot:
            lines.append(
                f"- Total rename yang dicatat bot sejak counter aktif: `{int(total_renamed_by_bot):,}`"
            )
        if inventory.get("truncated"):
            lines.append("")
            lines.append("Catatan: scan R2 terpotong karena batas halaman, jadi angka ini belum full bucket.")
        return "\n".join(lines)

    async def _message_text_with_attachments(self, message: discord.Message) -> str:
        text = sanitize_text(message.content).strip()
        attachments = list(getattr(message, "attachments", []) or [])
        if not attachments:
            return text

        result = await read_message_attachments(
            self.bot.session,
            attachments,
            purpose="personal DM chat context",
        )
        parts = [text] if text else []
        if result.text:
            parts.append("[Attachment content]\n" + result.text)
        if result.warnings:
            parts.append("[Attachment notes]\n" + "; ".join(result.warnings[:4]))
        return "\n\n".join(parts).strip()

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot:
            return
        if not bot_config.AI_CHAT_ENABLED:
            return
        if not isinstance(message.channel, discord.DMChannel):
            return
        if not self._allowed(message.author.id):
            return
        operator = getattr(self.bot, "ai_operator", None)
        if operator and operator.is_operator_command(message.content, message.author.id):
            return
        if not message.content.strip() and not getattr(message, "attachments", None):
            return
        if self._cooling_down(message.author.id):
            return

        lock = self._locks.setdefault(message.author.id, asyncio.Lock())
        if lock.locked():
            await message.channel.send("Saya masih memproses pesan sebelumnya. Mohon tunggu sebentar.")
            return

        async with lock:
            try:
                async with message.channel.typing():
                    user_message = await self._message_text_with_attachments(message)
                    if not user_message:
                        await message.channel.send("Saya belum bisa membaca isi attachment itu.")
                        return
                    if self._wants_zip_name_stats(user_message, message.author.id):
                        reply = await self._zip_name_stats_reply()
                        self.memory.append(message.author.id, "user", user_message)
                        self.memory.append(message.author.id, "assistant", reply)
                    else:
                        reply = await chat_with_triadbot(
                            self.bot,
                            user_id=message.author.id,
                            user_name=str(message.author),
                            user_message=user_message,
                            memory=self.memory,
                        )
                await self._reply_chunks(message, reply)
                if hasattr(self.bot, "record_ai_event"):
                    self.bot.record_ai_event(
                        "info",
                        "ai_chat",
                        "AI chat replied to an allowed DM.",
                        {"user_id": str(message.author.id)},
                    )
            except AICaretakerUnavailable as exc:
                log.warning("AI chat unavailable: %s", exc)
                await message.channel.send(
                    "AI provider utama belum siap. "
                    f"Provider: `{bot_config.AI_CHAT_PROVIDER}`, model: `{bot_config.AI_CHAT_MODEL}`. "
                    "Cek API key, quota, dan nama model di Railway variables."
                )
            except Exception as exc:
                log.exception("AI chat failed")
                await message.channel.send("Saya mengalami error saat menyusun jawaban. Silakan coba lagi sebentar.")
                if hasattr(self.bot, "record_ai_event"):
                    self.bot.record_ai_event(
                        "error",
                        "ai_chat",
                        "AI chat failed.",
                        {"error": repr(exc)[:800]},
                    )


async def setup(bot):
    await bot.add_cog(AIChat(bot))
