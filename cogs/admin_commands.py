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

    async def _resolve_add_game(self, query: str) -> tuple[str | None, str]:
        raw = str(query or "").strip()
        if not raw:
            return None, ""

        if raw.isdigit():
            entry = self.db.get_game(raw)
            db_name = str((entry or {}).get("name") or "").strip()
            if db_name:
                return raw, db_name
            steam_name = await self._fetch_steam_title(raw)
            return raw, steam_name or f"App {raw}"

        matches = self.db.search_games(raw, limit=1)
        for item in matches:
            appid = str(item.get("appid") or item.get("id") or "").strip()
            name = str(item.get("name") or "").strip()
            if appid and name:
                return appid, name

        try:
            async with self.bot.session.get(
                bot_config.STEAM_SEARCH_API,
                params={"term": raw, "l": "english", "cc": "US"},
                timeout=aiohttp.ClientTimeout(total=8),
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    for item in data.get("items", []):
                        appid = str(item.get("id") or "").strip()
                        name = str(item.get("name") or "").strip()
                        if appid and name:
                            return appid, name
        except Exception:
            log.exception("Steam search failed for /add_game query %r", raw)

        return None, ""

    async def _fetch_steam_title(self, appid: str) -> str | None:
        try:
            async with self.bot.session.get(
                bot_config.STEAM_STORE_API,
                params={"appids": appid, "l": "english", "cc": bot_config.DEFAULT_CC},
                timeout=aiohttp.ClientTimeout(total=8),
            ) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
                block = data.get(str(appid), {})
                if block.get("success") and isinstance(block.get("data"), dict):
                    name = str(block["data"].get("name") or "").strip()
                    return name or None
        except Exception:
            log.debug("Steam title lookup failed for %s", appid, exc_info=True)
        return None

    @staticmethod
    def _summary_detail(summary) -> str:
        errors = list(getattr(summary, "errors", []) or [])
        samples = list(getattr(summary, "samples", []) or [])
        if errors:
            return str(errors[-1])[:900]
        if samples:
            return str(samples[-1])[:900]
        return "-"

    async def _send_add_game_priority_result(
        self,
        interaction: discord.Interaction,
        appid: str,
        name: str,
        summary,
    ) -> None:
        uploaded = int(getattr(summary, "files_uploaded", 0) or 0)
        existing = int(getattr(summary, "files_existing", 0) or 0)
        skipped = int(getattr(summary, "files_skipped", 0) or 0)
        no_match = int(getattr(summary, "no_match", 0) or 0)
        errors = list(getattr(summary, "errors", []) or [])
        elapsed = float(getattr(summary, "elapsed_seconds", 0.0) or 0.0)

        if uploaded > 0:
            self.db.mark_as_starred(appid, name)
            self.db.save()
            title = "OpenDir upload complete"
            description = f"`{name}` (`{appid}`) sudah berhasil diupload ke R2."
            color = COLOR_SUCCESS
        elif existing > 0:
            self.db.mark_as_starred(appid, name)
            self.db.save()
            title = "OpenDir file already exists"
            description = f"`{name}` (`{appid}`) sudah ada di R2."
            color = COLOR_SUCCESS
        elif errors:
            title = "OpenDir upload failed"
            description = f"`{name}` (`{appid}`) gagal diproses."
            color = COLOR_ERROR
        elif no_match > 0:
            title = "OpenDir file not ready"
            description = f"`{name}` (`{appid}`) belum tersedia dari OpenDir."
            color = COLOR_WARNING
        else:
            title = "OpenDir upload not completed"
            description = f"`{name}` (`{appid}`) selesai dicek, tapi tidak ada file yang diupload."
            color = COLOR_WARNING

        embed = discord.Embed(title=title, description=description, color=color)
        embed.add_field(name="App ID", value=appid, inline=True)
        embed.add_field(name="Name", value=(name or "-")[:1024], inline=True)
        embed.add_field(name="Uploaded", value=str(uploaded), inline=True)
        embed.add_field(name="Already existed", value=str(existing), inline=True)
        embed.add_field(name="No match", value=str(no_match), inline=True)
        embed.add_field(name="Skipped", value=str(skipped), inline=True)
        embed.add_field(name="Elapsed", value=f"{elapsed:.1f}s", inline=True)
        embed.add_field(name="Detail", value=self._summary_detail(summary), inline=False)

        try:
            await interaction.followup.send(embed=embed, ephemeral=True)
            return
        except Exception:
            log.exception("Could not send /add_game final followup for %s", appid)

        try:
            await interaction.user.send(embed=embed)
            return
        except Exception:
            log.exception("Could not DM /add_game final result for %s", appid)

        notifier = getattr(self.bot, "notify_admins", None)
        if notifier:
            result = notifier(
                title,
                description,
                level="error" if errors else "info",
                fields={
                    "App ID": appid,
                    "Name": name,
                    "Uploaded": str(uploaded),
                    "Already existed": str(existing),
                    "No match": str(no_match),
                    "Skipped": str(skipped),
                    "Detail": self._summary_detail(summary)[:500],
                },
                key=f"add-game-result-{appid}",
                force=True,
            )
            if asyncio.iscoroutine(result):
                await result

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
        all_without_file = [g for g in self.db.search_games("", limit=limit * 10) if not g.get("file")]
        to_check = all_without_file[:limit]

        sem = asyncio.Semaphore(20)

        async def check_one(game):
            nonlocal found, checked
            appid = str(game.get("appid") or game.get("id") or "")
            if not appid:
                return
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

    @app_commands.command(name="add_game", description="[Admin] Add a game and priority-process it via OpenDir")
    @app_commands.describe(appid="Steam App ID or game title", name="Game name override (optional)", has_file="Mark as having a file immediately")
    @admin_check()
    async def add_game(self, interaction: discord.Interaction, appid: str,
                       name: Optional[str] = None, has_file: bool = False):
        await interaction.response.defer(ephemeral=True)

        resolved_appid, resolved_name = await self._resolve_add_game(appid)
        if not resolved_appid:
            await interaction.followup.send(
                embed=discord.Embed(
                    title="Game not found",
                    description=f"I could not find a Steam game for `{appid}`.",
                    color=COLOR_ERROR,
                ),
                ephemeral=True,
            )
            return

        appid = resolved_appid
        name = (name or resolved_name or f"App {appid}").strip()

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

        opendir_cog = self.bot.get_cog("OpenDirSync")
        priority_scheduled = False

        async def on_priority_done(summary) -> None:
            await self._send_add_game_priority_result(interaction, appid, name, summary)

        async def on_priority_task_done(done: asyncio.Task) -> None:
            try:
                summary = done.result()
            except asyncio.CancelledError:
                return
            except Exception as exc:
                log.exception("OpenDir direct priority task failed for /add_game %s", appid)

                class FailedSummary:
                    pass

                summary = FailedSummary()
                summary.files_uploaded = 0
                summary.files_existing = 0
                summary.files_skipped = 1
                summary.no_match = 0
                summary.elapsed_seconds = 0.0
                summary.errors = [f"OpenDir priority task failed: {exc!r}"]
                summary.samples = []
            await on_priority_done(summary)

        if opendir_cog:
            if hasattr(opendir_cog, "schedule_priority_sync"):
                priority_scheduled = bool(
                    opendir_cog.schedule_priority_sync(
                        appid,
                        source="add_game",
                        callback=on_priority_done,
                    )
                )
            else:
                task = asyncio.create_task(
                    opendir_cog.run_sync_once(priority_appid=appid),
                    name=f"opendir-priority-{appid}",
                )
                task.add_done_callback(
                    lambda done: asyncio.create_task(on_priority_task_done(done))
                )
                priority_scheduled = True

        embed = discord.Embed(
            title="✅ Database Updated" if not existed or has_file else "ℹ️ Already Exists",
            description=msg,
            color=COLOR_SUCCESS,
        )
        embed.add_field(name="App ID", value=appid, inline=True)
        if name:
            embed.add_field(name="Name", value=name, inline=True)
        embed.add_field(
            name="OpenDir priority",
            value=(
                "Started now in the priority lane. I will send a final confirmation when it uploads, already exists, or fails."
                if priority_scheduled
                else "OpenDir cog not available"
            ),
            inline=False,
        )
        embed.add_field(name="File?", value="✅ Yes" if has_file else "❌ No", inline=True)
        await interaction.followup.send(embed=embed, ephemeral=True)

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
        try:
            import sqlite3 as _sqlite3
            conn = _sqlite3.connect(self.db.db_path)
            conn.execute("DELETE FROM games WHERE appid = ?", (str(appid),))
            conn.commit()
            conn.close()
        except Exception as _del_exc:
            log.error("Failed to delete game %s from SQLite: %s", appid, _del_exc)
        self.db.save()

        embed = discord.Embed(
            title="🗑️ Game Removed",
            description=f"**{game_name}** (`{appid}`) has been removed from the database.",
            color=COLOR_WARNING,
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(name="backup", description="[Admin] Create a manual database backup")
    @admin_check()
    async def backup(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        import json as _json
        from config import DATA_DIR
        from datetime import datetime as _dt
        ok = False
        try:
            all_games = self.db.search_games("", limit=999999)
            ts = _dt.utcnow().strftime("%Y%m%d_%H%M%S")
            backup_path = DATA_DIR / f"games_backup_{ts}.json"
            backup_path.write_text(_json.dumps(all_games, indent=2, ensure_ascii=False), encoding="utf-8")
            ok = True
        except Exception as _bk_exc:
            log.error("Backup failed: %s", _bk_exc)
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
