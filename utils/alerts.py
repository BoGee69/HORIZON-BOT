"""
Admin DM alert helper.
"""
import logging
import time
from typing import Iterable, Optional

import discord

from config import ADMIN_ALERT_COOLDOWN_SECONDS, ADMIN_ALERT_IDS, COLOR_ERROR, COLOR_INFO, COLOR_WARNING

log = logging.getLogger(__name__)


class AdminNotifier:
    def __init__(
        self,
        bot,
        admin_ids: Optional[Iterable[int]] = None,
        cooldown_seconds: int = ADMIN_ALERT_COOLDOWN_SECONDS,
    ):
        self.bot = bot
        self.admin_ids = list(admin_ids or ADMIN_ALERT_IDS)
        self.cooldown_seconds = cooldown_seconds
        self._sent_at: dict[str, float] = {}

    async def send(
        self,
        title: str,
        description: str,
        *,
        level: str = "warning",
        fields: Optional[dict[str, str]] = None,
        key: Optional[str] = None,
        force: bool = False,
    ) -> int:
        if not self.admin_ids:
            return 0

        key = key or title
        now = time.time()
        if not force and self.cooldown_seconds > 0:
            last_sent = self._sent_at.get(key, 0)
            if now - last_sent < self.cooldown_seconds:
                return 0

        color = {
            "error": COLOR_ERROR,
            "warning": COLOR_WARNING,
            "info": COLOR_INFO,
        }.get(level, COLOR_WARNING)

        embed = discord.Embed(
            title=title[:256],
            description=description[:4096],
            color=color,
            timestamp=discord.utils.utcnow(),
        )
        for name, value in (fields or {}).items():
            embed.add_field(name=str(name)[:256], value=str(value)[:1024] or "-", inline=False)
        embed.set_footer(text="triadbot admin alert")

        delivered = 0
        for user_id in self.admin_ids:
            try:
                user = self.bot.get_user(user_id) or await self.bot.fetch_user(user_id)
                await user.send(embed=embed)
                delivered += 1
            except discord.Forbidden:
                log.warning("Cannot DM admin %s: DMs are closed.", user_id)
            except discord.HTTPException as exc:
                log.warning("Failed to DM admin %s: %s", user_id, exc)
            except Exception as exc:
                log.warning("Unexpected admin DM alert failure for %s: %s", user_id, exc)

        if delivered:
            self._sent_at[key] = now
        return delivered
