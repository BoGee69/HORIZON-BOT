"""
Passive security guardian for TriadBot.

Default mode is alert-only. It watches for spam bursts, mention floods, and
link floods, then records AI events and notifies admins. Destructive actions
(delete/timeout) are disabled unless explicitly enabled in env.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from datetime import timedelta
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Any

import discord
from discord.ext import commands

import config as bot_config
from utils.ai_caretaker import sanitize_data, sanitize_text

log = logging.getLogger(__name__)

_URL_RE = re.compile(r"https?://|discord\.gg/|discord\.com/invite/", re.I)


@dataclass
class SecurityStats:
    alerts_total: int = 0
    spam_flags: int = 0
    link_flags: int = 0
    mention_flags: int = 0
    auto_actions: int = 0
    recent_alerts: deque[str] = field(default_factory=lambda: deque(maxlen=12))


class AISecurity(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.enabled = bool(getattr(bot_config, "AI_SECURITY_ENABLED", True))
        self.alert_only = bool(getattr(bot_config, "AI_SECURITY_ALERT_ONLY", True))
        self.auto_delete_spam = bool(getattr(bot_config, "AI_SECURITY_AUTO_DELETE_SPAM", False))
        self.auto_timeout_spam = bool(getattr(bot_config, "AI_SECURITY_AUTO_TIMEOUT_SPAM", False))
        self.window_seconds = int(getattr(bot_config, "AI_SECURITY_WINDOW_SECONDS", 12) or 12)
        self.max_messages = int(getattr(bot_config, "AI_SECURITY_MAX_MESSAGES_PER_WINDOW", 7) or 7)
        self.max_mentions = int(getattr(bot_config, "AI_SECURITY_MAX_MENTIONS_PER_MESSAGE", 8) or 8)
        self.max_links = int(getattr(bot_config, "AI_SECURITY_MAX_LINKS_PER_WINDOW", 4) or 4)
        self.alert_cooldown = int(getattr(bot_config, "AI_SECURITY_ALERT_COOLDOWN_SECONDS", 180) or 180)
        self.timeout_seconds = int(getattr(bot_config, "AI_SECURITY_TIMEOUT_SECONDS", 600) or 600)
        self._events: dict[tuple[int, int], deque[dict[str, Any]]] = defaultdict(lambda: deque(maxlen=30))
        self._last_alert: dict[tuple[int, str], float] = {}
        self._stats = SecurityStats()
        bot.ai_security = self

    async def cog_unload(self):
        if getattr(self.bot, "ai_security", None) is self:
            self.bot.ai_security = None

    def snapshot(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "alert_only": self.alert_only,
            "auto_action_enabled": bool(self.auto_delete_spam or self.auto_timeout_spam) and not self.alert_only,
            "window_seconds": self.window_seconds,
            "max_messages_per_window": self.max_messages,
            "max_mentions_per_message": self.max_mentions,
            "max_links_per_window": self.max_links,
            "alerts_total": self._stats.alerts_total,
            "spam_flags": self._stats.spam_flags,
            "link_flags": self._stats.link_flags,
            "mention_flags": self._stats.mention_flags,
            "auto_actions": self._stats.auto_actions,
            "recent_alerts": list(self._stats.recent_alerts),
        }

    def _configured_guild_allowed(self, guild: discord.Guild | None) -> bool:
        if not guild:
            return False
        configured = set(getattr(bot_config, "SERVER_ADMIN_GUILD_IDS", set()) or set())
        return not configured or guild.id in configured

    def _is_exempt(self, member: discord.Member | discord.User) -> bool:
        if getattr(member, "id", None) in set(getattr(bot_config, "ADMIN_IDS", []) or []):
            return True
        roles = list(getattr(member, "roles", []) or [])
        role_ids = {getattr(role, "id", 0) for role in roles}
        role_names = {sanitize_text(getattr(role, "name", "")).lower() for role in roles}
        admin_ids = set(getattr(bot_config, "ADMIN_ROLE_IDS", set()) or set())
        admin_names = set(getattr(bot_config, "ADMIN_ROLE_NAMES", set()) or set())
        mod_ids = set(getattr(bot_config, "MODERATOR_ROLE_IDS", set()) or set())
        mod_names = set(getattr(bot_config, "MODERATOR_ROLE_NAMES", set()) or set())
        return bool((role_ids & (admin_ids | mod_ids)) or (role_names & (admin_names | mod_names)))

    async def _alert(self, message: discord.Message, kind: str, detail: str, fields: dict[str, Any]) -> None:
        now = time.time()
        key = (message.author.id, kind)
        if now - self._last_alert.get(key, 0) < self.alert_cooldown:
            return
        self._last_alert[key] = now
        self._stats.alerts_total += 1
        line = f"{kind}: {message.author} in #{getattr(message.channel, 'name', 'unknown')} — {detail}"
        self._stats.recent_alerts.append(line)

        if hasattr(self.bot, "record_ai_event"):
            self.bot.record_ai_event(
                "warning",
                "ai_security",
                f"Security flag: {kind}",
                sanitize_data({
                    "user": f"{message.author} ({message.author.id})",
                    "guild": str(message.guild),
                    "channel": getattr(message.channel, "name", str(message.channel)),
                    "detail": detail,
                    **fields,
                }),
            )

        notifier = getattr(self.bot, "notify_admins", None)
        if notifier:
            try:
                await notifier(
                    "Security alert",
                    detail,
                    level="warning",
                    fields={
                        "Kind": kind,
                        "User": f"{message.author} ({message.author.id})",
                        "Guild": str(message.guild),
                        "Channel": f"#{getattr(message.channel, 'name', 'unknown')}",
                        **{str(k): sanitize_text(str(v))[:600] for k, v in fields.items()},
                    },
                    key=f"ai-security-{kind}-{message.author.id}",
                )
            except Exception:
                log.warning("Failed to notify admins for security alert", exc_info=True)

        if getattr(self.bot, "ai_caretaker", None):
            try:
                self.bot.queue_ai_caretaker(
                    "security-alert",
                    {
                        "kind": kind,
                        "detail": detail,
                        "user_id": str(message.author.id),
                        "channel": getattr(message.channel, "name", ""),
                    },
                )
            except Exception:
                pass

    async def _maybe_auto_action(self, message: discord.Message, kind: str) -> None:
        if self.alert_only:
            return
        if not (self.auto_delete_spam or self.auto_timeout_spam):
            return
        me = getattr(message.guild, "me", None) if message.guild else None
        if self.auto_delete_spam and getattr(message.channel, "permissions_for", None) and me:
            try:
                perms = message.channel.permissions_for(me)
                if perms.manage_messages:
                    await message.delete()
                    self._stats.auto_actions += 1
            except Exception:
                log.debug("Auto-delete failed", exc_info=True)
        if self.auto_timeout_spam and isinstance(message.author, discord.Member):
            try:
                me_perms = getattr(message.guild.me, "guild_permissions", None) if message.guild and message.guild.me else None
                if me_perms and me_perms.moderate_members:
                    until = discord.utils.utcnow() + timedelta(seconds=self.timeout_seconds)
                    await message.author.timeout(until, reason=f"TriadBot AI Security: {kind}")
                    self._stats.auto_actions += 1
            except Exception:
                log.debug("Auto-timeout failed", exc_info=True)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if not self.enabled or message.author.bot:
            return
        if isinstance(message.channel, discord.DMChannel):
            return
        if not self._configured_guild_allowed(message.guild):
            return
        if self._is_exempt(message.author):
            return

        now = time.time()
        content = sanitize_text(message.content or "")
        mention_count = len(getattr(message, "mentions", []) or []) + len(getattr(message, "role_mentions", []) or [])
        link_count = len(_URL_RE.findall(content))
        bucket = self._events[(message.guild.id, message.author.id)]
        bucket.append({"t": now, "links": link_count, "mentions": mention_count})
        recent = [item for item in bucket if now - float(item.get("t", 0)) <= self.window_seconds]
        recent_links = sum(int(item.get("links", 0)) for item in recent)

        if len(recent) >= self.max_messages:
            self._stats.spam_flags += 1
            detail = f"User mengirim {len(recent)} pesan dalam {self.window_seconds}s."
            await self._alert(message, "message-spam", detail, {"messages_in_window": len(recent)})
            await self._maybe_auto_action(message, "message-spam")
            return

        if mention_count >= self.max_mentions:
            self._stats.mention_flags += 1
            detail = f"User melakukan mention flood: {mention_count} mention dalam satu pesan."
            await self._alert(message, "mention-flood", detail, {"mentions": mention_count})
            await self._maybe_auto_action(message, "mention-flood")
            return

        if recent_links >= self.max_links:
            self._stats.link_flags += 1
            detail = f"User mengirim {recent_links} link dalam {self.window_seconds}s."
            await self._alert(message, "link-flood", detail, {"links_in_window": recent_links})
            await self._maybe_auto_action(message, "link-flood")
            return


async def setup(bot):
    await bot.add_cog(AISecurity(bot))
