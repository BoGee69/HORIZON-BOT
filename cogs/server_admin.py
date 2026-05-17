"""
Automatic Discord server administration audit.

This cog is read-only by default. Server-changing actions are handled through
owner-approved AI operator proposals.
"""
from __future__ import annotations

import asyncio
import logging

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
