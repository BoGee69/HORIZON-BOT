"""
Game Commands Cog  —  /gen  /search  /info
"""
import asyncio
import logging
import time
import urllib.parse
import jwt
from typing import Dict, List, Optional

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

from config import (
    COLOR_DOWNLOAD, COLOR_ERROR, COLOR_INFO, COLOR_SUCCESS, COLOR_WARNING,
    DEFAULT_CC, LINK_EXPIRE_SECONDS, R2_BASE_URL, WEB_URL, JWT_SECRET,
)
from utils.helpers import (
    clean_search_string, extract_protection_type, format_size, truncate_text,
)
from utils.r2_presign import generate_presigned_url, _PRESIGN_ENABLED
from utils.steam_api import SteamAPI, locale_to_country_code

log = logging.getLogger(__name__)

# Urutan ini harus SAMA PERSIS dengan bot.py agar JWT redirect pakai key yang benar
R2_KEY_PATTERNS = [
    "Database/{appid}.zip",
    "Database/[{appid}].zip",
    "[{appid}].zip",
    "{appid}.zip",
]


def resolve_country_code(interaction: discord.Interaction) -> str:
    guild_locale = getattr(interaction, "guild_locale", None)
    if guild_locale:
        cc = locale_to_country_code(str(guild_locale))
        if cc != "us":
            return cc
    user_locale = str(interaction.locale)
    if user_locale not in ("en-US", "en-GB", "en"):
        cc = locale_to_country_code(user_locale)
        if cc != "us":
            return cc
    return DEFAULT_CC


# ── Autocomplete ──────────────────────────────────────────────────────────────

async def autocomplete_games(
    interaction: discord.Interaction, current: str
) -> List[app_commands.Choice[str]]:
    bot = interaction.client
    db  = bot.db

    q_raw   = (current or "").lower().strip()
    q_clean = clean_search_string(q_raw)

    starred, normal = [], []
    for game in db.game_db:
        name  = game.get("name")
        appid = str(game.get("id", ""))
        if not name:
            continue
        name_raw   = name.lower()
        name_clean = clean_search_string(name_raw)
        if not q_raw or q_raw in name_raw or (q_clean and q_clean in name_clean) or q_raw in appid:
            (starred if game.get("file") else normal).append(game)

    results:   List[app_commands.Choice] = []
    found_ids: set = set()

    for game in starred[:15]:
        results.append(app_commands.Choice(name=game["name"][:100], value=str(game["id"])))
        found_ids.add(str(game["id"]))

    for game in normal[: 25 - len(results)]:
        gid = str(game["id"])
        if gid not in found_ids:
            results.append(app_commands.Choice(name=game["name"][:100], value=gid))
            found_ids.add(gid)

    if q_raw and len(results) < 25:
        try:
            steam_api   = SteamAPI(bot.session)
            steam_items = await steam_api.search_games(urllib.parse.quote(q_raw), limit=25 - len(results))
            for item in steam_items:
                aid = item["id"]
                if aid not in found_ids:
                    results.append(app_commands.Choice(name=item["name"][:100], value=aid))
                    found_ids.add(aid)
                if len(results) >= 25:
                    break
        except Exception as e:
            log.error(f"Steam autocomplete error: {e}")

    return results


# ── Cog ───────────────────────────────────────────────────────────────────────

class GameCommands(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.db  = bot.db
        self.steam_api: Optional[SteamAPI] = None

    async def cog_load(self):
        if hasattr(self.bot, "session"):
            self.steam_api = SteamAPI(self.bot.session)
            log.info("✅ GameCommands cog loaded")

    # ── /gen ─────────────────────────────────────────────────────────────────

    @app_commands.command(name="gen", description="Search and download a Steam game")
    @app_commands.describe(query="Game title or App ID")
    @app_commands.autocomplete(query=autocomplete_games)
    @app_commands.checks.cooldown(1, 5.0, key=lambda i: i.user.id)
    async def gen(self, interaction: discord.Interaction, query: str):
        await interaction.response.defer()

        cc        = resolve_country_code(interaction)
        target_id = query.strip()

        if not target_id.isdigit():
            results = await self.steam_api.search_games(urllib.parse.quote(target_id), limit=1)
            if not results:
                await interaction.edit_original_response(embed=self._embed_not_found(target_id))
                return
            target_id = results[0]["id"]

        steam_data = await self.steam_api.get_app_details(target_id, cc=cc)
        if not steam_data:
            await interaction.edit_original_response(embed=self._embed_not_found(query))
            return

        game_info = self.steam_api.extract_game_info(steam_data)
        db_entry  = self.db.get_game(target_id)
        has_file  = db_entry.get("file", False) if db_entry else False

        dl = await self._find_download(target_id)

        await interaction.edit_original_response(
            embed=self._embed_game_card(game_info, db_entry, dl)
        )

        if dl["available"]:
            self.db.mark_as_starred(target_id, game_info["name"])
            self.db.save()
            await self._send_download_followup(interaction, game_info, dl)
        else:
            await interaction.followup.send(
                embed=self._embed_unavailable(game_info["name"]),
                ephemeral=True,
            )

    # ── /search ───────────────────────────────────────────────────────────────

    @app_commands.command(name="search", description="Search for a game in the local database")
    @app_commands.describe(query="Game title to look for")
    async def search(self, interaction: discord.Interaction, query: str):
        await interaction.response.defer(ephemeral=True)

        results = self.db.search_games(query, limit=10)
        if not results:
            embed = discord.Embed(
                title="🔍  No Results Found",
                description=(
                    f"No games found for **{query}** in the local database.\n"
                    "Try `/gen` to search Steam directly."
                ),
                color=COLOR_ERROR,
            )
            await interaction.followup.send(embed=embed, ephemeral=True)
            return

        embed = discord.Embed(
            title=f"🔍  Search Results",
            description=f"Found **{len(results)}** result(s) for `{query}`",
            color=COLOR_INFO,
        )
        embed.set_author(name="triadbot  •  Local Database")

        for game in results:
            name   = game.get("name", "Unknown")
            appid  = game["id"]
            status = "📦  File Available" if game.get("file") else "🔹  Registered"
            embed.add_field(
                name=name,
                value=f"`{appid}`  •  {status}",
                inline=False,
            )

        embed.set_footer(text="Use /gen <appid> to download an available game")
        await interaction.followup.send(embed=embed, ephemeral=True)

    # ── /info ─────────────────────────────────────────────────────────────────

    @app_commands.command(name="info", description="View full details for a Steam game")
    @app_commands.describe(appid="Steam App ID (numbers only)")
    async def info(self, interaction: discord.Interaction, appid: str):
        await interaction.response.defer(ephemeral=True)

        if not appid.isdigit():
            await interaction.followup.send(embed=self._embed_not_found(appid), ephemeral=True)
            return

        cc         = resolve_country_code(interaction)
        steam_data = await self.steam_api.get_app_details(appid, cc=cc)
        if not steam_data:
            await interaction.followup.send(embed=self._embed_not_found(appid), ephemeral=True)
            return

        game_info  = self.steam_api.extract_game_info(steam_data)
        db_entry   = self.db.get_game(appid)
        protection = extract_protection_type(game_info.get("drm_notice"))

        embed = discord.Embed(
            title=game_info["name"],
            description=truncate_text(game_info.get("short_description") or "—", 350),
            color=COLOR_INFO,
            url=f"https://store.steampowered.com/app/{appid}",
        )
        embed.set_author(name="triadbot  •  Steam Store Info")
        embed.add_field(name="🆔  App ID",     value=f"`{appid}`",             inline=True)
        embed.add_field(name="🎯  Type",       value=game_info["type"],         inline=True)
        embed.add_field(name="💰  Price",      value=game_info["price"],        inline=True)
        embed.add_field(name="🏷️  Genre",     value=game_info["genres"],       inline=True)
        embed.add_field(name="📅  Release",    value=game_info["release_date"], inline=True)
        embed.add_field(name="🛡️  DRM",       value=protection,                inline=True)
        embed.add_field(name="👥  Developer",  value=game_info["developers"],   inline=True)
        embed.add_field(name="🏢  Publisher",  value=game_info["publishers"],   inline=True)

        if db_entry:
            db_val = "✅  In Database"
            if db_entry.get("file"):
                db_val += "  •  📦 File Ready"
            embed.add_field(name="📊  DB Status", value=db_val, inline=True)

        if game_info.get("header_image"):
            embed.set_image(url=game_info["header_image"])

        embed.set_footer(text=f"triadbot  •  App ID: {appid}")
        await interaction.followup.send(embed=embed, ephemeral=True)

    # ── Error handler ─────────────────────────────────────────────────────────

    @gen.error
    async def gen_error(self, interaction: discord.Interaction, error):
        if isinstance(error, app_commands.CommandOnCooldown):
            embed = discord.Embed(
                title="⏳  Cooldown Active",
                description=f"Please wait **{error.retry_after:.1f}s** before using `/gen` again.",
                color=COLOR_WARNING,
            )
            if interaction.response.is_done():
                await interaction.followup.send(embed=embed, ephemeral=True)
            else:
                await interaction.response.send_message(embed=embed, ephemeral=True)
        else:
            log.error(f"/gen error: {error}", exc_info=error)

    # ── Internal helpers ──────────────────────────────────────────────────────

    async def _find_download(self, appid: str) -> Dict:
        """
        Coba semua pola key R2 secara berurutan.
        Simpan 'r2_key' yang berhasil agar JWT redirect di bot.py pakai key yang sama.
        """
        if not R2_BASE_URL and not _PRESIGN_ENABLED:
            return self._dl_empty(appid)

        filename = f"[{appid}].zip"

        for pattern in R2_KEY_PATTERNS:
            key = pattern.format(appid=appid)

            presigned = await generate_presigned_url(key)
            check_url = presigned or f"{R2_BASE_URL.rstrip('/')}/{key}"

            if not check_url:
                continue

            try:
                async with self.bot.session.head(
                    check_url,
                    timeout=aiohttp.ClientTimeout(total=8),
                    allow_redirects=True,
                ) as resp:
                    if resp.status == 200:
                        size = int(resp.headers.get("Content-Length", 0))

                        # Bangun URL final — JWT jika WEB_URL tersedia, fallback ke presigned/public
                        final_url = check_url
                        if WEB_URL:
                            try:
                                payload = {
                                    "app_id": str(appid),
                                    "r2_key": key,          # ← simpan key yang BERHASIL
                                    "exp":    int(time.time()) + LINK_EXPIRE_SECONDS,
                                }
                                token     = jwt.encode(payload, JWT_SECRET, algorithm="HS256")
                                final_url = f"{WEB_URL}/download/{appid}?token={token}"
                            except Exception as jwt_err:
                                log.warning(f"JWT encode failed: {jwt_err}")

                        return {
                            "available":  True,
                            "url":        final_url,
                            "size_bytes": size,
                            "filename":   filename,
                            "r2_key":     key,
                            "expires_in": LINK_EXPIRE_SECONDS,
                        }
            except Exception as e:
                log.debug(f"R2 check failed [{key}]: {e}")
                continue

        log.warning(f"❌ File AppID {appid} tidak ditemukan di R2")
        return self._dl_empty(appid)

    @staticmethod
    def _dl_empty(appid: str) -> Dict:
        return {
            "available":  False,
            "url":        None,
            "size_bytes": 0,
            "filename":   f"[{appid}].zip",
            "r2_key":     None,
            "expires_in": None,
        }

    # ── Embed builders ────────────────────────────────────────────────────────

    def _embed_game_card(self, game_info: Dict, db_entry: Optional[Dict], dl: Dict) -> discord.Embed:
        has_file  = db_entry.get("file", False) if db_entry else False

        if dl["available"]:
            color  = COLOR_DOWNLOAD
            status = "🟢  **Download Available**"
        elif has_file:
            color  = COLOR_SUCCESS
            status = "🟡  Verified (cached)"
        else:
            color  = COLOR_WARNING
            status = "🔴  Not Available"

        protection = extract_protection_type(game_info.get("drm_notice"))

        embed = discord.Embed(color=color)
        embed.set_author(
            name="triadbot  •  Steam Database",
            icon_url="https://store.steampowered.com/favicon.ico",
        )
        embed.add_field(
            name=f"🎮  {game_info['name']}",
            value=truncate_text(game_info.get("short_description") or "—", 220),
            inline=False,
        )
        embed.add_field(name="🆔  App ID",      value=f"`{game_info['appid']}`",  inline=True)
        embed.add_field(name="💰  Price",       value=game_info["price"],          inline=True)
        embed.add_field(name="🏷️  Genre",      value=game_info["genres"],         inline=True)
        embed.add_field(name="🛡️  DRM",        value=protection,                  inline=True)
        embed.add_field(name="📅  Release",    value=game_info["release_date"],    inline=True)
        embed.add_field(name="📊  Status",     value=status,                       inline=True)
        embed.add_field(
            name="👥  Developer",
            value=f"{game_info['developers']}  •  *{game_info['publishers']}*",
            inline=False,
        )

        if dl["available"] and dl["size_bytes"]:
            embed.add_field(name="📦  File Size", value=format_size(dl["size_bytes"]), inline=True)

        if game_info.get("header_image"):
            embed.set_image(url=game_info["header_image"])

        embed.set_footer(text="triadbot v9.1  •  Data from Steam Store")
        return embed

    async def _send_download_followup(
        self, interaction: discord.Interaction, game_info: Dict, dl: Dict
    ):
        """
        Kirim link download sebagai ephemeral followup — hanya terlihat oleh user.
        """
        size_str = format_size(dl["size_bytes"]) if dl["size_bytes"] else "—"

        expires_in = dl.get("expires_in")
        if expires_in:
            mins = expires_in // 60
            expiry_text = f"{mins // 60}h {mins % 60:02d}m" if mins >= 60 else f"{mins} minutes"
            expiry_label = f"⏱️  Expires in **{expiry_text}**"
        else:
            expiry_label = "⏱️  No expiry (public link)"

        embed = discord.Embed(
            title=f"📦  {game_info['name']}",
            description=(
                "Your download is ready.\n"
                "**This message is only visible to you** — it won't appear in the channel."
            ),
            color=COLOR_DOWNLOAD,
        )
        embed.set_author(name="triadbot  •  Secure File Delivery")
        embed.add_field(name="📁  Filename",    value=f"`{dl['filename']}`", inline=True)
        embed.add_field(name="💾  Size",        value=size_str,              inline=True)
        embed.add_field(name=expiry_label,      value="Download before it expires.",   inline=False)
        embed.add_field(
            name="⚠️  Reminder",
            value=(
                "• Do **not** share this link\n"
                "• Extract the `.zip` after downloading\n"
                "• Link will rotate on next request"
            ),
            inline=False,
        )
        if game_info.get("header_image"):
            embed.set_thumbnail(url=game_info["header_image"])
        embed.set_footer(text="triadbot  •  Powered by Cloudflare R2")

        view = discord.ui.View(timeout=None)
        view.add_item(
            discord.ui.Button(
                label="⬇️  Download Now",
                url=dl["url"],
                style=discord.ButtonStyle.link,
            )
        )
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)

    def _embed_not_found(self, query: str) -> discord.Embed:
        embed = discord.Embed(
            title="❌  Game Not Found",
            description=(
                f"No results found for **{query}**.\n\n"
                "• Double-check the game title or App ID\n"
                "• Try searching with fewer words\n"
                "• Use `/search` to browse the local database"
            ),
            color=COLOR_ERROR,
        )
        embed.set_footer(text="triadbot  •  Steam Database")
        return embed

    def _embed_unavailable(self, game_name: str) -> discord.Embed:
        embed = discord.Embed(
            title="📭  Download Not Available",
            description=(
                f"**{game_name}** is not yet available for download.\n\n"
                "Possible reasons:\n"
                "• Not yet uploaded to Cloudflare R2\n"
                "• DRM not supported (e.g. Denuvo)\n"
                "• File is currently being processed\n\n"
                "Try again later or contact an admin."
            ),
            color=COLOR_ERROR,
        )
        embed.set_footer(text="triadbot  •  This message is only visible to you")
        return embed


async def setup(bot):
    await bot.add_cog(GameCommands(bot))
