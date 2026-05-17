"""
Admin commands and optional background task for R2 database maintenance.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

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
from utils.r2_inventory import invalidate_r2_inventory_cache
from utils.r2_maintenance import R2MaintenanceSummary, run_r2_maintenance

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


def _summary_embed(summary: R2MaintenanceSummary, title: str = "R2 Maintenance") -> discord.Embed:
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
            "Dry-run only. No R2 object was changed."
            if summary.dry_run
            else "Changes were applied to R2."
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
    if summary.applied_samples:
        embed.add_field(
            name="Applied samples",
            value="\n".join(f"- {item}" for item in summary.applied_samples)[:1024],
            inline=False,
        )
    if summary.errors:
        embed.add_field(
            name="Errors",
            value="\n".join(f"- {item}" for item in summary.errors)[:1024],
            inline=False,
        )
    return embed


class R2MaintenanceCommands(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self._lock = asyncio.Lock()
        self._task: asyncio.Task | None = None

    async def cog_load(self):
        if bot_config.R2_MAINTENANCE_ENABLED:
            self._task = asyncio.create_task(self._maintenance_loop())
            log.info("R2 maintenance background task enabled")

    async def cog_unload(self):
        if self._task:
            self._task.cancel()

    async def _run_threaded(
        self,
        *,
        apply_changes: bool,
        prefix: str,
        limit: int,
        rename_objects: bool,
        clean_lua: bool,
        use_steam: bool,
        max_steam_lookups: int,
        use_queue: bool,
        fallback_to_appid: bool,
        ignore_blacklist: bool,
    ) -> R2MaintenanceSummary:
        async with self._lock:
            return await asyncio.to_thread(
                run_r2_maintenance,
                apply=apply_changes,
                prefix=prefix,
                limit=limit,
                games=getattr(self.bot.db, "game_db", []),
                rename_objects=rename_objects,
                clean_lua_comments=clean_lua,
                use_steam=use_steam,
                max_steam_lookups=max_steam_lookups,
                use_queue=use_queue,
                fallback_to_appid=fallback_to_appid,
                ignore_blacklist=ignore_blacklist,
            )

    async def _maintenance_loop(self):
        await self.bot.wait_until_ready()
        interval_seconds = max(1.0, bot_config.R2_MAINTENANCE_INTERVAL_HOURS) * 3600

        if not bot_config.R2_MAINTENANCE_RUN_ON_START:
            await asyncio.sleep(interval_seconds)

        while not self.bot.is_closed():
            if bot_config.R2_MAINTENANCE_RUN_ON_START and bot_config.R2_MAINTENANCE_START_DELAY_SECONDS > 0:
                await asyncio.sleep(bot_config.R2_MAINTENANCE_START_DELAY_SECONDS)

            try:
                summary = await self._run_threaded(
                    apply_changes=bot_config.R2_MAINTENANCE_APPLY,
                    prefix=bot_config.R2_MAINTENANCE_PREFIX,
                    limit=max(0, bot_config.R2_MAINTENANCE_MAX_OBJECTS),
                    rename_objects=bot_config.R2_MAINTENANCE_RENAME_OBJECTS,
                    clean_lua=bot_config.R2_MAINTENANCE_CLEAN_LUA_COMMENTS,
                    use_steam=bot_config.R2_MAINTENANCE_STEAM_LOOKUPS,
                    max_steam_lookups=bot_config.R2_MAINTENANCE_MAX_STEAM_LOOKUPS,
                    use_queue=bot_config.R2_MAINTENANCE_QUEUE_ENABLED,
                    fallback_to_appid=bot_config.R2_MAINTENANCE_FALLBACK_TO_APPID,
                    ignore_blacklist=False,
                )
                await self._alert_if_needed(summary, automatic=True)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.exception("Automatic R2 maintenance failed")
                notifier = getattr(self.bot, "notify_admins", None)
                if notifier:
                    await notifier(
                        "R2 maintenance crashed",
                        "The automatic R2 maintenance task failed unexpectedly.",
                        level="error",
                        fields={"Error": repr(exc)[:1000]},
                        key="r2-maintenance-crashed",
                    )
                if hasattr(self.bot, "queue_ai_caretaker"):
                    self.bot.queue_ai_caretaker(
                        "r2-maintenance-crashed",
                        {"error": repr(exc)[:1000]},
                        force=True,
                    )

            await asyncio.sleep(interval_seconds)

    async def _alert_if_needed(self, summary: R2MaintenanceSummary, *, automatic: bool) -> None:
        self.bot.last_r2_maintenance_summary = summary
        if summary.has_changes or summary.processed:
            invalidate_r2_inventory_cache()
        if hasattr(self.bot, "record_ai_event"):
            self.bot.record_ai_event(
                "error" if summary.errors else "info",
                "r2_maintenance",
                "R2 maintenance finished.",
                {
                    "automatic": automatic,
                    "fields": summary.to_fields(),
                    "errors": summary.errors,
                    "applied_samples": summary.applied_samples,
                },
            )
        if hasattr(self.bot, "queue_ai_caretaker"):
            self.bot.queue_ai_caretaker(
                "r2-maintenance-finished",
                {"automatic": automatic, "errors": len(summary.errors), "has_changes": summary.has_changes},
                force=bool(summary.errors),
            )

        notifier = getattr(self.bot, "notify_admins", None)
        if not notifier:
            return

        if not summary.errors and not (automatic and not summary.dry_run and summary.has_changes):
            return

        fields = summary.to_fields()
        if summary.applied_samples:
            fields["Applied samples"] = "\n".join(summary.applied_samples)
        if summary.samples:
            fields["Samples"] = "\n".join(summary.samples)
        if summary.errors:
            fields["Errors"] = "\n".join(summary.errors)

        await notifier(
            "R2 maintenance needs attention" if summary.errors else "R2 maintenance applied changes",
            "The R2 maintenance task finished with the summary below.",
            level="error" if summary.errors else "info",
            fields=fields,
            key="r2-maintenance-errors" if summary.errors else "r2-maintenance-applied",
        )

    @app_commands.command(name="r2_maintenance", description="[Admin] Normalize R2 ZIP names and clean Lua/manifest comments")
    @app_commands.describe(
        apply_changes="True writes changes to R2. False only previews.",
        limit="Maximum ZIP objects to scan this run. Use a small number first.",
        prefix="R2 prefix to scan, for example Database/",
        rename_objects="Rename objects to Game Name (appid).zip",
        clean_lua="Remove Lua/manifest comments inside ZIP files",
        use_steam="Fetch missing names from Steam when games.json/cache has no name",
        max_steam_lookups="Maximum Steam name lookups for this run",
        use_queue="Continue from the previous R2 scan position",
        fallback_to_appid="If Steam has no name, normalize to AppID.zip instead of skipping",
        ignore_blacklist="Retry AppIDs that were blacklisted after repeated Steam failures",
    )
    @admin_check()
    async def r2_maintenance(
        self,
        interaction: discord.Interaction,
        apply_changes: bool = False,
        limit: int = 25,
        prefix: Optional[str] = None,
        rename_objects: bool = True,
        clean_lua: bool = True,
        use_steam: bool = False,
        max_steam_lookups: int = 25,
        use_queue: bool = True,
        fallback_to_appid: bool = True,
        ignore_blacklist: bool = False,
    ):
        await interaction.response.defer(ephemeral=True, thinking=True)

        if self._lock.locked():
            await interaction.followup.send(
                embed=discord.Embed(
                    title="R2 maintenance already running",
                    description="Wait for the current maintenance run to finish, then try again.",
                    color=COLOR_WARNING,
                ),
                ephemeral=True,
            )
            return

        safe_limit = max(1, min(int(limit or 25), 500))
        safe_max_steam = max(0, min(int(max_steam_lookups or 0), 500))
        scan_prefix = bot_config.R2_MAINTENANCE_PREFIX if prefix is None else prefix.strip()

        summary = await self._run_threaded(
            apply_changes=apply_changes,
            prefix=scan_prefix,
            limit=safe_limit,
            rename_objects=rename_objects,
            clean_lua=clean_lua,
            use_steam=use_steam,
            max_steam_lookups=safe_max_steam,
            use_queue=use_queue,
            fallback_to_appid=fallback_to_appid,
            ignore_blacklist=ignore_blacklist,
        )
        await self._alert_if_needed(summary, automatic=False)
        await interaction.followup.send(embed=_summary_embed(summary), ephemeral=True)


async def setup(bot):
    await bot.add_cog(R2MaintenanceCommands(bot))
