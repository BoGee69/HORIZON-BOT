"""
Automatic Discord server administration audit.

This cog is read-only by default. Server-changing actions are handled through
owner-approved AI operator proposals.
"""
from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

import discord
from discord.ext import commands

import config as bot_config
from config import COLOR_ERROR, COLOR_INFO, COLOR_WARNING
from utils.server_admin import ServerAdminSummary, audit_servers

log = logging.getLogger(__name__)


class ServerAdmin(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self._task: asyncio.Task | None = None

    async def cog_load(self):
        if bot_config.SERVER_ADMIN_ENABLED:
            self._task = asyncio.create_task(self._audit_loop())
            log.info("Server admin audit task enabled")

    async def cog_unload(self):
        if self._task:
            self._task.cancel()

    async def _audit_loop(self):
        await self.bot.wait_until_ready()
        if bot_config.SERVER_ADMIN_START_DELAY_SECONDS > 0:
            await asyncio.sleep(bot_config.SERVER_ADMIN_START_DELAY_SECONDS)

        if bot_config.SERVER_ADMIN_AUDIT_ON_START:
            await self.run_audit(automatic=True)

        interval_seconds = max(1.0, bot_config.SERVER_ADMIN_AUDIT_INTERVAL_HOURS) * 3600
        while not self.bot.is_closed():
            await asyncio.sleep(interval_seconds)
            await self.run_audit(automatic=True)

    async def run_audit(self, *, automatic: bool = False) -> ServerAdminSummary:
        summary = await audit_servers(self.bot)
        self.bot.last_server_admin_summary = summary
        if hasattr(self.bot, "record_ai_event"):
            self.bot.record_ai_event(
                "warning" if summary.has_issues else "info",
                "server_admin",
                "Discord server audit finished.",
                {"automatic": automatic, "fields": summary.to_fields()},
            )
        if hasattr(self.bot, "queue_ai_caretaker") and summary.has_issues:
            self.bot.queue_ai_caretaker(
                "server-admin-audit-issues",
                {"automatic": automatic, "fields": summary.to_fields()},
                force=False,
            )
        if automatic and summary.has_issues and bot_config.SERVER_ADMIN_ALERT_ON_ISSUES:
            await self._alert(summary)
        return summary

    def _allowed_guilds(self) -> list[discord.Guild]:
        guilds = list(getattr(self.bot, "guilds", []) or [])
        if bot_config.SERVER_ADMIN_GUILD_IDS:
            guilds = [guild for guild in guilds if guild.id in bot_config.SERVER_ADMIN_GUILD_IDS]
        return guilds

    def _resolve_guild(self, params: dict[str, Any]) -> discord.Guild:
        guild_id = str(params.get("guild_id") or "").strip()
        guilds = self._allowed_guilds()
        if guild_id.isdigit():
            guild = discord.utils.get(guilds, id=int(guild_id))
            if guild:
                return guild
            raise ValueError(f"Configured guild `{guild_id}` is not available to the bot.")
        if len(guilds) == 1:
            return guilds[0]
        if not guilds:
            raise ValueError("No guild is available for server administration.")
        raise ValueError("Multiple guilds are available. Include `guild_id` in the proposal parameters.")

    @staticmethod
    def _normalize_channel_name(value: str) -> str:
        value = re.sub(r"<#(\d+)>", r"\1", str(value or "").strip())
        value = value.strip("#").strip()
        value = value.replace(" ", "-").lower()
        value = re.sub(r"[^a-z0-9_-]+", "-", value)
        value = re.sub(r"-{2,}", "-", value).strip("-")
        return value[:90]

    def _resolve_text_channel(
        self,
        guild: discord.Guild,
        params: dict[str, Any],
        *,
        fallback_names: set[str] | None = None,
    ) -> discord.TextChannel:
        target = str(params.get("channel_id") or params.get("channel") or params.get("channel_name") or "").strip()
        channel = None
        mention_match = re.fullmatch(r"<#(\d+)>", target)
        if mention_match:
            target = mention_match.group(1)
        if target.isdigit():
            channel = guild.get_channel(int(target))
        if not channel and target:
            normalized = self._normalize_channel_name(target)
            channel = discord.utils.find(
                lambda item: self._normalize_channel_name(item.name) == normalized,
                guild.text_channels,
            )
        if not channel and fallback_names:
            lowered = {self._normalize_channel_name(item) for item in fallback_names}
            channel = discord.utils.find(
                lambda item: self._normalize_channel_name(item.name) in lowered,
                guild.text_channels,
            )
        if not isinstance(channel, discord.TextChannel):
            raise ValueError("Target text channel was not found.")
        return channel

    @staticmethod
    def _require_text(params: dict[str, Any], key: str, *, max_chars: int) -> str:
        value = str(params.get(key) or "").strip()
        if not value:
            raise ValueError(f"`{key}` is required.")
        if len(value) > max_chars:
            raise ValueError(f"`{key}` is too long. Maximum is {max_chars} characters.")
        return value

    @staticmethod
    def _short_text(value: Any, limit: int = 140) -> str:
        text = str(value or "").replace("\n", " ").strip()
        if len(text) > limit:
            return text[: limit - 3] + "..."
        return text

    def _server_embed(self, *, title: str, description: str, color: int = COLOR_INFO) -> discord.Embed:
        embed = discord.Embed(
            title=title[:256],
            description=description[:4096],
            color=color,
            timestamp=discord.utils.utcnow(),
        )
        embed.set_footer(text="TriadBot")
        return embed

    async def send_announcement(self, params: dict[str, Any]) -> str:
        guild = self._resolve_guild(params)
        channel = self._resolve_text_channel(
            guild,
            params,
            fallback_names=bot_config.SERVER_ADMIN_ANNOUNCEMENT_CHANNEL_NAMES,
        )
        title = str(params.get("title") or "Announcement").strip()[:256]
        content = self._require_text(params, "content", max_chars=3800)
        embed = self._server_embed(title=title, description=content, color=COLOR_INFO)
        image_url = str(params.get("image_url") or "").strip()
        if image_url.startswith(("http://", "https://")):
            embed.set_image(url=image_url)
        message = await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
        return f"Announcement sent to #{channel.name} ({channel.id}). Message ID: {message.id}"

    async def update_rules(self, params: dict[str, Any]) -> str:
        guild = self._resolve_guild(params)
        channel = self._resolve_text_channel(
            guild,
            params,
            fallback_names=bot_config.SERVER_ADMIN_RULES_CHANNEL_NAMES,
        )
        title = str(params.get("title") or "Server Rules").strip()[:256]
        content = self._require_text(params, "content", max_chars=3800)
        embed = self._server_embed(title=title, description=content, color=COLOR_INFO)

        edited = False
        async for message in channel.history(limit=40):
            if message.author.id != self.bot.user.id:
                continue
            if message.embeds and (message.embeds[0].title or "").lower() == title.lower():
                await message.edit(embed=embed, allowed_mentions=discord.AllowedMentions.none())
                edited = True
                target_message = message
                break
        else:
            target_message = await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())

        if bool(params.get("pin")):
            try:
                await target_message.pin(reason="Owner-approved rules update")
            except discord.HTTPException:
                log.warning("Could not pin rules message in %s", channel, exc_info=True)

        action = "updated" if edited else "posted"
        return f"Rules {action} in #{channel.name} ({channel.id}). Message ID: {target_message.id}"

    async def pin_message(self, params: dict[str, Any]) -> str:
        guild = self._resolve_guild(params)
        channel = self._resolve_text_channel(guild, params)
        message_id = str(params.get("message_id") or "").strip()
        if message_id.isdigit():
            message = await channel.fetch_message(int(message_id))
        else:
            messages = [item async for item in channel.history(limit=1)]
            if not messages:
                raise ValueError(f"No recent message found in #{channel.name} to pin.")
            message = messages[0]
        await message.pin(reason="Owner-approved server administration action")
        return f"Message {message.id} pinned in #{channel.name} ({channel.id})."

    async def set_channel_topic(self, params: dict[str, Any]) -> str:
        guild = self._resolve_guild(params)
        channel = self._resolve_text_channel(guild, params)
        topic = self._require_text(params, "topic", max_chars=1024)
        await channel.edit(topic=topic, reason="Owner-approved channel topic update")
        return f"Topic updated for #{channel.name} ({channel.id}): {self._short_text(topic)}"

    async def create_channel(self, params: dict[str, Any]) -> str:
        guild = self._resolve_guild(params)
        raw_name = self._require_text(params, "name", max_chars=100)
        name = self._normalize_channel_name(raw_name)
        if not name:
            raise ValueError("Channel name is invalid after normalization.")
        if discord.utils.find(lambda item: item.name.lower() == name, guild.text_channels):
            raise ValueError(f"Text channel #{name} already exists.")

        category = None
        category_name = str(params.get("category") or "").strip().lower()
        if category_name:
            normalized_category = self._normalize_channel_name(category_name)
            category = discord.utils.find(
                lambda item: item.name.lower().replace(" ", "-") == normalized_category,
                guild.categories,
            )
            if not category:
                raise ValueError(f"Category `{category_name}` was not found.")

        topic = str(params.get("topic") or "").strip()[:1024] or None
        channel = await guild.create_text_channel(
            name=name,
            topic=topic,
            category=category,
            reason="Owner-approved text channel creation",
        )
        return f"Created text channel #{channel.name} ({channel.id})."

    async def _alert(self, summary: ServerAdminSummary) -> None:
        notifier = getattr(self.bot, "notify_admins", None)
        if not notifier:
            return
        fields = summary.to_fields()
        issue_lines = []
        for audit in summary.audits:
            if not audit.has_issues:
                continue
            issue_lines.append(f"{audit.guild_name} ({audit.guild_id})")
            for item in (audit.missing_permissions + audit.role_issues + audit.channel_issues)[:4]:
                issue_lines.append(f"- {item}")
            if audit.boosters_missing_role:
                issue_lines.append(f"- {audit.boosters_missing_role} booster(s) missing Booster role")
            if audit.non_boosters_with_role:
                issue_lines.append(f"- {audit.non_boosters_with_role} non-booster(s) still have Booster role")
            if len("\n".join(issue_lines)) > 900:
                break
        if issue_lines:
            fields["Samples"] = "\n".join(issue_lines)[:1024]
        await notifier(
            "Discord server audit needs attention",
            "Server caretaker found permission, role, channel, or Booster role issues.",
            level="warning",
            fields=fields,
            key="server-admin-audit-issues",
        )

    def summary_embed(self, summary: ServerAdminSummary) -> discord.Embed:
        embed = discord.Embed(
            title="Discord Server Audit",
            description="Read-only audit finished.",
            color=COLOR_WARNING if summary.has_issues else COLOR_INFO,
            timestamp=discord.utils.utcnow(),
        )
        for name, value in summary.to_fields().items():
            embed.add_field(name=name, value=f"`{value}`", inline=True)
        samples = []
        for audit in summary.audits:
            if not audit.has_issues:
                continue
            samples.append(f"{audit.guild_name}: issues detected")
            samples.extend(f"- {item}" for item in (audit.missing_permissions + audit.role_issues)[:3])
            if audit.boosters_missing_role:
                samples.append(f"- {audit.boosters_missing_role} booster(s) missing Booster role")
            if audit.non_boosters_with_role:
                samples.append(f"- {audit.non_boosters_with_role} non-booster(s) still have Booster role")
        if samples:
            embed.add_field(name="Samples", value="\n".join(samples)[:1024], inline=False)
        return embed


async def setup(bot):
    await bot.add_cog(ServerAdmin(bot))
