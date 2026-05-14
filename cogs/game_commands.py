"""
Game Commands Cog
/gen  - Search & download game
/search - Cari di database lokal
/info   - Detail lengkap game dari Steam
"""
import asyncio
import logging
import urllib.parse
from typing import Dict, List, Optional

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

from config import (
    COLOR_DOWNLOAD, COLOR_ERROR, COLOR_INFO, COLOR_SUCCESS, COLOR_WARNING,
    R2_BASE_URL,
)
from utils.helpers import (
    clean_search_string, extract_protection_type, format_size,
    make_safe_filename, truncate_text,
)
from utils.steam_api import SteamAPI

log = logging.getLogger(__name__)

# ── Autocomplete Super Cepat (Hanya DB Lokal) ─────────────────────────────────

async def autocomplete_games(
    interaction: discord.Interaction,
    current: str,
) -> List[app_commands.Choice[str]]:
    """
    Autocomplete 100% instan dari Database Lokal.
    Menghindari error "Loading options failed" dari Discord (Batas 3 detik).
    """
    db = interaction.client.db
    results: List[app_commands.Choice[str]] = []
    
    q = (current or "").lower().strip()
    
    # Kalau kosong, tampilkan game yang filenya tersedia di DB
    if not q:
        starred = [g for g in db.game_db if g.get("file") and g.get("name")]
        for g in starred[:25]:
            results.append(app_commands.Choice(name=f"⭐ {g['name']}"[:100], value=str(g["id"])))
        return results

    # Kalau ada input, cari cepat di DB lokal
    q_clean = clean_search_string(q)
    starred, normal = [], []

    for game in db.game_db:
        name = game.get("name", "")
        appid = str(game.get("id", ""))
        if not name:
            continue
        if q in name.lower() or q_clean in clean_search_string(name) or q in appid:
            if game.get("file"):
                starred.append(game)
            else:
                normal.append(game)

    # Prioritaskan yang ada filenya di atas
    for g in starred[:15]:
        results.append(app_commands.Choice(name=f"⭐ {g['name']}"[:100], value=str(g["id"])))
        
    for g in normal[: 25 - len(results)]:
        results.append(app_commands.Choice(name=f"📁 {g['name']}"[:100], value=str(g["id"])))

    return results


# ── Cog ───────────────────────────────────────────────────────────────────────

class GameCommands(commands.Cog):
    """Commands for searching and downloading games"""

    def __init__(self, bot):
        self.bot = bot
        self.db = bot.db
        self.steam_api: Optional[SteamAPI] = None

    async def cog_load(self):
        if hasattr(self.bot, "session"):
            self.steam_api = SteamAPI(self.bot.session)
            log.info("✅ GameCommands cog loaded")

    # ── /gen ──────────────────────────────────────────────────────────────────

    @app_commands.command(name="gen", description="Cari dan download game Steam")
    @app_commands.describe(query="Ketik Judul Game ATAU App ID (Angka)")
    @app_commands.autocomplete(query=autocomplete_games)
    @app_commands.checks.cooldown(1, 5.0, key=lambda i: i.user.id)
    async def gen(self, interaction: discord.Interaction, query: str):
        await interaction.response.defer()

        target_id = query.strip()

        # FITUR BARU: JIKA INPUT BUKAN ANGKA (JUDUL GAME), SEARCH KE STEAM DULU!
        if not target_id.isdigit():
            safe_q = urllib.parse.quote(target_id)
            search_results = await self.steam_api.search_games(safe_q, limit=1)
            
            if not search_results:
                embed = discord.Embed(
                    title="❌ Game Tidak Ditemukan",
                    description=f"Pencarian untuk **`{target_id}`** tidak membuahkan hasil di Steam.",
                    color=COLOR_ERROR,
                )
                await interaction.edit_original_response(embed=embed)
                return
            
            # Ambil AppID dari hasil pencarian teratas
            target_id = search_results[0]["id"]

        # Fetch Steam details using AppID
        steam_data = await self.steam_api.get_app_details(target_id)
        if not steam_data:
            embed = discord.Embed(
                title="❌ Game Tidak Ditemukan",
                description=f"Tidak ada game dengan App ID **`{target_id}`** di Steam.",
                color=COLOR_ERROR,
            )
            embed.set_footer(text="App ID bisa dicek di store.steampowered.com")
            await interaction.edit_original_response(embed=embed)
            return

        game_info = self.steam_api.extract_game_info(steam_data)
        db_entry = self.db.get_game(target_id)
        found_in_db = db_entry is not None
        has_file = db_entry.get("file", False) if found_in_db else False

        # Cek ketersediaan file asli di R2
        dl = await self._find_download(target_id, game_info["name"])

        # Tampilkan Embed Publik
        await self._send_game_info(interaction, game_info, found_in_db, has_file, dl)

        # Tampilkan Link Download Privat (Jika Ada)
        if dl["available"]:
            self.db.mark_as_starred(target_id, game_info["name"])
            self.db.save()
            await self._send_download_embed(interaction, game_info, dl)
        else:
            await interaction.followup.send(
                embed=self._unavailable_embed(game_info["name"]),
                ephemeral=True,
            )

    # ── /search ───────────────────────────────────────────────────────────────

    @app_commands.command(name="search", description="Cari game di database lokal")
    @app_commands.describe(query="Nama game yang ingin dicari")
    async def search(self, interaction: discord.Interaction, query: str):
        await interaction.response.defer(ephemeral=True)

        results = self.db.search_games(query, limit=10)
        if not results:
            await interaction.followup.send(
                embed=discord.Embed(
                    title="🔍 Tidak Ditemukan",
                    description=f"Tidak ada game yang cocok dengan **`{query}`** di database.",
                    color=COLOR_ERROR,
                ),
                ephemeral=True,
            )
            return

        embed = discord.Embed(
            title=f"🔍 Hasil: `{query}`",
            description=f"Ditemukan **{len(results)}** game",
            color=COLOR_SUCCESS,
        )
        for game in results:
            icon = "⭐" if game.get("file") else "📁"
            status = "File Tersedia" if game.get("file") else "Terdaftar"
            embed.add_field(
                name=f"{icon} {game.get('name', 'Unknown')}",
                value=f"AppID: `{game['id']}` • {status}",
                inline=False,
            )
        embed.set_footer(text="Gunakan /gen <Nama Game> untuk download")
        await interaction.followup.send(embed=embed, ephemeral=True)

    # ── /info ─────────────────────────────────────────────────────────────────

    @app_commands.command(name="info", description="Lihat detail lengkap sebuah game")
    @app_commands.describe(appid="Steam App ID (angka)")
    async def info(self, interaction: discord.Interaction, appid: str):
        await interaction.response.defer(ephemeral=True)

        if not appid.isdigit():
            await interaction.followup.send(
                embed=discord.Embed(
                    title="❌ App ID Tidak Valid",
                    description=f"`{appid}` bukan angka yang valid.",
                    color=COLOR_ERROR,
                ),
                ephemeral=True,
            )
            return

        steam_data = await self.steam_api.get_app_details(appid)
        if not steam_data:
            await interaction.followup.send(
                embed=discord.Embed(
                    title="❌ Tidak Ditemukan",
                    description=f"Game dengan App ID `{appid}` tidak ada di Steam.",
                    color=COLOR_ERROR,
                ),
                ephemeral=True,
            )
            return

        game_info = self.steam_api.extract_game_info(steam_data)
        db_game = self.db.get_game(appid)
        protection = extract_protection_type(game_info.get("drm_notice"))

        embed = discord.Embed(
            title=f"🎮 {game_info['name']}",
            description=game_info["short_description"],
            color=COLOR_INFO,
            url=f"https://store.steampowered.com/app/{appid}",
        )
        embed.add_field(name="🆔 AppID",      value=f"`{appid}`",              inline=True)
        embed.add_field(name="🎯 Tipe",       value=game_info["type"],         inline=True)
        embed.add_field(name="💰 Harga",      value=game_info["price"],        inline=True)
        embed.add_field(name="🏷️ Genre",      value=game_info["genres"],       inline=True)
        embed.add_field(name="📅 Rilis",      value=game_info["release_date"], inline=True)
        embed.add_field(name="🛡️ DRM",        value=protection,                inline=True)
        embed.add_field(name="👥 Developer",  value=game_info["developers"],   inline=False)
        embed.add_field(name="🏢 Publisher",  value=game_info["publishers"],   inline=False)
        if db_game:
            db_status = "✅ Ada di Database"
            if db_game.get("file"):
                db_status += " • ⭐ File Tersedia"
            embed.add_field(name="📊 Status DB", value=db_status, inline=False)
        if game_info.get("header_image"):
            embed.set_image(url=game_info["header_image"])
        embed.set_footer(text=f"SteamTools • App ID: {appid}")
        await interaction.followup.send(embed=embed, ephemeral=True)

    # ── Error handler ─────────────────────────────────────────────────────────

    @gen.error
    async def gen_error(self, interaction: discord.Interaction, error):
        if isinstance(error, app_commands.CommandOnCooldown):
            embed = discord.Embed(
                title="⏳ Cooldown",
                description=f"Tunggu **{error.retry_after:.1f} detik** sebelum request lagi.",
                color=COLOR_WARNING,
            )
            if interaction.response.is_done():
                await interaction.followup.send(embed=embed, ephemeral=True)
            else:
                await interaction.response.send_message(embed=embed, ephemeral=True)
        else:
            log.error(f"/gen error: {error}", exc_info=error)

    # ── Internal helpers ──────────────────────────────────────────────────────

    async def _find_download(self, appid: str, game_name: str) -> Dict:
        """Cek ketersediaan file di R2 dengan HEAD request."""
        filename = f"{make_safe_filename(game_name)} [{appid}].zip"
        url = f"{R2_BASE_URL}/Database/{appid}.zip"

        if not R2_BASE_URL:
            return {"available": False, "url": None, "size_bytes": 0, "filename": filename}

        try:
            async with self.bot.session.head(
                url, timeout=aiohttp.ClientTimeout(total=4), allow_redirects=True
            ) as resp:
                if resp.status == 200:
                    size = int(resp.headers.get("Content-Length", 0))
                    return {"available": True, "url": url, "size_bytes": size, "filename": filename}
        except Exception as e:
            log.error(f"R2 HEAD error for {appid}: {e}")

        return {"available": False, "url": None, "size_bytes": 0, "filename": filename}

    async def _send_game_info(self, interaction, game_info, found_in_db, has_file, dl):
        """Embed publik."""
        protection = extract_protection_type(game_info.get("drm_notice"))

        if dl["available"]:   color = COLOR_DOWNLOAD
        elif has_file:        color = COLOR_SUCCESS
        elif found_in_db:     color = COLOR_INFO
        else:                 color = COLOR_WARNING

        embed = discord.Embed(
            title=f"🎮 {game_info['name']}",
            description=truncate_text(game_info["short_description"], 300),
            color=color,
            url=f"https://store.steampowered.com/app/{game_info['appid']}",
        )

        embed.add_field(name="🆔 AppID",      value=f"`{game_info['appid']}`", inline=True)
        embed.add_field(name="🛡️ Protection", value=protection,               inline=True)
        embed.add_field(name="🏷️ Genre",      value=game_info["genres"],      inline=True)

        if dl["available"]:     sv = "✅ **Download Tersedia**"
        elif has_file:          sv = "✅ Verified"
        elif found_in_db:       sv = "📁 Scanned"
        else:                   sv = "🌐 Ditemukan"

        embed.add_field(name="📊 Status",  value=sv,                          inline=True)
        embed.add_field(name="📅 Release", value=game_info["release_date"],   inline=True)
        embed.add_field(name="💰 Harga",   value=game_info["price"],          inline=True)
        embed.add_field(
            name="👥 Developer",
            value=f"{game_info['developers']} • *{game_info['publishers']}*",
            inline=False,
        )

        if dl["available"]:
            size_str = format_size(dl["size_bytes"]) if dl["size_bytes"] else "—"
            embed.add_field(
                name="⬇️ Download",
                value=f"Tersedia • `{size_str}`\n🔒 *Link dikirim secara pribadi*",
                inline=False,
            )
        else:
            embed.add_field(name="⬇️ Download", value="❌ File belum tersedia", inline=False)

        if game_info.get("header_image"):
            embed.set_image(url=game_info["header_image"])

        embed.set_footer(text="SteamTools • Link download hanya terlihat oleh kamu")
        await interaction.edit_original_response(embed=embed)

    async def _send_download_embed(self, interaction, game_info, dl):
        """Embed privat dengan link download."""
        size_str = format_size(dl["size_bytes"]) if dl["size_bytes"] else "Tidak diketahui"

        embed = discord.Embed(
            title="🔒 Link Download Pribadimu",
            description=(
                f"**{game_info['name']}**\n"
                "Pesan ini **hanya terlihat olehmu** dan tidak muncul di channel."
            ),
            color=COLOR_DOWNLOAD,
        )
        embed.add_field(name="📁 File",    value=f"`{dl['filename']}`", inline=True)
        embed.add_field(name="💾 Ukuran", value=size_str,               inline=True)
        embed.add_field(name="🌐 Sumber", value="Cloudflare R2",        inline=True)
        embed.add_field(
            name="⚠️ Perhatian",
            value="• Jangan bagikan link ini\n• Link bisa berubah\n• Extract setelah download selesai",
            inline=False,
        )
        if game_info.get("header_image"):
            embed.set_thumbnail(url=game_info["header_image"])
        embed.set_footer(text="SteamTools • Pesan ini hanya untukmu")

        view = discord.ui.View(timeout=None)
        view.add_item(
            discord.ui.Button(
                label="⬇️  Download Sekarang",
                url=dl["url"],
                style=discord.ButtonStyle.link,
            )
        )
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)

    def _unavailable_embed(self, game_name: str) -> discord.Embed:
        embed = discord.Embed(
            title="❌ Download Tidak Tersedia",
            description=(
                f"**{game_name}** belum tersedia untuk didownload.\n\n"
                "Kemungkinan penyebab:\n"
                "• Belum ada di ManifestHub / R2\n"
                "• DRM yang belum didukung (Denuvo, dll)\n"
                "• File sedang diproses\n\n"
                "Coba lagi nanti atau hubungi admin."
            ),
            color=COLOR_ERROR,
        )
        embed.set_footer(text="Pesan ini hanya terlihat olehmu")
        return embed


async def setup(bot):
    await bot.add_cog(GameCommands(bot))