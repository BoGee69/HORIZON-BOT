"""
R2 → SQLite inventory sync cog.

Keeps the ``r2_inventory`` table in SQLite up-to-date so that
OpenDir (and any other cog) can check whether a key already exists in R2
with a fast local SQL query instead of an expensive R2 list_objects_v2 call.

Schedule
--------
* Rebuild is triggered automatically on bot start (after a short delay).
* A periodic task re-runs every OPENDIR_R2_CACHE_TTL_HOURS (default 12 h).
* Admins can force an immediate rebuild with ``/r2cache rebuild``.

Admin slash commands
--------------------
``/r2cache rebuild``   – force a full R2 → SQLite rebuild right now
``/r2cache status``    – show cache stats (count, age, prefix)
"""
from __future__ import annotations

import asyncio
import logging
import time
from contextlib import suppress

import boto3
import discord
from discord import app_commands
from discord.ext import commands, tasks

import config as bot_config

log = logging.getLogger(__name__)


def _cfg_str(name: str, default: str = "") -> str:
    return str(getattr(bot_config, name, default) or default).strip()


def _cfg_float(name: str, default: float) -> float:
    try:
        return float(getattr(bot_config, name, default))
    except Exception:
        return default


def _cfg_bool(name: str, default: bool = False) -> bool:
    raw = getattr(bot_config, name, default)
    if isinstance(raw, bool):
        return raw
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _r2_configured() -> bool:
    return all([
        _cfg_str("R2_ACCOUNT_ID"),
        _cfg_str("R2_ACCESS_KEY_ID"),
        _cfg_str("R2_SECRET_ACCESS_KEY"),
        _cfg_str("R2_BUCKET_NAME"),
    ])


def _make_r2_client():
    return boto3.client(
        "s3",
        endpoint_url=f"https://{_cfg_str('R2_ACCOUNT_ID')}.r2.cloudflarestorage.com",
        aws_access_key_id=_cfg_str("R2_ACCESS_KEY_ID"),
        aws_secret_access_key=_cfg_str("R2_SECRET_ACCESS_KEY"),
        region_name="auto",
    )


class R2InventorySync(commands.Cog):
    """Keeps the SQLite r2_inventory table in sync with Cloudflare R2."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._lock = asyncio.Lock()
        self._rebuild_task: asyncio.Task | None = None

        self.prefix = self._norm_prefix(_cfg_str("OPENDIR_R2_PREFIX", "Database/"))
        self.bucket = _cfg_str("R2_BUCKET_NAME")
        # Rebuild interval in hours (reuse the same env var as OpenDir cache TTL)
        interval_hours = max(1.0, _cfg_float("OPENDIR_R2_CACHE_TTL_HOURS", 12.0))
        self._interval_seconds = interval_hours * 3600

        # Try to import the DB helper
        try:
            from utils.database import R2InventoryDB
            self._db = R2InventoryDB()
        except Exception as exc:
            log.error("R2InventorySync: cannot import R2InventoryDB: %s", exc)
            self._db = None  # type: ignore[assignment]

    # ─── lifecycle ──────────────────────────────────────────────────────

    async def cog_load(self) -> None:
        if not _r2_configured():
            log.warning("R2InventorySync: R2 credentials incomplete — cog idle.")
            return
        if self._db is None:
            log.warning("R2InventorySync: R2InventoryDB unavailable — cog idle.")
            return
        self._rebuild_task = asyncio.create_task(
            self._auto_rebuild_loop(), name="r2-inventory-sync-loop"
        )
        log.info(
            "R2InventorySync cog loaded (prefix=%r, interval=%.1fh)",
            self.prefix,
            self._interval_seconds / 3600,
        )

    async def cog_unload(self) -> None:
        if self._rebuild_task:
            self._rebuild_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._rebuild_task

    # ─── background loop ────────────────────────────────────────────────

    async def _auto_rebuild_loop(self) -> None:
        await self.bot.wait_until_ready()
        await asyncio.sleep(15)  # short delay after bot ready

        while not self.bot.is_closed():
            try:
                await self._do_rebuild(triggered_by="scheduler")
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("R2InventorySync: auto-rebuild failed")
            await asyncio.sleep(self._interval_seconds)

    # ─── core rebuild logic ─────────────────────────────────────────────

    async def _do_rebuild(self, *, triggered_by: str = "unknown") -> dict:
        async with self._lock:
            log.info("R2InventorySync: starting rebuild (triggered_by=%s) …", triggered_by)
            t0 = time.time()
            s3 = _make_r2_client()
            result = await asyncio.to_thread(
                self._db.rebuild, s3, self.bucket, self.prefix
            )
            result["triggered_by"] = triggered_by
            result["elapsed_seconds"] = round(time.time() - t0, 2)
            log.info("R2InventorySync: rebuild done — %s", result)
            return result

    # ─── admin slash commands ────────────────────────────────────────────

    r2cache = app_commands.Group(
        name="r2cache",
        description="Manage the local SQLite cache of R2 keys",
        guild_only=True,
    )

    def _is_admin(self, interaction: discord.Interaction) -> bool:
        admin_ids: list[int] = getattr(bot_config, "ADMIN_IDS", [])
        return interaction.user.id in admin_ids

    @r2cache.command(name="rebuild", description="Force a full R2 → SQLite key cache rebuild")
    async def cmd_rebuild(self, interaction: discord.Interaction) -> None:
        if not self._is_admin(interaction):
            await interaction.response.send_message("❌ Admin only.", ephemeral=True)
            return
        if self._db is None or not _r2_configured():
            await interaction.response.send_message("❌ R2 not configured.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        try:
            result = await self._do_rebuild(triggered_by=f"admin:{interaction.user}")
        except Exception as exc:
            await interaction.followup.send(f"❌ Rebuild failed: {exc}", ephemeral=True)
            return

        embed = discord.Embed(
            title="✅ R2 Cache Rebuilt",
            color=discord.Color.green(),
        )
        embed.add_field(name="Keys synced", value=str(result.get("keys_synced", "?")), inline=True)
        embed.add_field(name="Prefix", value=f"`{result.get('prefix', self.prefix)}`", inline=True)
        embed.add_field(name="Elapsed", value=f"{result.get('elapsed_seconds', '?')}s", inline=True)
        await interaction.followup.send(embed=embed, ephemeral=True)

    @r2cache.command(name="status", description="Show the current R2 SQLite cache statistics")
    async def cmd_status(self, interaction: discord.Interaction) -> None:
        if not self._is_admin(interaction):
            await interaction.response.send_message("❌ Admin only.", ephemeral=True)
            return
        if self._db is None:
            await interaction.response.send_message("❌ R2InventoryDB not available.", ephemeral=True)
            return

        count = self._db.count(self.prefix)
        last_ts = self._db.last_synced_at(self.prefix)
        age_s = time.time() - last_ts if last_ts else None

        embed = discord.Embed(title="R2 SQLite Cache Status", color=discord.Color.blue())
        embed.add_field(name="Cached keys", value=f"{count:,}", inline=True)
        embed.add_field(name="Prefix", value=f"`{self.prefix}`", inline=True)
        if age_s is not None:
            age_str = f"{age_s / 3600:.1f}h ago" if age_s >= 3600 else f"{int(age_s)}s ago"
        else:
            age_str = "never"
        embed.add_field(name="Last rebuilt", value=age_str, inline=True)
        embed.add_field(
            name="Rebuild interval",
            value=f"{self._interval_seconds / 3600:.1f}h",
            inline=True,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ─── helpers ────────────────────────────────────────────────────────

    @staticmethod
    def _norm_prefix(p: str) -> str:
        p = (p or "").strip().lstrip("/")
        return f"{p}/" if p and not p.endswith("/") else p


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(R2InventorySync(bot))
