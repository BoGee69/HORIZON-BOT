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

import config as bot_config
from config import (
    ADMIN_IDS, ADMIN_ROLE_IDS, ADMIN_ROLE_NAMES, ADMIN_WEBHOOK, COLOR_DOWNLOAD, COLOR_ERROR, COLOR_INFO,
    COLOR_SUCCESS, COLOR_WARNING, GEN_DAILY_LIMIT, GEN_USAGE_PATH, R2_BASE_URL,
)
from utils.diagnostics import collect_health, yes_no
from utils.helpers import format_number, format_size, has_any_role_id, has_any_role_name, is_admin_interaction
from utils.gen_limits import DailyGenLimiter

log = logging.getLogger(__name__)


def is_admin(interaction: discord.Interaction) -> bool:
    return is_admin_interaction(interaction, ADMIN_IDS, ADMIN_ROLE_IDS, ADMIN_ROLE_NAMES)


def admin_check():
    """app_commands check — admin only."""
    async def predicate(interaction: discord.Interaction) -> bool:
        if not is_admin(interaction):
            notifier = getattr(interaction.client, "notify_admins", None)
            if notifier:
                await notifier(
                    "Unauthorized admin command attempt",
                    "A user tried to run an admin-only command.",
                    level="warning",
                    fields={
                        "User": f"{interaction.user} ({interaction.user.id})",
                        "Command": getattr(getattr(interaction, "command", None), "qualified_name", "unknown"),
                        "Guild": str(interaction.guild or "DM"),
                    },
                    key=f"unauthorized-admin-{interaction.user.id}",
                )
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
        self.gen_limiter = getattr(bot, "gen_limiter", None)
        if self.gen_limiter is None:
            self.gen_limiter = DailyGenLimiter()
            bot.gen_limiter = self.gen_limiter

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

    @app_commands.command(name="config_status", description="[Admin] Check runtime config, storage, and alerts")
    @admin_check()
    async def config_status(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        health = await collect_health(self.bot)
        checks = health["checks"]
        paths = health["paths"]
        database = health["database"]
        roles = health["roles"]
        r2 = health["r2"]
        r2_maintenance = health["r2_maintenance"]
        steam_db_sync = health["steam_db_sync"]

        embed = discord.Embed(
            title="Runtime Config Status",
            color=COLOR_SUCCESS if health["ok"] else COLOR_WARNING,
            timestamp=discord.utils.utcnow(),
        )
        embed.add_field(
            name="Core",
            value=(
                f"Discord ready: `{yes_no(checks['discord_ready'])}`\n"
                f"HTTP session: `{yes_no(checks['http_session_open'])}`\n"
                f"JWT secret: `{yes_no(checks['jwt_secret_configured'])}`\n"
                f"WEB_URL: `{bot_config.WEB_URL}`"
            ),
            inline=False,
        )
        embed.add_field(
            name="Storage",
            value=(
                f"GEN_USAGE_PATH: `{paths['gen_usage_path']}`\n"
                f"Writable: `{yes_no(checks['gen_usage_path_writable'])}`\n"
                f"Status: `{paths['gen_usage_parent_status']}`"
            ),
            inline=False,
        )
        embed.add_field(
            name="Database",
            value=(
                f"DB_PATH: `{database['db_path']}`\n"
                f"Total games: `{database['total_games']}`\n"
                f"With files: `{database['with_files']}`"
            ),
            inline=False,
        )
        embed.add_field(
            name="Steam DB Sync",
            value=(
                f"Enabled: `{steam_db_sync['enabled']}`\n"
                f"Apply: `{steam_db_sync['apply']}`\n"
                f"Run on start: `{steam_db_sync['run_on_start']}`\n"
                f"Start delay: `{steam_db_sync['start_delay_seconds']}s`\n"
                f"Interval: `{steam_db_sync['interval_hours']}h`\n"
                f"Include new: `{steam_db_sync['include_new']}`\n"
                f"Max new: `{steam_db_sync['max_new']}`\n"
                f"Max updates: `{steam_db_sync['max_updates']}`\n"
                f"Page size: `{steam_db_sync['page_size']}`\n"
                f"Types: `games={steam_db_sync['include_games']}, dlc={steam_db_sync['include_dlc']}, software={steam_db_sync['include_software']}`\n"
                f"Steam API key: `{yes_no(steam_db_sync['steam_api_key_configured'])}`"
            ),
            inline=False,
        )
        embed.add_field(
            name="R2",
            value=(
                f"Public URL: `{yes_no(r2['public_base_url_configured'])}`\n"
                f"Presign: `{yes_no(r2['presign_enabled'])}`\n"
                f"Bucket: `{yes_no(r2['bucket_configured'])}`\n"
                f"Link expiry: `{r2['link_expire_seconds']}s`"
            ),
            inline=False,
        )
        embed.add_field(
            name="R2 Maintenance",
            value=(
                f"Enabled: `{r2_maintenance['enabled']}`\n"
                f"Apply: `{r2_maintenance['apply']}`\n"
                f"Run on start: `{r2_maintenance['run_on_start']}`\n"
                f"Start delay: `{r2_maintenance['start_delay_seconds']}s`\n"
                f"Interval: `{r2_maintenance['interval_hours']}h`\n"
                f"Prefix: `{r2_maintenance['prefix']}`\n"
                f"Max objects: `{r2_maintenance['max_objects']}`\n"
                f"Rename: `{r2_maintenance['rename_objects']}`\n"
                f"Clean comments: `{r2_maintenance['clean_comments']}`\n"
                f"Clean extensions: `{', '.join(r2_maintenance['clean_extensions'])}`\n"
                f"Steam lookups: `{r2_maintenance['steam_lookups']}`\n"
                f"Max Steam lookups: `{r2_maintenance['max_steam_lookups']}`\n"
                f"Steam delay: `{r2_maintenance['steam_delay_seconds']}s`"
            ),
            inline=False,
        )
        embed.add_field(
            name="Roles",
            value=(
                f"Admin role IDs: `{roles['admin_role_ids']}`\n"
                f"Donor role IDs: `{roles['donor_role_ids']}`\n"
                f"Booster role IDs: `{roles['booster_role_ids']}`\n"
                f"Fallback names: donor={roles['donor_role_names']}, booster={roles['booster_role_names']}"
            ),
            inline=False,
        )
        embed.add_field(
            name="Alerts",
            value=(
                f"DM admins: `{len(bot_config.ADMIN_ALERT_IDS)}`\n"
                f"Cooldown: `{bot_config.ADMIN_ALERT_COOLDOWN_SECONDS}s`\n"
                f"Limit hit alerts: `{bot_config.ALERT_ON_LIMIT_HIT}`"
            ),
            inline=False,
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(name="role_debug", description="[Admin] Explain why a member is limited or exempt")
    @app_commands.describe(member="Server member to inspect")
    @admin_check()
    async def role_debug(self, interaction: discord.Interaction, member: discord.Member):
        await interaction.response.defer(ephemeral=True)

        is_admin_user = is_admin_interaction(interaction, ADMIN_IDS, ADMIN_ROLE_IDS, ADMIN_ROLE_NAMES)
        target_admin = (
            member.id in ADMIN_IDS
            or (interaction.guild and interaction.guild.owner_id == member.id)
            or bool(getattr(member.guild_permissions, "administrator", False))
            or has_any_role_id(member, ADMIN_ROLE_IDS)
            or has_any_role_name(member, ADMIN_ROLE_NAMES)
        )
        donor_id = has_any_role_id(member, bot_config.DONOR_ROLE_IDS)
        donor_name = has_any_role_name(member, bot_config.DONOR_ROLE_NAMES)
        booster_id = has_any_role_id(member, bot_config.BOOSTER_ROLE_IDS)
        booster_name = has_any_role_name(member, bot_config.BOOSTER_ROLE_NAMES)
        exempt = target_admin or donor_id or donor_name or booster_id or booster_name
        allowed, used, remaining = self.gen_limiter.check(member.id)

        reasons = []
        if target_admin:
            reasons.append("admin/owner/admin permission")
        if donor_id:
            reasons.append("donor role ID")
        if donor_name:
            reasons.append("donor role name")
        if booster_id:
            reasons.append("booster role ID")
        if booster_name:
            reasons.append("booster role name")

        embed = discord.Embed(
            title="Role Debug",
            description=f"{member.mention} (`{member.id}`)",
            color=COLOR_SUCCESS if exempt or allowed else COLOR_WARNING,
        )
        embed.add_field(name="Limit exempt?", value="Yes" if exempt else "No", inline=True)
        embed.add_field(name="Reason", value=", ".join(reasons) or "No exemption matched", inline=False)
        embed.add_field(name="Usage", value=f"{used}/{GEN_DAILY_LIMIT} used, {remaining} remaining", inline=False)
        embed.add_field(
            name="Role IDs",
            value=", ".join(str(role.id) for role in member.roles if role.name != "@everyone") or "-",
            inline=False,
        )
        embed.add_field(
            name="Role names",
            value=", ".join(role.name for role in member.roles if role.name != "@everyone") or "-",
            inline=False,
        )
        embed.set_footer(text=f"Checked by admin: {interaction.user} | Admin check: {is_admin_user}")
        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(name="alert_test", description="[Admin] Send a test DM alert to configured admins")
    @admin_check()
    async def alert_test(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        delivered = await self.bot.notify_admins(
            "Test alert from triadbot",
            "DM alert delivery is working.",
            level="info",
            fields={
                "Triggered by": f"{interaction.user} ({interaction.user.id})",
                "Guild": str(interaction.guild or "DM"),
            },
            key=f"alert-test-{interaction.user.id}",
            force=True,
        )
        embed = discord.Embed(
            title="Alert Test Sent" if delivered else "Alert Test Failed",
            description=f"Delivered to **{delivered}** admin account(s).",
            color=COLOR_SUCCESS if delivered else COLOR_WARNING,
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

    # Daily /gen limit tools

    @app_commands.command(name="limit_status", description="[Admin] Check a user's daily /gen usage")
    @app_commands.describe(user="Discord user to check")
    @admin_check()
    async def limit_status(self, interaction: discord.Interaction, user: discord.User):
        await interaction.response.defer(ephemeral=True)

        allowed, used, remaining = self.gen_limiter.check(user.id)
        reset_ts = int(self.gen_limiter.reset_at_utc().timestamp())

        embed = discord.Embed(
            title="Daily /gen Limit Status",
            color=COLOR_SUCCESS if allowed else COLOR_WARNING,
            timestamp=discord.utils.utcnow(),
        )
        embed.add_field(name="User", value=f"{user.mention}\n`{user.id}`", inline=False)
        embed.add_field(name="Used", value=f"{used}/{GEN_DAILY_LIMIT}", inline=True)
        embed.add_field(name="Remaining", value=str(remaining), inline=True)
        embed.add_field(name="Allowed now?", value="Yes" if allowed else "No", inline=True)
        embed.add_field(name="Reset", value=f"<t:{reset_ts}:F>\n<t:{reset_ts}:R>", inline=False)
        embed.add_field(name="Storage", value=f"`{GEN_USAGE_PATH}`", inline=False)
        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(name="limit_reset", description="[Admin] Reset daily /gen usage")
    @app_commands.describe(
        user="Discord user to reset",
        reset_all="Reset all users instead of one user",
    )
    @admin_check()
    async def limit_reset(
        self,
        interaction: discord.Interaction,
        user: Optional[discord.User] = None,
        reset_all: bool = False,
    ):
        await interaction.response.defer(ephemeral=True)

        if reset_all:
            affected = self.gen_limiter.reset_all()
            embed = discord.Embed(
                title="Daily /gen Limits Reset",
                description=f"Reset usage for **{affected}** tracked user(s).",
                color=COLOR_SUCCESS,
            )
            await interaction.followup.send(embed=embed, ephemeral=True)
            return

        if not user:
            embed = discord.Embed(
                title="Missing User",
                description="Choose a user, or set `reset_all` to true.",
                color=COLOR_ERROR,
            )
            await interaction.followup.send(embed=embed, ephemeral=True)
            return

        previous, remaining = self.gen_limiter.reset_user(user.id)
        embed = discord.Embed(
            title="Daily /gen Limit Reset",
            description=f"{user.mention} has been reset.",
            color=COLOR_SUCCESS,
        )
        embed.add_field(name="Previous usage", value=str(previous), inline=True)
        embed.add_field(name="Remaining now", value=str(remaining), inline=True)
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
