"""
Admin command and background task for syncing games.json from Steam App List.
"""
from __future__ import annotations

import asyncio
import logging

import discord
from discord import app_commands
from discord.ext import commands

import config as bot_config
from config import (
    ADMIN_IDS,
    ADMIN_ROLE_IDS,
    ADMIN_ROLE_NAMES,
    COLOR_ERROR,
    COLOR_INFO,
    COLOR_SUCCESS,
    COLOR_WARNING,
)
from utils.helpers import is_admin_interaction
from utils.steam_db_sync import SteamDbSyncSummary, sync_steam_database

log = logging.getLogger(__name__)


def admin_check():
    async def predicate(interaction: discord.Interaction) -> bool:
        if is_admin_interaction(interaction, ADMIN_IDS, ADMIN_ROLE_IDS, ADMIN_ROLE_NAMES):
            return True
        await interaction.response.send_message(
            embed=discord.Embed(
                title="Access Denied",
                description="This command is restricted to bot admins.",
                color=COLOR_ERROR,
            ),
            ephemeral=True,
        )
        return False

    return app_commands.check(predicate)


def _summary_embed(summary: SteamDbSyncSummary, title: str = "Steam DB Sync") -> discord.Embed:
    if summary.errors:
        color = COLOR_ERROR
    elif summary.dry_run and summary.has_changes:
        color = COLOR_WARNING
    elif summary.has_changes:
        color = COLOR_SUCCESS
    else:
        color = COLOR_INFO

    embed = discord.Embed(
        title=title,
        description=(
            "Dry-run only. games.json was not changed."
            if summary.dry_run
            else "Steam names were applied to games.json."
        ),
        color=color,
        timestamp=discord.utils.utcnow(),
    )
    for name, value in summary.to_fields().items():
        embed.add_field(name=name, value=f"`{value}`", inline=True)
    if summary.samples:
        embed.add_field(
            name="Samples",
            value="\n".join(f"- {item}" for item in summary.samples)[:1024],
            inline=False,
        )
    if summary.errors:
        embed.add_field(
            name="Errors",
            value="\n".join(f"- {item}" for item in summary.errors)[:1024],
            inline=False,
        )
    return embed


class SteamDbSyncCommands(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self._lock = asyncio.Lock()
        self._task: asyncio.Task | None = None
        bot.steam_db_sync_lock = self._lock

    async def cog_load(self):
        if bot_config.STEAM_DB_SYNC_ENABLED:
            self._task = asyncio.create_task(self._sync_loop())
            log.info("Steam DB sync background task enabled")

    async def cog_unload(self):
        if self._task:
            self._task.cancel()

    async def _run_threaded(
        self,
        *,
        apply_changes: bool,
        include_new: bool,
        max_new: int,
        max_updates: int,
    ) -> SteamDbSyncSummary:
        async with self._lock:
            return await asyncio.to_thread(
                sync_steam_database,
                self.bot.db,
                apply=apply_changes,
                include_new=include_new,
                max_new=max_new,
                max_updates=max_updates,
            )

    async def _sync_loop(self):
        await self.bot.wait_until_ready()
        interval_seconds = max(1.0, bot_config.STEAM_DB_SYNC_INTERVAL_HOURS) * 3600
        first_run = True

        while not self.bot.is_closed():
            if first_run:
                first_run = False
                if not bot_config.STEAM_DB_SYNC_RUN_ON_START:
                    await asyncio.sleep(interval_seconds)
                elif bot_config.STEAM_DB_SYNC_START_DELAY_SECONDS > 0:
                    await asyncio.sleep(bot_config.STEAM_DB_SYNC_START_DELAY_SECONDS)

            try:
                summary = await self._run_threaded(
                    apply_changes=bot_config.STEAM_DB_SYNC_APPLY,
                    include_new=bot_config.STEAM_DB_SYNC_INCLUDE_NEW,
                    max_new=max(0, bot_config.STEAM_DB_SYNC_MAX_NEW),
                    max_updates=max(0, bot_config.STEAM_DB_SYNC_MAX_UPDATES),
                )
                await self._alert_if_needed(summary, automatic=True)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.exception("Steam DB sync failed")
                notifier = getattr(self.bot, "notify_admins", None)
                if notifier:
                    await notifier(
                        "Steam DB sync crashed",
                        "The automatic Steam database sync failed unexpectedly.",
                        level="error",
                        fields={"Error": repr(exc)[:1000]},
                        key="steam-db-sync-crashed",
                    )

            await asyncio.sleep(interval_seconds)

    async def _alert_if_needed(self, summary: SteamDbSyncSummary, *, automatic: bool) -> None:
        notifier = getattr(self.bot, "notify_admins", None)
        if not notifier:
            return
        if not summary.errors and not (automatic and not summary.dry_run and summary.has_changes):
            return

        fields = summary.to_fields()
        if summary.samples:
            fields["Samples"] = "\n".join(summary.samples)
        if summary.errors:
            fields["Errors"] = "\n".join(summary.errors)

        await notifier(
            "Steam DB sync needs attention" if summary.errors else "Steam DB sync applied changes",
            "Steam database sync finished with the summary below.",
            level="error" if summary.errors else "info",
            fields=fields,
            key="steam-db-sync-errors" if summary.errors else "steam-db-sync-applied",
        )

    @app_commands.command(name="steam_db_sync", description="[Admin] Fill games.json names from Steam App List")
    @app_commands.describe(
        apply_changes="True writes to games.json. False only previews.",
        include_new="Add Steam AppIDs that are not yet in games.json",
        max_new="Maximum new entries to add. 0 means unlimited.",
        max_updates="Maximum placeholder names to update. 0 means unlimited.",
    )
    @admin_check()
    async def steam_db_sync(
        self,
        interaction: discord.Interaction,
        apply_changes: bool = False,
        include_new: bool = True,
        max_new: int = 0,
        max_updates: int = 0,
    ):
        await interaction.response.defer(ephemeral=True, thinking=True)

        if self._lock.locked():
            await interaction.followup.send(
                embed=discord.Embed(
                    title="Steam DB sync already running",
                    description="Wait for the current sync to finish, then try again.",
                    color=COLOR_WARNING,
                ),
                ephemeral=True,
            )
            return

        summary = await self._run_threaded(
            apply_changes=apply_changes,
            include_new=include_new,
            max_new=max(0, min(int(max_new or 0), 100000)),
            max_updates=max(0, min(int(max_updates or 0), 100000)),
        )
        await self._alert_if_needed(summary, automatic=False)
        await interaction.followup.send(embed=_summary_embed(summary), ephemeral=True)


async def setup(bot):
    await bot.add_cog(SteamDbSyncCommands(bot))
