"""Discord cog for automatic SQLite backup to GitHub."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path

import discord
from discord.ext import commands, tasks

from config import (
    GITHUB_BACKUP_ENABLED,
    GITHUB_TOKEN,
    GITHUB_REPO,
    GITHUB_BRANCH,
    GITHUB_DB_PATH,
    GITHUB_DB_METADATA_PATH,
    GITHUB_BACKUP_INTERVAL_HOURS,
    GITHUB_BACKUP_START_DELAY_SECONDS,
    GITHUB_BACKUP_NOTIFY_ON_SUCCESS,
    GITHUB_BACKUP_TIMEOUT_SECONDS,
    GITHUB_BACKUP_CHUNK_SIZE_MB,
    ADMIN_IDS,
    SQLITE_PATH,
)
from utils.github_db_backup import GitHubDatabaseBackup

log = logging.getLogger(__name__)


class GitHubDBBackupCog(commands.Cog):
    """Periodically backs up /data/games.db to GitHub."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._lock = asyncio.Lock()
        self._last_result = None
        self._start_time = datetime.now(timezone.utc)

        self.backup_client = GitHubDatabaseBackup(
            token=GITHUB_TOKEN,
            repo=GITHUB_REPO,
            branch=GITHUB_BRANCH,
            db_path=Path(SQLITE_PATH),
            github_db_path=GITHUB_DB_PATH,
            metadata_path=GITHUB_DB_METADATA_PATH,
            session=getattr(bot, "session", None),
            timeout_seconds=GITHUB_BACKUP_TIMEOUT_SECONDS,
            chunk_size_mb=GITHUB_BACKUP_CHUNK_SIZE_MB,
        )

        hours = max(float(GITHUB_BACKUP_INTERVAL_HOURS), 0.25)
        self.backup_loop.change_interval(hours=hours)

    async def cog_load(self) -> None:
        if not GITHUB_BACKUP_ENABLED:
            log.info("GitHub DB backup cog loaded but disabled")
            return
        if not self.backup_client.enabled:
            log.warning("GitHub DB backup enabled but missing GITHUB_TOKEN/GITHUB_REPO/GITHUB_DB_PATH")
            return
        self.backup_loop.start()
        log.info(
            "GitHub DB backup enabled: %s -> %s/%s (%s)",
            SQLITE_PATH,
            GITHUB_REPO,
            GITHUB_DB_PATH,
            GITHUB_BRANCH,
        )

    async def cog_unload(self) -> None:
        self.backup_loop.cancel()

    @tasks.loop(hours=12)
    async def backup_loop(self) -> None:
        await self.run_backup(force=False, reason="scheduled")

    @backup_loop.before_loop
    async def before_backup_loop(self) -> None:
        await self.bot.wait_until_ready()
        delay = max(float(GITHUB_BACKUP_START_DELAY_SECONDS), 0.0)
        if delay:
            await asyncio.sleep(delay)

    async def run_backup(self, *, force: bool = False, reason: str = "manual"):
        async with self._lock:
            result = await self.backup_client.backup(force=force, reason=reason)
            self._last_result = result

        if result.ok:
            if result.uploaded:
                log.info(
                    "GitHub DB backup uploaded: %s bytes sha256=%s",
                    result.size_bytes,
                    result.sha256,
                )
                if GITHUB_BACKUP_NOTIFY_ON_SUCCESS:
                    await self._notify_admins(
                        "✅ GitHub DB backup uploaded",
                        f"Repo: `{GITHUB_REPO}`\nPath: `{GITHUB_DB_PATH}.gz.partXXX`\nChunks: `{result.chunk_count}`\nRaw size: `{result.size_bytes:,}` bytes\nCompressed: `{result.compressed_size_bytes:,}` bytes\nSHA256: `{result.sha256[:16]}...`",
                    )
            else:
                log.info("GitHub DB backup skipped: %s", result.message)
        else:
            log.error("GitHub DB backup failed: %s - %s", result.status, result.message)
            await self._notify_admins(
                "⚠️ GitHub DB backup failed",
                f"Status: `{result.status}`\nMessage: `{result.message[:1500]}`",
            )
        return result

    async def _notify_admins(self, title: str, description: str) -> None:
        notifier = getattr(self.bot, "notify_admins", None)
        if callable(notifier):
            try:
                await notifier(title, description)
                return
            except Exception:
                log.exception("Failed to send GitHub backup alert through bot notifier")

        for user_id in ADMIN_IDS[:3]:
            try:
                user = await self.bot.fetch_user(int(user_id))
                embed = discord.Embed(title=title, description=description, color=0x3498DB)
                await user.send(embed=embed)
            except Exception:
                pass

    @commands.hybrid_group(name="dbbackup", description="Admin tools for GitHub SQLite backup")
    @commands.is_owner()
    async def dbbackup(self, ctx: commands.Context) -> None:
        if ctx.invoked_subcommand is None:
            await ctx.reply("Pakai `/dbbackup status` atau `/dbbackup run`.", mention_author=False)

    @dbbackup.command(name="status", description="Show GitHub SQLite backup status")
    @commands.is_owner()
    async def dbbackup_status(self, ctx: commands.Context) -> None:
        result = self._last_result
        status_text = "Belum pernah jalan" if not result else f"{result.status}: {result.message}"
        embed = discord.Embed(title="GitHub DB Backup Status", color=0x3498DB)
        embed.add_field(name="Enabled", value=str(bool(GITHUB_BACKUP_ENABLED)), inline=True)
        embed.add_field(name="Repo", value=f"`{GITHUB_REPO or '-'}`", inline=True)
        embed.add_field(name="Branch", value=f"`{GITHUB_BRANCH}`", inline=True)
        embed.add_field(name="SQLite", value=f"`{SQLITE_PATH}`", inline=False)
        embed.add_field(name="GitHub path", value=f"`{GITHUB_DB_PATH}`", inline=False)
        embed.add_field(name="Last result", value=status_text[:1024], inline=False)
        await ctx.reply(embed=embed, mention_author=False)

    @dbbackup.command(name="run", description="Force backup SQLite database to GitHub now")
    @commands.is_owner()
    async def dbbackup_run(self, ctx: commands.Context) -> None:
        await ctx.defer(ephemeral=True if getattr(ctx, "interaction", None) else False)
        result = await self.run_backup(force=True, reason="manual")
        if result.ok:
            await ctx.reply(
                f"✅ `{result.status}` — {result.message}\nChunks: `{result.chunk_count}`\nRaw size: `{result.size_bytes:,}` bytes\nCompressed: `{result.compressed_size_bytes:,}` bytes\nSHA256: `{result.sha256[:16]}...`",
                mention_author=False,
            )
        else:
            await ctx.reply(f"❌ `{result.status}` — {result.message[:1500]}", mention_author=False)


async def setup(bot: commands.Bot):
    await bot.add_cog(GitHubDBBackupCog(bot))
