from pathlib import Path
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
    ADMIN_IDS, ADMIN_ROLE_IDS, ADMIN_ROLE_NAMES, ALERT_ON_LIMIT_HIT,
    COLOR_DOWNLOAD, COLOR_ERROR, COLOR_INFO, COLOR_SUCCESS, COLOR_WARNING,
    BOOSTER_ROLE_IDS, BOOSTER_ROLE_NAMES, DEFAULT_CC, DONOR_ROLE_IDS, DONOR_ROLE_NAMES, GEN_DAILY_LIMIT,
    LINK_EXPIRE_SECONDS, R2_BASE_URL, WEB_URL, JWT_SECRET,
)
from utils.helpers import (
    clean_search_string, extract_protection_type, format_size, has_any_role,
    is_admin_interaction, is_valid_appid, truncate_text,
)
from utils.gen_limits import DailyGenLimiter
from utils.r2_keys import build_r2_key_candidates
from utils.r2_presign import generate_presigned_url, _PRESIGN_ENABLED
from utils.steam_api import SteamAPI, locale_to_country_code

log = logging.getLogger(__name__)

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

    q_raw   = (current or "").strip()

    starred, normal = [], []

    # SQLite-backed title autocomplete only. Keep suggestions fast and local.
    try:
        if hasattr(db, "autocomplete_titles"):
            all_results = await asyncio.to_thread(db.autocomplete_titles, q_raw, limit=25)
        else:
            all_results = await asyncio.to_thread(db.search_games, q_raw or "", limit=25)
    except Exception:
        all_results = []

    for game in all_results:
        appid = str(game.get("appid") or game.get("id") or "")
        name  = game.get("name") or ""
        if not appid or not name:
            continue
        (starred if game.get("file") else normal).append(game)

    results:   List[app_commands.Choice] = []
    found_ids: set = set()

    for game in starred[:15]:
        appid = str(game.get("appid") or game.get("id") or "")
        name  = game.get("name", "")[:100]
        if appid and appid not in found_ids:
            results.append(app_commands.Choice(name=name, value=appid))
            found_ids.add(appid)

    for game in normal[: 25 - len(results)]:
        appid = str(game.get("appid") or game.get("id") or "")
        name  = game.get("name", "")[:100]
        if appid and appid not in found_ids:
            results.append(app_commands.Choice(name=name, value=appid))
            found_ids.add(appid)

    return results


# ── Cog ───────────────────────────────────────────────────────────────────────

class GameCommands(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.db  = bot.db
        self.steam_api: Optional[SteamAPI] = None
        self.gen_limiter = getattr(bot, "gen_limiter", None)
        if self.gen_limiter is None:
            self.gen_limiter = DailyGenLimiter()
            bot.gen_limiter = self.gen_limiter

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
        is_limit_exempt = self._is_gen_limit_exempt(interaction)
        limit_status = None

        if not is_limit_exempt:
            allowed, _, _ = self.gen_limiter.check(interaction.user.id)
            if not allowed:
                if ALERT_ON_LIMIT_HIT:
                    await self._alert_limit_hit(interaction)
                await interaction.response.send_message(
                    embed=self._embed_gen_limited(interaction),
                    ephemeral=True,
                )
                return

        try:
            await interaction.response.defer()
        except discord.NotFound:
            # Interaction already expired before we could defer (e.g. after bot restart)
            log.warning(f"/gen interaction expired before defer for user {interaction.user.id}")
            return

        async def safe_edit(embed):
            """Edit original response, silently dropping stale-interaction errors."""
            try:
                await interaction.edit_original_response(embed=embed)
            except discord.NotFound:
                log.debug("/gen: interaction token expired, dropping edit")
            except Exception as e:
                log.error(f"/gen safe_edit error: {e}")

        cc        = resolve_country_code(interaction)
        target_id = query.strip()

        if not target_id.isdigit():
            results = await self.steam_api.search_games(urllib.parse.quote(target_id), limit=1)
            if not results:
                await safe_edit(self._embed_not_found(target_id))
                return
            target_id = results[0]["id"]

        if not is_valid_appid(target_id):
            await safe_edit(self._embed_not_found(query))
            return

        steam_data = await self.steam_api.get_app_details(target_id, cc=cc)
        if not steam_data:
            # Retry once with a short delay before giving up
            await asyncio.sleep(1.5)
            steam_data = await self.steam_api.get_app_details(target_id, cc=cc)
        if not steam_data:
            await safe_edit(self._embed_steam_unavailable(query))
            return

        game_info = self.steam_api.extract_game_info(steam_data)
        db_entry  = await asyncio.to_thread(self.db.get_game, target_id)
        has_file  = db_entry.get("file", False) if db_entry else False

        dl = await self._find_download(target_id, game_info["name"])

        await safe_edit(self._embed_game_card(game_info, db_entry, dl))

        if dl["available"]:
            await asyncio.to_thread(self.db.mark_as_starred, target_id, game_info["name"])
            await asyncio.to_thread(self.db.save)
            if not is_limit_exempt:
                limit_status = self.gen_limiter.consume(interaction.user.id)
            try:
                await self._send_download_followup(interaction, game_info, dl)
                if limit_status:
                    used, remaining = limit_status
                    await self._send_gen_limit_usage_followup(interaction, used, remaining)
            except discord.NotFound:
                log.debug("/gen: interaction expired before followup could be sent")
            except Exception as e:
                log.error(f"/gen followup error: {e}")
        else:
            # File not in R2 — trigger a background priority sync so the next
            # /gen attempt for this game is more likely to succeed.
            self._trigger_priority_sync(target_id, game_info["name"])
            try:
                await interaction.followup.send(
                    embed=self._embed_unavailable(game_info["name"]),
                    ephemeral=True,
                )
            except discord.NotFound:
                log.debug("/gen: interaction expired before unavailable followup")


    # ── /request ─────────────────────────────────────────────────────────────

    @app_commands.command(name="request", description="Request a game to be prioritized for Cloudflare R2 mirroring")
    @app_commands.describe(query="Game title or App ID")
    @app_commands.autocomplete(query=autocomplete_games)
    async def request(self, interaction: discord.Interaction, query: str):
        from pathlib import Path
        import json
        import time

        try:
            await interaction.response.send_message("🔍 Processing your request...", ephemeral=True)
        except:
            pass

        target_id = query.strip()
        game_name = query

        try:
            if not target_id.isdigit():
                results = await self.steam_api.search_games(query, limit=1)
                if not results:
                    await interaction.edit_original_response(content="❌ Game not found on Steam.")
                    return
                target_id = results[0]["id"]
                game_name = results[0]["name"]

            await asyncio.to_thread(self.db.add_game, target_id, game_name)

            from config import DATA_DIR
            priority_file = DATA_DIR / "priority_requests.json"
            priority_data = {}
            if priority_file.exists():
                try: priority_data = json.loads(priority_file.read_text())
                except: pass

            priority_data[target_id] = {"name": game_name, "requested_by": interaction.user.id, "time": time.time()}
            priority_file.parent.mkdir(parents=True, exist_ok=True)
            priority_file.write_text(json.dumps(priority_data, indent=2))
            # Trigger instant sync for this game
            opendir_cog = self.bot.get_cog("OpenDirSync")  # class name in cogs/opendir_sync.py
            if opendir_cog:
                # Run sync in background so it doesn't block the response
                asyncio.create_task(opendir_cog.run_sync_once(priority_appid=target_id))
                log.info(f"⚡ Instant Priority Sync triggered for {game_name} ({target_id})")


            embed = discord.Embed(
                title="✅ Request Received",
                description=f"**{game_name} ({target_id})** has been added to the priority sync queue and an instant sync has been triggered! ⚡",
                color=0x00ff00
            )
            await interaction.edit_original_response(content=None, embed=embed)
        except Exception as e:
            try: await interaction.edit_original_response(content=f"❌ An error occurred: {e}")
            except: pass

    # ── /search ───────────────────────────────────────────────────────────────

    @app_commands.command(name="search", description="Search for a game in the local database")
    @app_commands.describe(query="Game title to look for")
    async def search(self, interaction: discord.Interaction, query: str):
        await interaction.response.defer(ephemeral=True)

        results = await asyncio.to_thread(self.db.search_games, query, limit=10)
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
            appid  = str(game.get("appid") or game.get("id") or "?")
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

        if not is_valid_appid(appid):
            await interaction.followup.send(embed=self._embed_not_found(appid), ephemeral=True)
            return

        cc         = resolve_country_code(interaction)
        steam_data = await self.steam_api.get_app_details(appid, cc=cc)
        if not steam_data:
            await interaction.followup.send(embed=self._embed_not_found(appid), ephemeral=True)
            return

        game_info  = self.steam_api.extract_game_info(steam_data)
        db_entry   = await asyncio.to_thread(self.db.get_game, appid)
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
        # Unwrap CommandInvokeError to get the actual cause
        cause = getattr(error, "original", error)

        # 40060 = already acknowledged, 10062 = unknown/expired interaction.
        # Both mean the user already received a response — no need to alert or re-respond.
        if isinstance(cause, discord.HTTPException) and cause.code in (40060, 10062):
            log.debug(f"/gen stale interaction ignored (code {cause.code}) for user {interaction.user.id}")
            return

        if isinstance(error, app_commands.CommandOnCooldown):
            embed = discord.Embed(
                title="⏳  Cooldown Active",
                description=f"Please wait **{error.retry_after:.1f}s** before using `/gen` again.",
                color=COLOR_WARNING,
            )
            try:
                if interaction.response.is_done():
                    await interaction.followup.send(embed=embed, ephemeral=True)
                else:
                    await interaction.response.send_message(embed=embed, ephemeral=True)
            except Exception:
                pass
        else:
            log.error(f"/gen error: {error}", exc_info=error)
            notifier = getattr(self.bot, "notify_admins", None)
            if notifier:
                await notifier(
                    "/gen command error",
                    "A `/gen` request failed unexpectedly.",
                    level="error",
                    fields={
                        "User": f"{interaction.user} ({interaction.user.id})",
                        "Guild": str(interaction.guild or "DM"),
                        "Error": repr(error)[:1000],
                    },
                    key=f"gen-error-{type(error).__name__}",
                )
            embed = discord.Embed(
                title="Download request failed",
                description="Something went wrong while processing `/gen`. The admin has been notified.",
                color=COLOR_ERROR,
            )
            try:
                if interaction.response.is_done():
                    await interaction.followup.send(embed=embed, ephemeral=True)
                else:
                    await interaction.response.send_message(embed=embed, ephemeral=True)
            except Exception:
                pass

    # ── Internal helpers ──────────────────────────────────────────────────────

    async def _find_download(self, appid: str, game_name: Optional[str] = None) -> Dict:
        """
        Coba semua pola key R2 secara berurutan.
        Simpan 'r2_key' yang berhasil agar JWT redirect di bot.py pakai key yang sama.
        """
        if not R2_BASE_URL and not _PRESIGN_ENABLED:
            return self._dl_empty(appid)

        for key in build_r2_key_candidates(appid, game_name):
            presigned = await generate_presigned_url(key)
            check_url = presigned or f"{R2_BASE_URL.rstrip('/')}/{key}"

            if not check_url:
                continue

            try:
                async with self.bot.session.get(
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
                                notifier = getattr(self.bot, "notify_admins", None)
                                if notifier:
                                    await notifier(
                                        "JWT download link error",
                                        "The bot could not create a JWT download link.",
                                        level="error",
                                        fields={
                                            "App ID": str(appid),
                                            "R2 key": key,
                                            "Error": repr(jwt_err)[:1000],
                                        },
                                        key="jwt-download-link-error",
                                    )

                        return {
                            "available":  True,
                            "url":        final_url,
                            "size_bytes": size,
                            "filename":   key.rsplit("/", 1)[-1],
                            "r2_key":     key,
                            "expires_in": LINK_EXPIRE_SECONDS,
                        }
            except Exception as e:
                log.debug(f"R2 check failed [{key}]: {e}")
                continue

        log.warning(f"❌ File AppID {appid} tidak ditemukan di R2")
        return self._dl_empty(appid)

    def _trigger_priority_sync(self, appid: str, game_name: str) -> None:
        """
        Fire-and-forget: ask OpenDirSync to do a targeted fetch for *appid*.

        Runs as a background task so it never blocks the /gen response.
        Safe to call even when OpenDirSync is disabled or not loaded.
        """
        opendir_cog = self.bot.get_cog("OpenDirSync")
        if opendir_cog is None:
            return
        async def _run_priority_sync() -> None:
            # Make sure game is in DB so targeted sync can look it up.
            await asyncio.to_thread(self.db.add_game, appid, game_name)
            await opendir_cog.run_sync_once(priority_appid=appid)

        log.info("⚡ /gen triggered background priority sync for %s (%s)", game_name, appid)
        asyncio.create_task(
            _run_priority_sync(),
            name=f"opendir-priority-{appid}",
        )

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

    async def _send_gen_limit_usage_followup(
        self, interaction: discord.Interaction, used: int, remaining: int
    ):
        reset_ts = int(self.gen_limiter.reset_at_utc().timestamp())
        locale = str(getattr(interaction, "locale", "")).lower()
        guild_locale = str(getattr(interaction, "guild_locale", "")).lower()
        is_id = locale.startswith("id") or guild_locale.startswith("id")

        if is_id:
            title = "Sisa limit /gen"
            description = (
                f"Terpakai hari ini: **{used}/{GEN_DAILY_LIMIT}**\n"
                f"Sisa request: **{remaining}**\n"
                f"Reset global: **00:00 UTC** - <t:{reset_ts}:R>"
            )
        else:
            title = "Daily /gen usage"
            description = (
                f"Used today: **{used}/{GEN_DAILY_LIMIT}**\n"
                f"Remaining requests: **{remaining}**\n"
                f"Global reset: **00:00 UTC** - <t:{reset_ts}:R>"
            )

        embed = discord.Embed(title=title, description=description, color=COLOR_INFO)
        await interaction.followup.send(embed=embed, ephemeral=True)

    async def _alert_limit_hit(self, interaction: discord.Interaction):
        notifier = getattr(self.bot, "notify_admins", None)
        if not notifier:
            return
        await notifier(
            "User hit daily /gen limit",
            "A regular user tried to use `/gen` after reaching the daily limit.",
            level="warning",
            fields={
                "User": f"{interaction.user} ({interaction.user.id})",
                "Guild": str(interaction.guild or "DM"),
                "Limit": str(GEN_DAILY_LIMIT),
                "Reset": self.gen_limiter.reset_at_utc().isoformat(),
            },
            key=f"gen-limit-{interaction.user.id}-{self.gen_limiter.usage_date}",
        )

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

    def _embed_steam_unavailable(self, query: str) -> discord.Embed:
        embed = discord.Embed(
            title="Steam Data Unavailable",
            description=(
                f"I could not fetch Steam data for **{query}** right now.\n\n"
                "Please try again in a moment. If this keeps happening, the admin will need to check Steam/API logs."
            ),
            color=COLOR_WARNING,
        )
        embed.set_footer(text="triadbot  -  Steam Store")
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

    def _is_gen_limit_exempt(self, interaction: discord.Interaction) -> bool:
        if is_admin_interaction(interaction, ADMIN_IDS, ADMIN_ROLE_IDS, ADMIN_ROLE_NAMES):
            return True
        return (
            has_any_role(interaction.user, DONOR_ROLE_IDS, DONOR_ROLE_NAMES)
            or has_any_role(interaction.user, BOOSTER_ROLE_IDS, BOOSTER_ROLE_NAMES)
        )

    def _embed_gen_limited(self, interaction: discord.Interaction) -> discord.Embed:
        reset_ts = int(self.gen_limiter.reset_at_utc().timestamp())
        locale = str(getattr(interaction, "locale", "")).lower()
        guild_locale = str(getattr(interaction, "guild_locale", "")).lower()
        is_id = locale.startswith("id") or guild_locale.startswith("id")

        if is_id:
            title = "Limit /gen harian habis"
            description = (
                f"User biasa hanya bisa memakai `/gen` **{GEN_DAILY_LIMIT} kali per hari**.\n"
                f"Reset global berikutnya: <t:{reset_ts}:F> (<t:{reset_ts}:R>).\n\n"
                "Get Donor Role to remove daily limit by donating to the server"
            )
        else:
            title = "Daily /gen limit reached"
            description = (
                f"Regular users can use `/gen` **{GEN_DAILY_LIMIT} times per day**.\n"
                f"Next global reset: <t:{reset_ts}:F> (<t:{reset_ts}:R>).\n\n"
                "Get Donor Role to remove daily limit by donating to the server"
            )

        return discord.Embed(title=title, description=description, color=COLOR_WARNING)


async def setup(bot):
    await bot.add_cog(GameCommands(bot))
