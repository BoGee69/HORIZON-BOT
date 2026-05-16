"""
Automatic Gemini-based caretaker for operational bot health.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Optional

import config as bot_config
from discord.ext import commands
from utils.ai_caretaker import AICaretakerResult, AICaretakerUnavailable, analyze_bot, sanitize_data, sanitize_text
from utils.alerts import AdminNotifier

log = logging.getLogger(__name__)


class AICaretaker(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self._lock = asyncio.Lock()
        self._task: asyncio.Task | None = None
        self._last_trigger_at = 0.0
        self._owner_notifier = AdminNotifier(
            bot,
            admin_ids=bot_config.AI_MAINTENANCE_ALERT_IDS,
            cooldown_seconds=bot_config.AI_MAINTENANCE_COOLDOWN_SECONDS,
        )
        bot.ai_caretaker = self

    async def cog_load(self):
        if bot_config.AI_MAINTENANCE_ENABLED:
            self._task = asyncio.create_task(self._loop())
            log.info("AI caretaker background task enabled")
        else:
            log.info("AI caretaker disabled")

    async def cog_unload(self):
        if getattr(self.bot, "ai_caretaker", None) is self:
            self.bot.ai_caretaker = None
        if self._task:
            self._task.cancel()

    async def _loop(self):
        await self.bot.wait_until_ready()
        if bot_config.AI_MAINTENANCE_START_DELAY_SECONDS > 0:
            await asyncio.sleep(bot_config.AI_MAINTENANCE_START_DELAY_SECONDS)

        await self.trigger("bot-startup", force=True)

        interval_seconds = max(5.0, bot_config.AI_MAINTENANCE_INTERVAL_MINUTES * 60)
        while not self.bot.is_closed():
            await asyncio.sleep(interval_seconds)
            await self.trigger("periodic-health-check")

    def _should_dm(self, result: AICaretakerResult) -> bool:
        if result.status == "OK":
            return bot_config.AI_MAINTENANCE_DM_ON_OK
        if result.status == "CRITICAL":
            return bot_config.AI_MAINTENANCE_DM_ON_CRITICAL
        return bot_config.AI_MAINTENANCE_DM_ON_WARNING

    def _result_fields(self, result: AICaretakerResult, reason: str) -> dict[str, str]:
        fields = {
            "Status": result.status,
            "Trigger": reason,
        }
        if result.causes:
            fields["Likely causes"] = "\n".join(f"- {item}" for item in result.causes)[:1024]
        if result.actions:
            fields["Manual actions"] = "\n".join(f"- {item}" for item in result.actions)[:1024]
        if result.env_to_check:
            fields["Env to check"] = "\n".join(f"- `{item}`" for item in result.env_to_check)[:1024]
        return sanitize_data(fields)

    async def _send_unavailable(self, reason: str, exc: Exception) -> None:
        message = sanitize_text(str(exc))[:1000]
        await self._owner_notifier.send(
            "AI caretaker unavailable",
            "Gemini could not analyze the bot right now. The bot is still running normally.",
            level="warning",
            fields={
                "Trigger": reason,
                "Error": message,
                "Next step": "Check `GEMINI_API_KEY`, free-tier quota, or Gemini API availability.",
            },
            key="ai-caretaker-unavailable",
        )

    async def trigger(
        self,
        reason: str,
        *,
        context: Optional[dict[str, Any]] = None,
        force: bool = False,
    ) -> Optional[AICaretakerResult]:
        if not bot_config.AI_MAINTENANCE_ENABLED:
            return None
        if self._lock.locked():
            return None

        now = time.time()
        cooldown = max(0, bot_config.AI_MAINTENANCE_COOLDOWN_SECONDS)
        if not force and cooldown and now - self._last_trigger_at < cooldown:
            return None

        async with self._lock:
            self._last_trigger_at = time.time()
            safe_context = sanitize_data(context or {})
            try:
                result = await analyze_bot(self.bot, reason=reason, context=safe_context)
                self.bot.last_ai_caretaker_result = result
                if hasattr(self.bot, "record_ai_event"):
                    self.bot.record_ai_event(
                        result.level,
                        "ai_caretaker",
                        f"{result.status}: {result.title}",
                        {"reason": reason, "summary": result.summary},
                    )
            except (AICaretakerUnavailable, asyncio.TimeoutError) as exc:
                log.warning("AI caretaker unavailable for %s: %s", reason, exc)
                await self._send_unavailable(reason, exc)
                return None
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.exception("AI caretaker crashed")
                await self._send_unavailable(reason, exc)
                return None

        if self._should_dm(result):
            await self._owner_notifier.send(
                result.title or "AI caretaker report",
                result.summary or "Gemini returned an empty summary.",
                level=result.level,
                fields=self._result_fields(result, reason),
                key=f"ai-caretaker-{result.status.lower()}-{reason}",
                force=result.status == "CRITICAL",
            )
        return result


async def setup(bot):
    await bot.add_cog(AICaretaker(bot))
