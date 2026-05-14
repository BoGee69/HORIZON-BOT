"""
Admin Commands Cog
Semua command di sini hanya bisa dijalankan oleh Admin ID yang ada di config.
Semua response ephemeral (hanya terlihat admin yang menjalankan).
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
    """app_commands check untuk admin only."""
    async def predicate(interaction: discord.Interaction) -> bool:
        if not is_admin(interaction.user.id):
            await interaction.response.send_message(
                embed=discord.Embed(
                    title="🚫 Akses Ditolak",
                    description="Command ini hanya untuk admin bot.",
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

    @app_commands.command(name="status", description="[Admin] Status bot & database")
    @admin_check()
    async def status(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        uptime = discord.utils.utcnow() - self.bot.start_time
        hours, rem = divmod(int(uptime.total_seconds()), 3600)
        mins, secs = divmod(rem, 60)
        uptime_str = f"{hours}j {mins}m {secs}d"

        stats = self.db.get_stats()

        embed = discord.Embed(title="📊 Status Bot", color=COLOR_INFO,
                              timestamp=discord.utils.utcnow())
        embed.add_field(name="🤖 Bot",         value=str(self.bot.user),                    inline=True)
        embed.add_field(name="⏱️ Uptime",      value=uptime_str,                             inline=True)
        embed.add_field(name="📡 Ping",        value=f"{round(self.bot.latency * 1000)}ms",  inline=True)
        embed.add_field(name="🌐 Guilds",      value=format_number(len(self.bot.guilds)),     inline=True)
        embed.add_field(name="👥 Users",       value=format_number(sum(g.member_count or 0 for g in self.bot.guilds)), inline=True)
        embed.add_field(name="📦 Version",     value=self.bot.version,                        inline=True)
        embed.add_field(name="🗄️ Total Game",  value=format_number(stats["total"]),           inline=True)
        embed.add_field(name="⭐ Punya File",  value=format_number(stats["with_files"]),      inline=True)
        embed.add_field(name="🏷️ Ada Nama",    value=format_number(stats["with_names"]),      inline=True)
        embed.add_field(name="🔝 Last AppID",  value=format_number(stats["last_appid"]),      inline=True)
        embed.add_field(name="☁️ R2 URL",      value=R2_BASE_URL or "❌ Tidak diset",          inline=False)
        embed.set_footer(text=f"Diminta oleh {interaction.user}")
        await interaction.followup.send(embed=embed, ephemeral=True)

    # ── /admin check_r2 ───────────────────────────────────────────────────────

    @app_commands.command(name="check_r2", description="[Admin] Scan DB dan tandai game yang filenya ada di R2")
    @app_commands.describe(limit="Maksimal game yang dicek (default 500)")
    @admin_check()
    async def check_r2(self, interaction: discord.Interaction, limit: int = 500):
        await interaction.response.defer(ephemeral=True)

        if not R2_BASE_URL:
            await interaction.followup.send(
                embed=discord.Embed(
                    title="❌ R2 Belum Dikonfigurasi",
                    description="Set `R2_BASE_URL` di `.env` terlebih dahulu.",
                    color=COLOR_ERROR,
                ),
                ephemeral=True,
            )
            return

        embed = discord.Embed(
            title="☁️ Scan R2",
            description=f"Mengecek hingga **{limit}** game ke R2...",
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

        embed = discord.Embed(title="✅ Scan R2 Selesai", color=COLOR_SUCCESS)
        embed.add_field(name="🔍 Dicek",        value=format_number(checked), inline=True)
        embed.add_field(name="✅ Ditemukan",    value=format_number(found),   inline=True)
        embed.add_field(name="⭐ Total File",   value=format_number(self.db.get_stats()["with_files"]), inline=True)
        await interaction.edit_original_response(embed=embed)

    # ── /admin add_game ────────────────────────────────────────────────────────

    @app_commands.command(name="add_game", description="[Admin] Tambah game manual ke database")
    @app_commands.describe(appid="Steam App ID", name="Nama game (opsional)", has_file="Langsung tandai punya file")
    @admin_check()
    async def add_game(self, interaction: discord.Interaction, appid: str,
                       name: Optional[str] = None, has_file: bool = False):
        await interaction.response.defer(ephemeral=True)

        if not appid.isdigit():
            await interaction.followup.send(
                embed=discord.Embed(title="❌ App ID Tidak Valid", description=f"`{appid}` bukan angka.", color=COLOR_ERROR),
                ephemeral=True,
            )
            return

        existed = self.db.get_game(appid) is not None
        if existed:
            if has_file:
                self.db.mark_as_starred(appid, name)
                self.db.save()
                msg = f"Game `{appid}` sudah ada, status diupdate → ⭐ punya file."
            else:
                msg = f"Game `{appid}` sudah ada di database."
        else:
            self.db.add_game(appid, name, has_file)
            self.db.save()
            msg = f"Game `{appid}` berhasil ditambahkan."

        embed = discord.Embed(
            title="✅ Database Updated" if not existed or has_file else "ℹ️ Sudah Ada",
            description=msg,
            color=COLOR_SUCCESS,
        )
        embed.add_field(name="AppID", value=appid, inline=True)
        if name:
            embed.add_field(name="Nama", value=name, inline=True)
        embed.add_field(name="File?", value="✅ Ya" if has_file else "❌ Tidak", inline=True)
        await interaction.followup.send(embed=embed, ephemeral=True)

    # ── /admin remove_game ────────────────────────────────────────────────────

    @app_commands.command(name="remove_game", description="[Admin] Hapus game dari database")
    @app_commands.describe(appid="Steam App ID yang akan dihapus")
    @admin_check()
    async def remove_game(self, interaction: discord.Interaction, appid: str):
        await interaction.response.defer(ephemeral=True)

        game = self.db.get_game(appid)
        if not game:
            await interaction.followup.send(
                embed=discord.Embed(title="❌ Tidak Ditemukan", description=f"Game `{appid}` tidak ada di database.", color=COLOR_ERROR),
                ephemeral=True,
            )
            return

        game_name = game.get("name", "Unknown")
        self.db.game_db = [g for g in self.db.game_db if str(g["id"]) != appid]
        self.db.game_index.pop(appid, None)
        self.db.save()

        embed = discord.Embed(
            title="🗑️ Game Dihapus",
            description=f"**{game_name}** (`{appid}`) berhasil dihapus dari database.",
            color=COLOR_WARNING,
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

    # ── /admin db_stats ───────────────────────────────────────────────────────

    @app_commands.command(name="db_stats", description="[Admin] Statistik detail database")
    @admin_check()
    async def db_stats(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        stats = self.db.get_stats()
        pct_file = (stats["with_files"] / stats["total"] * 100) if stats["total"] else 0
        pct_name = (stats["with_names"] / stats["total"] * 100) if stats["total"] else 0

        embed = discord.Embed(title="🗄️ Statistik Database", color=COLOR_INFO,
                              timestamp=discord.utils.utcnow())
        embed.add_field(name="📊 Total Entry",    value=format_number(stats["total"]),      inline=True)
        embed.add_field(name="⭐ Punya File",     value=f"{format_number(stats['with_files'])} ({pct_file:.1f}%)", inline=True)
        embed.add_field(name="🏷️ Ada Nama",       value=f"{format_number(stats['with_names'])} ({pct_name:.1f}%)", inline=True)
        embed.add_field(name="🔝 AppID Tertinggi", value=format_number(stats["last_appid"]), inline=True)

        # Backup info
        from config import DATA_DIR
        backups = sorted(DATA_DIR.glob("games_backup_*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        backup_info = f"{len(backups)} backup tersedia"
        if backups:
            latest = datetime.fromtimestamp(backups[0].stat().st_mtime)
            backup_info += f"\nTerbaru: `{latest.strftime('%d %b %Y %H:%M')}`"
        embed.add_field(name="💾 Backup", value=backup_info, inline=False)
        await interaction.followup.send(embed=embed, ephemeral=True)

    # ── /admin backup ─────────────────────────────────────────────────────────

    @app_commands.command(name="backup", description="[Admin] Buat backup database manual")
    @admin_check()
    async def backup(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        ok = self.db._create_backup()
        if ok:
            embed = discord.Embed(
                title="💾 Backup Berhasil",
                description="Database berhasil di-backup.",
                color=COLOR_SUCCESS,
            )
        else:
            embed = discord.Embed(
                title="❌ Backup Gagal",
                description="Terjadi kesalahan saat membuat backup.",
                color=COLOR_ERROR,
            )
        await interaction.followup.send(embed=embed, ephemeral=True)

    # ── /admin reload_cog ─────────────────────────────────────────────────────

    @app_commands.command(name="reload_cog", description="[Admin] Reload cog tanpa restart bot")
    @app_commands.describe(cog="Nama cog (contoh: game_commands)")
    @admin_check()
    async def reload_cog(self, interaction: discord.Interaction, cog: str):
        await interaction.response.defer(ephemeral=True)

        cog_name = f"cogs.{cog}"
        try:
            await self.bot.reload_extension(cog_name)
            embed = discord.Embed(
                title="✅ Reload Berhasil",
                description=f"`{cog_name}` berhasil di-reload.",
                color=COLOR_SUCCESS,
            )
        except commands.ExtensionNotLoaded:
            try:
                await self.bot.load_extension(cog_name)
                embed = discord.Embed(
                    title="✅ Load Berhasil",
                    description=f"`{cog_name}` berhasil di-load.",
                    color=COLOR_SUCCESS,
                )
            except Exception as e:
                embed = discord.Embed(title="❌ Gagal", description=str(e), color=COLOR_ERROR)
        except Exception as e:
            embed = discord.Embed(title="❌ Gagal", description=str(e), color=COLOR_ERROR)

        await interaction.followup.send(embed=embed, ephemeral=True)

    # ── /admin announce ───────────────────────────────────────────────────────

    @app_commands.command(name="announce", description="[Admin] Kirim pengumuman ke channel ini")
    @app_commands.describe(message="Isi pengumuman", title="Judul (opsional)")
    @admin_check()
    async def announce(self, interaction: discord.Interaction, message: str,
                       title: Optional[str] = "📢 Pengumuman"):
        await interaction.response.defer(ephemeral=True)

        embed = discord.Embed(title=title, description=message, color=COLOR_INFO,
                              timestamp=discord.utils.utcnow())
        embed.set_footer(text=f"Dari admin • {interaction.guild.name if interaction.guild else 'DM'}")

        await interaction.channel.send(embed=embed)
        await interaction.followup.send(
            embed=discord.Embed(title="✅ Terkirim", description="Pengumuman berhasil dikirim.", color=COLOR_SUCCESS),
            ephemeral=True,
        )

    # ── /admin refresh_trending ───────────────────────────────────────────────

    @app_commands.command(name="refresh_trending", description="[Admin] Paksa refresh cache trending games")
    @admin_check()
    async def refresh_trending(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        # Reset cache
        from cogs.game_commands import _trending_cache
        _trending_cache["ts"] = 0.0

        # Trigger refresh
        from cogs.game_commands import _get_trending_with_r2
        games = await _get_trending_with_r2(self.bot)

        embed = discord.Embed(
            title="🔥 Trending Diperbarui",
            description=f"Cache trending berhasil di-refresh.\n**{len(games)}** game ditemukan di R2.",
            color=COLOR_SUCCESS,
        )
        for i, g in enumerate(games[:10], 1):
            embed.add_field(name=f"{i}. {g['name']}", value=f"`{g['appid']}`", inline=True)
        await interaction.followup.send(embed=embed, ephemeral=True)

    # ── /admin sync_slash ─────────────────────────────────────────────────────

    @app_commands.command(name="sync_slash", description="[Admin] Sync ulang slash commands ke Discord")
    @admin_check()
    async def sync_slash(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            synced = await self.bot.tree.sync()
            embed = discord.Embed(
                title="✅ Sync Berhasil",
                description=f"**{len(synced)}** slash commands berhasil di-sync.",
                color=COLOR_SUCCESS,
            )
        except Exception as e:
            embed = discord.Embed(title="❌ Sync Gagal", description=str(e), color=COLOR_ERROR)
        await interaction.followup.send(embed=embed, ephemeral=True)


async def setup(bot):
    await bot.add_cog(AdminCommands(bot))
