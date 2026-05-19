"""
Open Directory → Cloudflare R2 Sync Module

Dual-mode logic:
  1. Initial Sync  — saat startup, scan semua folder di Open Directory dan upload
                     file yang belum ada di R2 (dibandingkan via key set).
  2. Monitor Mode  — setelah initial sync selesai, background task cek file baru
                     setiap OPENDIR_SYNC_INTERVAL_HOURS jam.

Design principles:
  - Zero local storage: aiohttp stream → BytesIO → boto3 (RAM only).
  - Non-blocking      : semua boto3 call dibungkus asyncio.to_thread().
  - Resilient         : timeout, retry, dan error handling per-file/per-folder
                        agar satu file rusak tidak menghentikan seluruh sync.
  - Concurrency       : asyncio.Semaphore membatasi upload paralel.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from io import BytesIO
from typing import Optional
from urllib.parse import urljoin

import aiohttp
import boto3
import discord
from botocore.exceptions import BotoCoreError, ClientError
from bs4 import BeautifulSoup
from discord import app_commands
from discord.ext import commands

import config as bot_config
from config import (
    ADMIN_IDS,
    ADMIN_ROLE_IDS,
    ADMIN_ROLE_NAMES,
    COLOR_ERROR,
    COLOR_INFO,
    COLOR_SUCCESS,
    COLOR_WARNING,
    R2_ACCESS_KEY_ID,
    R2_ACCOUNT_ID,
    R2_BUCKET_NAME,
    R2_SECRET_ACCESS_KEY,
)
from utils.helpers import is_admin_interaction

log = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────
# Konstanta & threshold
# ──────────────────────────────────────────────────────────────
_MULTIPART_THRESHOLD = getattr(bot_config, "OPENDIR_SYNC_MULTIPART_THRESHOLD_MB", 50) * 1024 * 1024
_CHUNK_SIZE          = getattr(bot_config, "OPENDIR_SYNC_CHUNK_SIZE_MB", 8) * 1024 * 1024
_MIN_PART_SIZE       = 5 * 1024 * 1024   # R2/S3 minimum 5 MB per part (hard limit AWS)
_HTTP_TIMEOUT        = aiohttp.ClientTimeout(
    total=getattr(bot_config, "OPENDIR_SYNC_TIMEOUT_SECONDS", 120),
    connect=15,
    sock_read=60,
)
_MAX_RETRIES  = 3
_RETRY_BACKOFF = 2.0   # seconds, doubles tiap retry


# ──────────────────────────────────────────────────────────────
# Admin guard (konsisten dengan cog lain)
# ──────────────────────────────────────────────────────────────
def admin_check():
    async def predicate(interaction: discord.Interaction) -> bool:
        if is_admin_interaction(interaction, ADMIN_IDS, ADMIN_ROLE_IDS, ADMIN_ROLE_NAMES):
            return True
        await interaction.response.send_message(
            embed=discord.Embed(
                title="Access Denied",
                description="This command is restricted to bot admins.",
                color=COLOR_ERROR,
            ),
            ephemeral=True,
        )
        return False
    return app_commands.check(predicate)


# ──────────────────────────────────────────────────────────────
# Data classes
# ──────────────────────────────────────────────────────────────
@dataclass
class SyncStats:
    """Statistik untuk satu sesi sync."""
    folders_scanned: int = 0
    files_found:     int = 0
    files_skipped:   int = 0   # sudah ada di R2
    files_uploaded:  int = 0
    files_failed:    int = 0
    bytes_uploaded:  int = 0
    errors: list[str]    = field(default_factory=list)
    started_at: float    = field(default_factory=time.time)

    @property
    def elapsed(self) -> float:
        return time.time() - self.started_at

    def to_embed_fields(self) -> dict[str, str]:
        return {
            "📁 Folders Scanned":   str(self.folders_scanned),
            "🔍 Files Found":       str(self.files_found),
            "✅ Uploaded":          str(self.files_uploaded),
            "⏭️ Skipped (exist)":  str(self.files_skipped),
            "❌ Failed":            str(self.files_failed),
            "📦 Data Transferred":  _fmt_bytes(self.bytes_uploaded),
            "⏱️ Duration":         f"{self.elapsed:.1f}s",
        }


def _fmt_bytes(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.2f} {unit}"
        n /= 1024
    return f"{n:.2f} PB"


# ──────────────────────────────────────────────────────────────
# Core sync engine
# ──────────────────────────────────────────────────────────────
def _make_s3_client():
    """Buat boto3 S3 client yang mengarah ke Cloudflare R2."""
    return boto3.client(
        "s3",
        endpoint_url=f"https://{R2_ACCOUNT_ID}.r2.cloudflarestorage.com",
        aws_access_key_id=R2_ACCESS_KEY_ID,
        aws_secret_access_key=R2_SECRET_ACCESS_KEY,
        region_name="auto",
    )


async def _list_r2_keys(s3, prefix: str) -> set[str]:
    """
    Ambil semua key di R2 bucket di bawah prefix tertentu.
    Dijalankan di thread pool karena boto3 bersifat blocking.
    """
    def _do_list():
        keys = set()
        paginator = s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=R2_BUCKET_NAME, Prefix=prefix):
            for obj in page.get("Contents", []):
                keys.add(obj["Key"])
        return keys

    return await asyncio.to_thread(_do_list)


async def _fetch_links(session: aiohttp.ClientSession, url: str) -> list[str]:
    """
    Fetch HTML dari `url`, parse semua <a href> dengan BeautifulSoup.
    Return list href mentah. Retry otomatis dengan exponential backoff.
    """
    for attempt in range(1, _MAX_RETRIES + 1):
        try:
            async with session.get(url, timeout=_HTTP_TIMEOUT) as resp:
                resp.raise_for_status()
                html = await resp.text(errors="replace")
            soup = BeautifulSoup(html, "html.parser")
            return [
                a["href"]
                for a in soup.find_all("a", href=True)
                if a["href"] not in ("#", "", "/", "../", "./")
            ]
        except asyncio.TimeoutError:
            log.warning("⏱️ Timeout fetching %s (attempt %d/%d)", url, attempt, _MAX_RETRIES)
        except aiohttp.ClientResponseError as exc:
            log.warning("🌐 HTTP %s fetching %s (attempt %d/%d)", exc.status, url, attempt, _MAX_RETRIES)
            if exc.status in (403, 404, 410):
                break  # Jangan retry 4xx
        except aiohttp.ClientError as exc:
            log.warning("🌐 Client error fetching %s: %s (attempt %d/%d)", url, exc, attempt, _MAX_RETRIES)
        except Exception as exc:
            log.error("❌ Unexpected error fetching %s: %s", url, exc)
            break

        if attempt < _MAX_RETRIES:
            await asyncio.sleep(_RETRY_BACKOFF * attempt)

    return []


async def _upload_small(s3, key: str, data: bytes) -> None:
    """Upload file kecil via put_object (blocking call di thread)."""
    def _do():
        s3.put_object(Bucket=R2_BUCKET_NAME, Key=key, Body=data)
    await asyncio.to_thread(_do)


async def _upload_multipart(
    s3,
    key: str,
    session: aiohttp.ClientSession,
    url: str,
) -> int:
    """
    Upload file besar via S3 Multipart Upload, streaming dari aiohttp.
    Data TIDAK pernah menyentuh disk — buffer per-chunk di RAM.
    Return jumlah bytes yang berhasil diupload.
    """
    # Buat multipart upload session di R2
    def _create():
        return s3.create_multipart_upload(Bucket=R2_BUCKET_NAME, Key=key)
    mpu       = await asyncio.to_thread(_create)
    upload_id = mpu["UploadId"]

    parts        = []
    part_number  = 1
    total_bytes  = 0
    buf          = BytesIO()

    async def _flush_part(part_buf: BytesIO) -> dict:
        nonlocal part_number, total_bytes
        part_buf.seek(0)
        raw  = part_buf.read()
        pn   = part_number
        total_bytes += len(raw)

        def _up():
            return s3.upload_part(
                Bucket=R2_BUCKET_NAME,
                Key=key,
                UploadId=upload_id,
                PartNumber=pn,
                Body=raw,
            )
        resp = await asyncio.to_thread(_up)
        part_number += 1
        return {"PartNumber": pn, "ETag": resp["ETag"]}

    try:
        async with session.get(url, timeout=_HTTP_TIMEOUT) as resp:
            resp.raise_for_status()
            async for chunk in resp.content.iter_chunked(_CHUNK_SIZE):
                buf.write(chunk)
                # Flush ke R2 kalau buffer sudah cukup besar (≥ minimum part size)
                if buf.tell() >= _MIN_PART_SIZE:
                    parts.append(await _flush_part(buf))
                    buf = BytesIO()

        # Flush sisa buffer terakhir (bisa < minimum part size — diizinkan untuk part terakhir)
        if buf.tell() > 0:
            parts.append(await _flush_part(buf))

        # Complete multipart upload
        def _complete():
            s3.complete_multipart_upload(
                Bucket=R2_BUCKET_NAME,
                Key=key,
                UploadId=upload_id,
                MultipartUpload={"Parts": parts},
            )
        await asyncio.to_thread(_complete)
        return total_bytes

    except Exception:
        # WAJIB abort agar tidak ada incomplete multipart yang menumpuk di R2 (kena billing!)
        def _abort():
            try:
                s3.abort_multipart_upload(
                    Bucket=R2_BUCKET_NAME, Key=key, UploadId=upload_id
                )
                log.info("🗑️ Aborted incomplete multipart upload for %s", key)
            except Exception as e:
                log.warning("⚠️ Failed to abort multipart upload for %s: %s", key, e)
        await asyncio.to_thread(_abort)
        raise


async def _upload_file(
    s3,
    session: aiohttp.ClientSession,
    file_url: str,
    r2_key: str,
) -> int:
    """
    Stream file dari Open Directory ke R2. Tidak ada disk I/O.
    Otomatis pilih strategi: put_object (kecil) atau multipart (besar).
    Retry dengan exponential backoff. Return jumlah bytes uploaded.
    """
    # HEAD request opsional untuk cek ukuran file
    content_length: Optional[int] = None
    try:
        async with session.head(file_url, timeout=aiohttp.ClientTimeout(total=10)) as head:
            cl = head.headers.get("Content-Length")
            if cl and cl.isdigit():
                content_length = int(cl)
    except Exception:
        pass  # HEAD gagal → tidak masalah, lanjut ke GET

    # File besar → langsung multipart streaming (tidak perlu buffer penuh di RAM)
    if content_length is not None and content_length > _MULTIPART_THRESHOLD:
        log.debug("🔀 Multipart upload: %s (%s)", r2_key, _fmt_bytes(content_length))
        for attempt in range(1, _MAX_RETRIES + 1):
            try:
                return await _upload_multipart(s3, r2_key, session, file_url)
            except (asyncio.TimeoutError, aiohttp.ClientError) as exc:
                log.warning("⚠️ Multipart attempt %d/%d failed for %s: %s", attempt, _MAX_RETRIES, r2_key, exc)
            except (BotoCoreError, ClientError) as exc:
                log.warning("☁️ R2 multipart error for %s: %s", r2_key, exc)
            if attempt < _MAX_RETRIES:
                await asyncio.sleep(_RETRY_BACKOFF * attempt)
        raise RuntimeError(f"Multipart upload failed after {_MAX_RETRIES} attempts: {r2_key}")

    # File kecil / ukuran tidak diketahui → baca ke BytesIO, lalu put_object
    for attempt in range(1, _MAX_RETRIES + 1):
        try:
            async with session.get(file_url, timeout=_HTTP_TIMEOUT) as resp:
                resp.raise_for_status()
                data = await resp.read()  # In-memory, no disk

            await _upload_small(s3, r2_key, data)
            return len(data)

        except asyncio.TimeoutError:
            log.warning("⏱️ Timeout downloading %s (attempt %d/%d)", file_url, attempt, _MAX_RETRIES)
        except aiohttp.ClientResponseError as exc:
            log.warning("🌐 HTTP %s for %s (attempt %d/%d)", exc.status, file_url, attempt, _MAX_RETRIES)
            if exc.status in (403, 404, 410):
                break
        except (BotoCoreError, ClientError) as exc:
            log.warning("☁️ R2 error for %s: %s (attempt %d/%d)", r2_key, exc, attempt, _MAX_RETRIES)
        except Exception as exc:
            log.error("❌ Unexpected error uploading %s: %s", r2_key, exc)
            break

        if attempt < _MAX_RETRIES:
            await asyncio.sleep(_RETRY_BACKOFF * attempt)

    raise RuntimeError(f"Upload failed after {_MAX_RETRIES} attempts: {r2_key}")


async def run_sync(
    session: aiohttp.ClientSession,
    base_url: str,
    r2_prefix: str,
    allowed_exts: set[str],
    existing_keys: set[str],
    semaphore: asyncio.Semaphore,
    stop_event: asyncio.Event,
) -> SyncStats:
    """
    Satu sesi sync penuh:
      1. Scan semua subfolder di Open Directory.
      2. Bangun task list: file yang belum ada di existing_keys.
      3. Upload secara concurrent (bounded oleh semaphore).

    `existing_keys` diupdate in-place saat upload berhasil.
    `stop_event` memungkinkan pembatalan dari luar.
    """
    stats = SyncStats()
    s3    = _make_s3_client()

    if not base_url.endswith("/"):
        base_url += "/"

    # ── Tahap 1: Temukan subfolder di root Open Directory ──────
    log.info("🔍 Scanning root: %s", base_url)
    root_links = await _fetch_links(session, base_url)

    # href yang berakhiran '/' dan bukan navigasi ('..') = subfolder
    folders = [
        urljoin(base_url, href)
        for href in root_links
        if href.endswith("/") and href not in ("../", "./", "/")
    ]

    # Kalau tidak ada subfolder, root sendiri dianggap folder berisi file
    if not folders:
        log.info("ℹ️  No subfolders found — treating root as single folder")
        folders = [base_url]

    log.info("📂 %d folders to scan", len(folders))

    # ── Tahap 2: Kumpulkan task upload dari semua folder ──────
    upload_tasks: list[tuple[str, str]] = []

    for folder_url in folders:
        if stop_event.is_set():
            log.info("🛑 Stop event received — aborting folder scan")
            break

        stats.folders_scanned += 1
        file_links = await _fetch_links(session, folder_url)

        for href in file_links:
            if stop_event.is_set():
                break
            if href.endswith("/"):
                continue  # Lewati nested subfolder (tidak rekursif)

            filename = href.split("/")[-1]
            ext      = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""

            if ext not in allowed_exts:
                continue

            stats.files_found += 1
            file_url  = urljoin(folder_url, href)

            # R2 key: preserve path relatif di bawah base_url
            try:
                rel = file_url[len(base_url):].lstrip("/")
            except Exception:
                rel = filename
            r2_key = r2_prefix.rstrip("/") + "/" + rel

            if r2_key in existing_keys:
                stats.files_skipped += 1
                continue

            upload_tasks.append((file_url, r2_key))

    # ── Tahap 3: Upload concurrent dengan semaphore ────────────
    async def _bounded_upload(file_url: str, r2_key: str) -> None:
        if stop_event.is_set():
            return
        async with semaphore:
            if stop_event.is_set():
                return
            try:
                log.info("⬆️  %s → %s", file_url.split("/")[-1], r2_key)
                size = await _upload_file(s3, session, file_url, r2_key)
                existing_keys.add(r2_key)
                stats.files_uploaded += 1
                stats.bytes_uploaded += size
                log.info("✅ %s (%s)", r2_key.split("/")[-1], _fmt_bytes(size))
            except Exception as exc:
                stats.files_failed += 1
                msg = f"{r2_key.split('/')[-1]}: {exc}"
                stats.errors.append(msg[:200])
                log.error("❌ Upload failed — %s", msg)

    if upload_tasks:
        log.info(
            "🚀 %d files to upload (max concurrent: %d)",
            len(upload_tasks),
            semaphore._value,  # type: ignore[attr-defined]
        )
        await asyncio.gather(*[_bounded_upload(u, k) for u, k in upload_tasks])

    return stats


# ──────────────────────────────────────────────────────────────
# Cog
# ──────────────────────────────────────────────────────────────
class OpenDirSync(commands.Cog):
    """Mensinkronisasi file dari Open Directory ke Cloudflare R2."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

        # Async primitives
        self._task:        asyncio.Task | None = None
        self._lock:        asyncio.Lock        = asyncio.Lock()
        self._stop_event:  asyncio.Event       = asyncio.Event()
        self._semaphore:   asyncio.Semaphore   = asyncio.Semaphore(
            getattr(bot_config, "OPENDIR_SYNC_MAX_CONCURRENT", 3)
        )

        # State
        self._initial_sync_done: bool          = False
        self._paused:            bool          = False
        self._last_stats:        SyncStats | None = None
        self._existing_keys:     set[str]      = set()

        # Config
        self.enabled        = getattr(bot_config, "OPENDIR_SYNC_ENABLED", False)
        self.base_url: str  = getattr(bot_config, "OPENDIR_SYNC_URL", "").rstrip("/") + "/"
        self.r2_prefix      = getattr(bot_config, "OPENDIR_SYNC_R2_PREFIX", "Database/")
        self.interval_hours = float(getattr(bot_config, "OPENDIR_SYNC_INTERVAL_HOURS", 6.0))
        self.start_delay    = float(getattr(bot_config, "OPENDIR_SYNC_START_DELAY_SECONDS", 15.0))
        self.allowed_exts: set[str] = set(
            getattr(bot_config, "OPENDIR_SYNC_EXTENSIONS", {"zip", "manifest", "lua"})
        )

    # ── Lifecycle ──────────────────────────────────────────────

    async def cog_load(self):
        if not self.enabled:
            log.info("ℹ️  OpenDirSync disabled — set OPENDIR_SYNC_ENABLED=true untuk aktifkan")
            return
        if not self.base_url or self.base_url == "/":
            log.warning("⚠️  OPENDIR_SYNC_URL kosong. OpenDirSync tidak akan berjalan.")
            return
        if not all([R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, R2_ACCOUNT_ID, R2_BUCKET_NAME]):
            log.warning("⚠️  R2 credentials belum lengkap. OpenDirSync tidak akan berjalan.")
            return

        self._task = asyncio.create_task(self._sync_loop(), name="opendir_sync_loop")
        log.info(
            "🔄 OpenDirSync enabled | URL: %s | Interval: %.1fh | Prefix: %s",
            self.base_url, self.interval_hours, self.r2_prefix,
        )

    async def cog_unload(self):
        self._stop_event.set()
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        log.info("🛑 OpenDirSync cog unloaded")

    # ── Background loop ────────────────────────────────────────

    async def _sync_loop(self):
        await self.bot.wait_until_ready()
        log.info("⏳ OpenDirSync waiting %.1fs before first run...", self.start_delay)
        await asyncio.sleep(self.start_delay)

        while not self._stop_event.is_set():
            if self._paused:
                await asyncio.sleep(60)
                continue

            mode = "Initial" if not self._initial_sync_done else "Monitor"
            try:
                await self._run_sync(automatic=True)
                self._initial_sync_done = True
                log.info("✅ %s sync complete. Next in %.1fh.", mode, self.interval_hours)
            except asyncio.CancelledError:
                log.info("🛑 OpenDirSync loop cancelled")
                return
            except Exception as exc:
                log.error("❌ OpenDirSync loop error: %s", exc, exc_info=True)

            # Tunggu interval, bisa di-interrupt oleh stop_event
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(),
                    timeout=self.interval_hours * 3600,
                )
            except asyncio.TimeoutError:
                pass  # Normal → loop lagi

    async def _run_sync(self, automatic: bool = True) -> SyncStats:
        """
        Satu sesi sync lengkap, dilindungi oleh lock agar tidak parallel.
        """
        async with self._lock:
            log.info(
                "🔄 [%s] OpenDirSync dimulai (%s mode)...",
                "AUTO" if automatic else "MANUAL",
                "Initial" if not self._initial_sync_done else "Monitor",
            )

            # Gunakan shared bot session jika tersedia
            bot_session: aiohttp.ClientSession | None = getattr(self.bot, "session", None)
            if bot_session and not bot_session.closed:
                session   = bot_session
                own_session = False
            else:
                session   = aiohttp.ClientSession()
                own_session = True

            try:
                # Initial sync: list semua R2 key sebagai baseline pembanding
                if not self._initial_sync_done or not self._existing_keys:
                    log.info("📋 Listing R2 keys under prefix '%s'...", self.r2_prefix)
                    s3 = _make_s3_client()
                    self._existing_keys = await _list_r2_keys(s3, self.r2_prefix)
                    log.info("📋 R2 baseline: %d existing keys", len(self._existing_keys))

                stats = await run_sync(
                    session      = session,
                    base_url     = self.base_url,
                    r2_prefix    = self.r2_prefix,
                    allowed_exts = self.allowed_exts,
                    existing_keys= self._existing_keys,
                    semaphore    = self._semaphore,
                    stop_event   = self._stop_event,
                )
                self._last_stats = stats

                log.info(
                    "📊 Sync: ✅%d uploaded | ⏭️%d skipped | ❌%d failed | %s | %.1fs",
                    stats.files_uploaded, stats.files_skipped, stats.files_failed,
                    _fmt_bytes(stats.bytes_uploaded), stats.elapsed,
                )

                # Notifikasi admin kalau ada failure
                if stats.files_failed > 0 and hasattr(self.bot, "notify_admins"):
                    await self.bot.notify_admins(
                        "OpenDirSync — Partial Failure",
                        f"{stats.files_failed} file gagal diupload ke R2.",
                        level="warning",
                        fields={
                            "Uploaded": str(stats.files_uploaded),
                            "Failed":   str(stats.files_failed),
                            "Sample errors": "\n".join(stats.errors[:3]),
                        },
                        key="opendir-sync-partial-failure",
                    )

                return stats

            finally:
                if own_session:
                    await session.close()

    # ── Slash Commands ─────────────────────────────────────────

    sync_group = app_commands.Group(
        name="opendirsync",
        description="Kelola sinkronisasi Open Directory → R2",
    )

    @sync_group.command(name="status", description="Lihat status sync Open Directory")
    @admin_check()
    async def cmd_status(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        enabled_txt  = "✅ Enabled"  if self.enabled  else "❌ Disabled"
        mode_txt     = "Initial Sync" if not self._initial_sync_done else "Monitor Mode"
        paused_txt   = " (⏸️ Paused)" if self._paused  else ""
        running_txt  = "🔄 Running"  if self._lock.locked() else "💤 Idle"

        embed = discord.Embed(
            title="📡 OpenDir Sync — Status",
            color=COLOR_INFO if self.enabled else COLOR_WARNING,
            timestamp=discord.utils.utcnow(),
        )
        embed.add_field(name="Status",          value=f"{enabled_txt}{paused_txt}", inline=True)
        embed.add_field(name="Mode",            value=mode_txt,                     inline=True)
        embed.add_field(name="Task",            value=running_txt,                  inline=True)
        embed.add_field(name="Source URL",      value=f"`{self.base_url}`",         inline=False)
        embed.add_field(name="R2 Prefix",       value=f"`{self.r2_prefix}`",        inline=True)
        embed.add_field(name="Interval",        value=f"{self.interval_hours}h",    inline=True)
        embed.add_field(name="R2 Keys Tracked", value=str(len(self._existing_keys)),inline=True)
        embed.add_field(
            name="Extensions",
            value=", ".join(sorted(self.allowed_exts)) or "—",
            inline=True,
        )

        if self._last_stats:
            s = self._last_stats
            embed.add_field(
                name="Last Sync",
                value=(
                    f"✅ {s.files_uploaded} up | ⏭️ {s.files_skipped} skip | "
                    f"❌ {s.files_failed} fail | 📦 {_fmt_bytes(s.bytes_uploaded)} | ⏱️ {s.elapsed:.1f}s"
                ),
                inline=False,
            )
            if s.errors:
                embed.add_field(
                    name="Last Errors",
                    value="\n".join(f"• {e}" for e in s.errors[:3])[:1024],
                    inline=False,
                )

        await interaction.followup.send(embed=embed, ephemeral=True)

    @sync_group.command(name="trigger", description="Jalankan sync manual sekarang")
    @admin_check()
    async def cmd_trigger(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)

        if not self.enabled:
            await interaction.followup.send(
                embed=discord.Embed(
                    title="❌ OpenDirSync Disabled",
                    description="Set `OPENDIR_SYNC_ENABLED=true` di `.env` untuk mengaktifkan.",
                    color=COLOR_ERROR,
                ),
                ephemeral=True,
            )
            return

        if self._lock.locked():
            await interaction.followup.send(
                embed=discord.Embed(
                    title="⚠️ Sync Sedang Berjalan",
                    description="Tunggu sync saat ini selesai, atau cek status via `/opendirsync status`.",
                    color=COLOR_WARNING,
                ),
                ephemeral=True,
            )
            return

        if self._paused:
            await interaction.followup.send(
                embed=discord.Embed(
                    title="⏸️ Sync Di-pause",
                    description="Gunakan `/opendirsync pause` untuk resume terlebih dulu.",
                    color=COLOR_WARNING,
                ),
                ephemeral=True,
            )
            return

        # Kirim acknowledgement dulu, lalu jalankan sync
        await interaction.followup.send(
            embed=discord.Embed(
                title="🚀 Manual Sync Dimulai",
                description=(
                    "Sync berjalan. Tunggu sebentar...\n"
                    "Cek `/opendirsync status` untuk progress real-time."
                ),
                color=COLOR_INFO,
            ),
            ephemeral=True,
        )

        try:
            stats = await self._run_sync(automatic=False)
            self._initial_sync_done = True

            color = COLOR_ERROR if stats.files_failed > 0 else COLOR_SUCCESS
            result_embed = discord.Embed(
                title="✅ Manual Sync Selesai",
                color=color,
                timestamp=discord.utils.utcnow(),
            )
            for name, value in stats.to_embed_fields().items():
                result_embed.add_field(name=name, value=value, inline=True)
            if stats.errors:
                result_embed.add_field(
                    name="Errors",
                    value="\n".join(f"• {e}" for e in stats.errors[:5])[:1024],
                    inline=False,
                )
            await interaction.followup.send(embed=result_embed, ephemeral=True)

        except Exception as exc:
            await interaction.followup.send(
                embed=discord.Embed(
                    title="❌ Sync Gagal",
                    description=f"```{str(exc)[:1500]}```",
                    color=COLOR_ERROR,
                ),
                ephemeral=True,
            )

    @sync_group.command(name="pause", description="Pause atau resume sync otomatis")
    @admin_check()
    async def cmd_pause(self, interaction: discord.Interaction):
        self._paused = not self._paused
        label = "⏸️ Di-pause" if self._paused else "▶️ Di-resume"
        color = COLOR_WARNING if self._paused else COLOR_SUCCESS
        desc  = (
            "Sync otomatis di-pause. Manual trigger via `/opendirsync trigger` masih bisa."
            if self._paused
            else "Sync otomatis aktif kembali."
        )
        await interaction.response.send_message(
            embed=discord.Embed(title=f"OpenDirSync {label}", description=desc, color=color),
            ephemeral=True,
        )

    @sync_group.command(name="refresh_keys", description="Refresh cache daftar key dari R2 bucket")
    @admin_check()
    async def cmd_refresh_keys(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            s3        = _make_s3_client()
            old_count = len(self._existing_keys)
            self._existing_keys = await _list_r2_keys(s3, self.r2_prefix)
            new_count = len(self._existing_keys)
            await interaction.followup.send(
                embed=discord.Embed(
                    title="🔄 R2 Key Cache Refreshed",
                    description=(
                        f"**Sebelum:** {old_count:,} keys\n"
                        f"**Sekarang:** {new_count:,} keys\n"
                        f"**Delta:** {new_count - old_count:+,}"
                    ),
                    color=COLOR_SUCCESS,
                    timestamp=discord.utils.utcnow(),
                ),
                ephemeral=True,
            )
        except Exception as exc:
            await interaction.followup.send(
                embed=discord.Embed(
                    title="❌ Refresh Gagal",
                    description=f"```{str(exc)[:1000]}```",
                    color=COLOR_ERROR,
                ),
                ephemeral=True,
            )


# ──────────────────────────────────────────────────────────────
# Setup hook
# ──────────────────────────────────────────────────────────────
async def setup(bot: commands.Bot):
    await bot.add_cog(OpenDirSync(bot))
