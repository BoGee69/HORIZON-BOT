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
        if not message.content.strip():
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
                    reply = await chat_with_triadbot(
                        self.bot,
                        user_id=message.author.id,
                        user_name=str(message.author),
                        user_message=message.content,
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
