"""
Admin Commands Cog
All commands here can only be run by Admin IDs defined in config.
All responses are ephemeral (only visible to the admin who ran the command).
"""
import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Optional

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

from config import (
    ADMIN_IDS, ADMIN_WEBHOOK, COLOR_DOWNLOAD, COLOR_ERROR, COLOR_INFO,
    COLOR_SUCCESS, COLOR_WARNING, R2_BASE_URL,
)
from utils.helpers import format_number, format_size

log = logging.getLogger(__name__)


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


def admin_check():
    """app_commands check — admin only."""
    async def predicate(interaction: discord.Interaction) -> bool:
        if not is_admin(interaction.user.id):
            await interaction.response.send_message(
                embed=discord.Embed(
                    title="🚫 Access Denied",
                    description="This command is restricted to bot admins.",
                    color=COLOR_ERROR,
                ),
                ephemeral=True,
            )
            return False
        return True
    return app_commands.check(predicate)


class AdminCommands(commands.Cog):
    """Admin-only commands"""

    def __init__(self, bot):
        self.bot = bot
        self.db = bot.db

    # ── /admin status ─────────────────────────────────────────────────────────

    @app_commands.command(name="status", description="[Admin] Bot & database status")
    @admin_check()
    async def status(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        uptime = discord.utils.utcnow() - self.bot.start_time
        hours, rem = divmod(int(uptime.total_seconds()), 3600)
        mins, secs = divmod(rem, 60)
        uptime_str = f"{hours}h {mins}m {secs}s"

        stats = self.db.get_stats()

        embed = discord.Embed(title="📊 Bot Status", color=COLOR_INFO,
                              timestamp=discord.utils.utcnow())
        embed.add_field(name="🤖 Bot",          value=str(self.bot.user),                                   inline=True)
        embed.add_field(name="⏱️ Uptime",       value=uptime_str,                                            inline=True)
        embed.add_field(name="📡 Ping",         value=f"{round(self.bot.latency * 1000)}ms",                 inline=True)
        embed.add_field(name="🌐 Guilds",       value=format_number(len(self.bot.guilds)),                   inline=True)
        embed.add_field(name="👥 Users",        value=format_number(sum(g.member_count or 0 for g in self.bot.guilds)), inline=True)
        embed.add_field(name="📦 Version",      value=self.bot.version,                                      inline=True)
        embed.add_field(name="🗄️ Total Games",  value=format_number(stats["total"]),                         inline=True)
        embed.add_field(name="⭐ With File",    value=format_number(stats["with_files"]),                    inline=True)
        embed.add_field(name="🏷️ With Name",   value=format_number(stats["with_names"]),                    inline=True)
        embed.add_field(name="🔝 Last AppID",   value=format_number(stats["last_appid"]),                    inline=True)
        embed.add_field(name="☁️ R2 URL",       value=R2_BASE_URL or "❌ Not configured",                    inline=False)
        embed.set_footer(text=f"Requested by {interaction.user}")
        await interaction.followup.send(embed=embed, ephemeral=True)

    # ── /admin check_r2 ───────────────────────────────────────────────────────

    @app_commands.command(name="check_r2", description="[Admin] Scan DB and mark games whose files exist in R2")
    @app_commands.describe(limit="Maximum number of games to check (default: 500)")
    @admin_check()
    async def check_r2(self, interaction: discord.Interaction, limit: int = 500):
        await interaction.response.defer(ephemeral=True)

        if not R2_BASE_URL:
            await interaction.followup.send(
                embed=discord.Embed(
                    title="❌ R2 Not Configured",
                    description="Set `R2_BASE_URL` in your `.env` file first.",
                    color=COLOR_ERROR,
                ),
                ephemeral=True,
            )
            return

        embed = discord.Embed(
            title="☁️ Scanning R2",
            description=f"Checking up to **{limit}** games against R2…",
            color=COLOR_WARNING,
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

        found = 0
        checked = 0
        to_check = [g for g in self.db.game_db if not g.get("file")][:limit]

        # Batch HEAD requests (max 20 concurrent)
        sem = asyncio.Semaphore(20)

        async def check_one(game):
            nonlocal found, checked
            appid = str(game["id"])
            url = f"{R2_BASE_URL}/Database/{appid}.zip"
            async with sem:
                try:
                    async with self.bot.session.head(
                        url, timeout=aiohttp.ClientTimeout(total=5), allow_redirects=True
                    ) as resp:
                        checked += 1
                        if resp.status == 200:
                            self.db.mark_as_starred(appid, game.get("name"))
                            found += 1
                except Exception:
                    checked += 1

        await asyncio.gather(*[check_one(g) for g in to_check])
        self.db.save()

        embed = discord.Embed(title="✅ R2 Scan Complete", color=COLOR_SUCCESS)
        embed.add_field(name="🔍 Checked",     value=format_number(checked), inline=True)
        embed.add_field(name="✅ Found",       value=format_number(found),   inline=True)
        embed.add_field(name="⭐ Total Files", value=format_number(self.db.get_stats()["with_files"]), inline=True)
        await interaction.edit_original_response(embed=embed)

    # ── /admin add_game ────────────────────────────────────────────────────────

    @app_commands.command(name="add_game", description="[Admin] Manually add a game to the database")
    @app_commands.describe(appid="Steam App ID", name="Game name (optional)", has_file="Mark as having a file immediately")
    @admin_check()
    async def add_game(self, interaction: discord.Interaction, appid: str,
                       name: Optional[str] = None, has_file: bool = False):
        await interaction.response.defer(ephemeral=True)

        if not appid.isdigit():
            await interaction.followup.send(
                embed=discord.Embed(
                    title="❌ Invalid App ID",
                    description=f"`{appid}` is not a valid number.",
                    color=COLOR_ERROR,
                ),
                ephemeral=True,
            )
            return

        existed = self.db.get_game(appid) is not None
        if existed:
            if has_file:
                self.db.mark_as_starred(appid, name)
                self.db.save()
                msg = f"Game `{appid}` already existed — status updated → ⭐ has file."
            else:
                msg = f"Game `{appid}` is already in the database."
        else:
            self.db.add_game(appid, name, has_file)
            self.db.save()
            msg = f"Game `{appid}` successfully added."

        embed = discord.Embed(
            title="✅ Database Updated" if not existed or has_file else "ℹ️ Already Exists",
            description=msg,
            color=COLOR_SUCCESS,
        )
        embed.add_field(name="App ID", value=appid, inline=True)
        if name:
            embed.add_field(name="Name", value=name, inline=True)
        embed.add_field(name="File?", value="✅ Yes" if has_file else "❌ No", inline=True)
        await interaction.followup.send(embed=embed, ephemeral=True)

    # ── /admin remove_game ────────────────────────────────────────────────────

    @app_commands.command(name="remove_game", description="[Admin] Remove a game from the database")
    @app_commands.describe(appid="Steam App ID to remove")
    @admin_check()
    async def remove_game(self, interaction: discord.Interaction, appid: str):
        await interaction.response.defer(ephemeral=True)

        game = self.db.get_game(appid)
        if not game:
            await interaction.followup.send(
                embed=discord.Embed(
                    title="❌ Not Found",
                    description=f"Game `{appid}` is not in the database.",
                    color=COLOR_ERROR,
                ),
                ephemeral=True,
            )
            return

        game_name = game.get("name", "Unknown")
        self.db.game_db = [g for g in self.db.game_db if str(g["id"]) != appid]
        self.db.game_index.pop(appid, None)
        self.db.save()

        embed = discord.Embed(
            title="🗑️ Game Removed",
            description=f"**{game_name}** (`{appid}`) has been removed from the database.",
            color=COLOR_WARNING,
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

    # ── /admin db_stats ───────────────────────────────────────────────────────

    @app_commands.command(name="db_stats", description="[Admin] Detailed database statistics")
    @admin_check()
    async def db_stats(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        stats = self.db.get_stats()
        pct_file = (stats["with_files"] / stats["total"] * 100) if stats["total"] else 0
        pct_name = (stats["with_names"] / stats["total"] * 100) if stats["total"] else 0

        embed = discord.Embed(title="🗄️ Database Statistics", color=COLOR_INFO,
                              timestamp=discord.utils.utcnow())
        embed.add_field(name="📊 Total Entries",   value=format_number(stats["total"]),      inline=True)
        embed.add_field(name="⭐ With File",       value=f"{format_number(stats['with_files'])} ({pct_file:.1f}%)", inline=True)
        embed.add_field(name="🏷️ With Name",      value=f"{format_number(stats['with_names'])} ({pct_name:.1f}%)", inline=True)
        embed.add_field(name="🔝 Highest AppID",   value=format_number(stats["last_appid"]),  inline=True)

        # Backup info
        from config import DATA_DIR
        backups = sorted(DATA_DIR.glob("games_backup_*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        backup_info = f"{len(backups)} backup(s) available"
        if backups:
            latest = datetime.fromtimestamp(backups[0].stat().st_mtime)
            backup_info += f"\nLatest: `{latest.strftime('%d %b %Y %H:%M')}`"
        embed.add_field(name="💾 Backups", value=backup_info, inline=False)
        await interaction.followup.send(embed=embed, ephemeral=True)

    # ── /admin backup ─────────────────────────────────────────────────────────

    @app_commands.command(name="backup", description="[Admin] Create a manual database backup")
    @admin_check()
    async def backup(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        ok = self.db._create_backup()
        if ok:
            embed = discord.Embed(
                title="💾 Backup Successful",
                description="Database has been backed up successfully.",
                color=COLOR_SUCCESS,
            )
        else:
            embed = discord.Embed(
                title="❌ Backup Failed",
                description="An error occurred while creating the backup.",
                color=COLOR_ERROR,
            )
        await interaction.followup.send(embed=embed, ephemeral=True)

    # ── /admin reload_cog ─────────────────────────────────────────────────────

    @app_commands.command(name="reload_cog", description="[Admin] Reload a cog without restarting the bot")
    @app_commands.describe(cog="Cog name (e.g. game_commands)")
    @admin_check()
    async def reload_cog(self, interaction: discord.Interaction, cog: str):
        await interaction.response.defer(ephemeral=True)

        cog_name = f"cogs.{cog}"
        try:
            await self.bot.reload_extension(cog_name)
            embed = discord.Embed(
                title="✅ Reload Successful",
                description=f"`{cog_name}` reloaded successfully.",
                color=COLOR_SUCCESS,
            )
        except commands.ExtensionNotLoaded:
            try:
                await self.bot.load_extension(cog_name)
                embed = discord.Embed(
                    title="✅ Load Successful",
                    description=f"`{cog_name}` loaded successfully.",
                    color=COLOR_SUCCESS,
                )
            except Exception as e:
                embed = discord.Embed(title="❌ Failed", description=str(e), color=COLOR_ERROR)
        except Exception as e:
            embed = discord.Embed(title="❌ Failed", description=str(e), color=COLOR_ERROR)

        await interaction.followup.send(embed=embed, ephemeral=True)

    # ── /admin announce ───────────────────────────────────────────────────────

    @app_commands.command(name="announce", description="[Admin] Send an announcement to this channel")
    @app_commands.describe(message="Announcement body", title="Title (optional)")
    @admin_check()
    async def announce(self, interaction: discord.Interaction, message: str,
                       title: Optional[str] = "📢 Announcement"):
        await interaction.response.defer(ephemeral=True)

        embed = discord.Embed(title=title, description=message, color=COLOR_INFO,
                              timestamp=discord.utils.utcnow())
        embed.set_footer(text=f"From admin • {interaction.guild.name if interaction.guild else 'DM'}")

        await interaction.channel.send(embed=embed)
        await interaction.followup.send(
            embed=discord.Embed(
                title="✅ Sent",
                description="Announcement delivered successfully.",
                color=COLOR_SUCCESS,
            ),
            ephemeral=True,
        )

    # ── /admin sync_slash ─────────────────────────────────────────────────────

    @app_commands.command(name="sync_slash", description="[Admin] Re-sync slash commands to Discord")
    @admin_check()
    async def sync_slash(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            synced = await self.bot.tree.sync()
            embed = discord.Embed(
                title="✅ Sync Successful",
                description=f"**{len(synced)}** slash command(s) synced successfully.",
                color=COLOR_SUCCESS,
            )
        except Exception as e:
            embed = discord.Embed(title="❌ Sync Failed", description=str(e), color=COLOR_ERROR)
        await interaction.followup.send(embed=embed, ephemeral=True)


async def setup(bot):
    await bot.add_cog(AdminCommands(bot))
