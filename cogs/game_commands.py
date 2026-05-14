"""
Game Commands Cog
/gen    - Search & download a game
/search - Search in local database
/info   - Full game details from Steam
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
    DEFAULT_CC, LINK_EXPIRE_SECONDS, R2_BASE_URL,
)
from utils.helpers import (
    clean_search_string, extract_protection_type, format_size,
    make_safe_filename, truncate_text,
)
from utils.r2_presign import generate_presigned_url, _PRESIGN_ENABLED
from utils.steam_api import SteamAPI, locale_to_country_code

log = logging.getLogger(__name__)


def resolve_country_code(interaction: discord.Interaction) -> str:
    """Pick the best Steam country code for this interaction."""
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

async def autocomplete_games(
    interaction: discord.Interaction,
    current: str,
) -> List[app_commands.Choice[str]]:
    """Instant autocomplete from local database."""
    db = interaction.client.db
    results: List[app_commands.Choice[str]] = []

    q = (current or "").strip().lower()

    # Fallback if Railway database is completely empty
    if not db.game_db and q:
        results.append(app_commands.Choice(name="⚠️ Database is empty. Press Enter to search Steam.", value=q))
        return results

    if not q:
        top = [g for g in db.game_db if g.get("file") and g.get("name")]
        for g in top[:25]:
            results.append(app_commands.Choice(name=f"⭐ {g['name']}"[:100], value=str(g["id"])))
        return results

    q_clean = clean_search_string(q)
    with_file, without_file = [], []

    for game in db.game_db:
        name: str = game.get("name", "")
        appid: str = str(game.get("id", ""))
        if not name:
            continue

        name_lower = name.lower()
        if q in name_lower or q_clean in clean_search_string(name_lower) or q in appid:
            if game.get("file"):
                with_file.append(game)
            else:
                without_file.append(game)

    for g in with_file[:15]:
        results.append(app_commands.Choice(name=f"⭐ {g['name']}"[:100], value=str(g["id"])))
        
    remaining = 25 - len(results)
    for g in without_file[:remaining]:
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

    @app_commands.command(name="gen", description="Search and download a Steam game")
    @app_commands.describe(query="Game title OR App ID (numbers only)")
    @app_commands.autocomplete(query=autocomplete_games)
    @app_commands.checks.cooldown(1, 5.0, key=lambda i: i.user.id)
    async def gen(self, interaction: discord.Interaction, query: str):
        await interaction.response.defer()

        cc = resolve_country_code(interaction)
        target_id = query.strip()

        # If user typed a title instead of App ID
        if not target_id.isdigit():
            safe_q = urllib.parse.quote(target_id)
            search_results = await self.steam_api.search_games(safe_q, limit=1)

            if not search_results:
                embed = discord.Embed(
                    title="❌ Game Not Found",
                    description=f"No results for **`{target_id}`** on Steam.\nTry searching with a different spelling.",
                    color=COLOR_ERROR,
                )
                await interaction.edit_original_response(embed=embed)
                return

            target_id = search_results[0]["id"]

        # Fetch Steam details
        steam_data = await self.steam_api.get_app_details(target_id, cc=cc)
        if not steam_data:
            embed = discord.Embed(
                title="❌ Game Not Found",
                description=f"No game with App ID **`{target_id}`** was found on Steam.",
                color=COLOR_ERROR,
            )
            embed.set_footer(text="You can look up App IDs at store.steampowered.com")
            await interaction.edit_original_response(embed=embed)
            return

        game_info = self.steam_api.extract_game_info(steam_data)
        db_entry = self.db.get_game(target_id)
        found_in_db = db_entry is not None
        has_file = db_entry.get("file", False) if found_in_db else False

        dl = await self._find_download(target_id, game_info["name"])

        await self._send_game_info(interaction, game_info, found_in_db, has_file, dl)

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

    @app_commands.command(name="search", description="Search for a game in the local database")
    @app_commands.describe(query="Game title to look for")
    async def search(self, interaction: discord.Interaction, query: str):
        await interaction.response.defer(ephemeral=True)

        results = self.db.search_games(query, limit=10)
        if not results:
            await interaction.followup.send(
                embed=discord.Embed(
                    title="🔍 No Results",
                    description=f"No games matching **`{query}`** were found in the database.",
                    color=COLOR_ERROR,
                ),
                ephemeral=True,
            )
            return

        embed = discord.Embed(
            title=f"🔍 Results for: `{query}`",
            description=f"Found **{len(results)}** game(s)",
            color=COLOR_SUCCESS,
        )
        for game in results:
            icon = "⭐" if game.get("file") else "📁"
            status = "File Available" if game.get("file") else "Registered"
            embed.add_field(
                name=f"{icon} {game.get('name', 'Unknown')}",
                value=f"AppID: `{game['id']}` • {status}",
                inline=False,
            )
        embed.set_footer(text="Use /gen <game name> to download")
        await interaction.followup.send(embed=embed, ephemeral=True)

    # ── /info ─────────────────────────────────────────────────────────────────

    @app_commands.command(name="info", description="View full details for a Steam game")
    @app_commands.describe(appid="Steam App ID (numbers only)")
    async def info(self, interaction: discord.Interaction, appid: str):
        await interaction.response.defer(ephemeral=True)

        if not appid.isdigit():
            await interaction.followup.send(
                embed=discord.Embed(
                    title="❌ Invalid App ID",
                    description=f"`{appid}` is not a valid numeric App ID.",
                    color=COLOR_ERROR,
                ),
                ephemeral=True,
            )
            return

        cc = resolve_country_code(interaction)
        steam_data = await self.steam_api.get_app_details(appid, cc=cc)
        
        if not steam_data:
            await interaction.followup.send(
                embed=discord.Embed(
                    title="❌ Not Found",
                    description=f"No game with App ID `{appid}` exists on Steam.",
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
        embed.add_field(name="🆔 App ID",      value=f"`{appid}`",              inline=True)
        embed.add_field(name="🎯 Type",        value=game_info["type"],          inline=True)
        embed.add_field(name="💰 Price",       value=game_info["price"],         inline=True)
        embed.add_field(name="🏷️ Genre",       value=game_info["genres"],        inline=True)
        embed.add_field(name="📅 Release",     value=game_info["release_date"],  inline=True)
        embed.add_field(name="🛡️ DRM",         value=protection,                 inline=True)
        embed.add_field(name="👥 Developer",   value=game_info["developers"],    inline=False)
        embed.add_field(name="🏢 Publisher",   value=game_info["publishers"],    inline=False)
        
        if db_game:
            db_status = "✅ In Database"
            if db_game.get("file"):
                db_status += " • ⭐ File Available"
            embed.add_field(name="📊 DB Status", value=db_status, inline=False)
            
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
                description=f"Please wait **{error.retry_after:.1f}s** before requesting again.",
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
        """Check whether a file exists in R2 and return the best available URL."""
        filename = f"{make_safe_filename(game_name)} [{appid}].zip"

        if not R2_BASE_URL:
            return {"available": False, "url": None, "size_bytes": 0, "filename": filename, "expires_in": None}

        check_url = f"{R2_BASE_URL}/Database/{appid}.zip"
        final_url = check_url
        expires_in = None

        # FIX: If Presign is enabled (private bucket), generate the signed URL FIRST to bypass 403 Forbidden!
        if _PRESIGN_ENABLED:
            signed_url = await generate_presigned_url(appid)
            if signed_url:
                check_url = signed_url
                final_url = signed_url
                expires_in = LINK_EXPIRE_SECONDS
            else:
                log.warning(f"Presign failed for {appid}, falling back to public URL")

        try:
            async with self.bot.session.head(
                check_url, timeout=aiohttp.ClientTimeout(total=5), allow_redirects=True
            ) as resp:
                if resp.status != 200:
                    return {"available": False, "url": None, "size_bytes": 0, "filename": filename, "expires_in": None}
                size = int(resp.headers.get("Content-Length", 0))
                return {"available": True, "url": final_url, "size_bytes": size, "filename": filename, "expires_in": expires_in}
        except Exception as e:
            log.error(f"R2 HEAD error for {appid}: {e}")
            return {"available": False, "url": None, "size_bytes": 0, "filename": filename, "expires_in": None}

    async def _send_game_info(self, interaction, game_info, found_in_db, has_file, dl):
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

        embed.add_field(name="🆔 App ID",      value=f"`{game_info['appid']}`", inline=True)
        embed.add_field(name="🛡️ Protection",  value=protection,                inline=True)
        embed.add_field(name="🏷️ Genre",       value=game_info["genres"],       inline=True)

        if dl["available"]:   sv = "✅ **Download Available**"
        elif has_file:        sv = "✅ Verified"
        elif found_in_db:     sv = "📁 Scanned"
        else:                 sv = "🌐 Found on Steam"

        embed.add_field(name="📊 Status",  value=sv,                           inline=True)
        embed.add_field(name="📅 Release", value=game_info["release_date"],    inline=True)
        embed.add_field(name="💰 Price",   value=game_info["price"],           inline=True)
        embed.add_field(
            name="👥 Developer",
            value=f"{game_info['developers']} • *{game_info['publishers']}*",
            inline=False,
        )

        if dl["available"]:
            size_str = format_size(dl["size_bytes"]) if dl["size_bytes"] else "—"
            embed.add_field(
                name="⬇️ Download",
                value=f"Available • `{size_str}`\n🔒 *Link sent privately*",
                inline=False,
            )
        else:
            embed.add_field(name="⬇️ Download", value="❌ File not available", inline=False)

        if game_info.get("header_image"):
            embed.set_image(url=game_info["header_image"])

        embed.set_footer(text="SteamTools • Download link is only visible to you")
        await interaction.edit_original_response(embed=embed)

    async def _send_download_embed(self, interaction, game_info, dl):
        size_str = format_size(dl["size_bytes"]) if dl["size_bytes"] else "Unknown"

        expires_in = dl.get("expires_in")
        if expires_in:
            mins = expires_in // 60
            if mins >= 60:
                expiry_label = f"⏱️ Expires in **{mins // 60}h {mins % 60:02d}m**"
            else:
                expiry_label = f"⏱️ Expires in **{mins} minutes**"
        else:
            expiry_label = "⏱️ No expiry (public link)"

        embed = discord.Embed(
            title="🔒 Your Private Download Link",
            description=(
                f"**{game_info['name']}**\n"
                "This message is **only visible to you** and will not appear in the channel."
            ),
            color=COLOR_DOWNLOAD,
        )
        embed.add_field(name="📁 File",   value=f"`{dl['filename']}`", inline=True)
        embed.add_field(name="💾 Size",   value=size_str,               inline=True)
        embed.add_field(name="⚠️ Notice",
            value=(
                "• Do not share this link\n"
                "• Link may expire or change\n"
                "• Extract the archive after download"
            ),
            inline=False,
        )
        embed.add_field(name=expiry_label, value="Download before the link expires.", inline=False)
        
        if game_info.get("header_image"):
            embed.set_thumbnail(url=game_info["header_image"])
            
        embed.set_footer(text="SteamTools • This message is for you only")

        view = discord.ui.View(timeout=None)
        view.add_item(
            discord.ui.Button(
                label="⬇️  Download Now",
                url=dl["url"],
                style=discord.ButtonStyle.link,
            )
        )
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)

    def _unavailable_embed(self, game_name: str) -> discord.Embed:
        embed = discord.Embed(
            title="❌ Download Not Available",
            description=(
                f"**{game_name}** is not yet available for download.\n\n"
                "Possible reasons:\n"
                "• Not yet uploaded to Cloudflare R2\n"
                "• DRM not supported (e.g. Denuvo)\n"
                "• File is currently processing\n\n"
                "Try again later or contact an administrator."
            ),
            color=COLOR_ERROR,
        )
        embed.set_footer(text="This message is only visible to you")
        return embed

async def setup(bot):
    await bot.add_cog(GameCommands(bot))