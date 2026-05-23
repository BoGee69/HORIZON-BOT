"""
SQLite-driven Open Directory -> Cloudflare R2 sync cog.

Purpose:
- Use the persistent SQLite database as the source list of AppIDs and game names.
- For each game, check whether matching files exist in a configured Open Directory.
- Stream matching files directly from HTTP to Cloudflare R2 without writing to local disk.
- Rename uploaded objects into the normal TriadBot format: "Game Name (AppID).zip".
- Notify admins through the existing bot notifier when a sync run finishes.

This cog is intentionally disabled by default. Only enable it for sources that you own
or have permission to mirror.
"""
from __future__ import annotations

import asyncio
import inspect
import json
import logging
import posixpath
import queue
import re
import time
import unicodedata
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import quote, unquote, urldefrag, urljoin, urlparse

import aiohttp
import boto3
from boto3.s3.transfer import TransferConfig
from bs4 import BeautifulSoup
from discord.ext import commands

import config as bot_config

try:
    from utils.rename_database_files import sanitize_game_name as _sanitize_game_name
except Exception:
    _sanitize_game_name = None

try:
    from utils.r2_inventory import invalidate_r2_inventory_cache
except Exception:
    def invalidate_r2_inventory_cache() -> None:
        return None

try:
    from utils.r2_maintenance import clean_zip_comments
except Exception:
    clean_zip_comments = None

try:
    from utils.database import R2InventoryDB, SQLITE_PATH as _SQLITE_PATH
    _r2_inventory_db: R2InventoryDB | None = R2InventoryDB()
except Exception:
    _r2_inventory_db = None
    _SQLITE_PATH = None


log = logging.getLogger(__name__)

_SENTINEL = object()
_DEFAULT_CHUNK_SIZE = 1024 * 1024
_APPID_RE = re.compile(r"(?<!\d)(\d{2,10})(?!\d)")
_HTML_CONTENT_TYPES = {"text/html", "application/xhtml+xml"}
_INVALID_FILENAME_CHARS = '<>:"/\\|?*'


def _cfg_str(name: str, default: str = "") -> str:
    return str(getattr(bot_config, name, default) or default).strip()


def _cfg_int(name: str, default: int) -> int:
    try:
        return int(getattr(bot_config, name, default))
    except Exception:
        return default


def _cfg_float(name: str, default: float) -> float:
    try:
        return float(getattr(bot_config, name, default))
    except Exception:
        return default


def _cfg_bool(name: str, default: bool = False) -> bool:
    raw = getattr(bot_config, name, default)
    if isinstance(raw, bool):
        return raw
    if raw is None:
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _cfg_list(name: str, default: str = "") -> list[str]:
    raw = getattr(bot_config, name, default)
    if isinstance(raw, (list, tuple, set)):
        return [str(x).strip() for x in raw if str(x).strip()]
    return [x.strip() for x in str(raw or default).split(",") if x.strip()]


def _cfg_path(name: str, default: Path) -> Path:
    raw = _cfg_str(name, "")
    if not raw:
        return default
    path = Path(raw)
    if not path.is_absolute():
        base_dir = Path(getattr(bot_config, "BASE_DIR", Path(__file__).resolve().parents[1]))
        path = base_dir / path
    return path


def _safe_game_name(name: str, *, max_len: int = 170) -> str:
    if _sanitize_game_name:
        cleaned = _sanitize_game_name(str(name), max_len=max_len)
        if cleaned:
            return cleaned
    cleaned = "".join(" " if ch in _INVALID_FILENAME_CHARS or ord(ch) < 32 else ch for ch in str(name))
    cleaned = " ".join(cleaned.split()).strip(" .")
    return (cleaned[:max_len].strip(" .") or "Unknown Game")


def _safe_metadata_value(value: Any, *, max_len: int = 180) -> str:
    """
    S3 user metadata values must be US-ASCII. Object keys may keep Unicode, but
    metadata such as game-name must be normalized before boto validates it.
    """
    text = str(value or "").replace("\r", " ").replace("\n", " ")
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    text = " ".join(text.split()).strip()
    return (text[:max_len].strip() or "Unknown Game")


class OpenDirSyncError(RuntimeError):
    """Raised for recoverable sync failures."""


class QueueReadStream:
    """
    Blocking file-like object backed by a queue.

    aiohttp runs in the asyncio event loop as producer. boto3 runs in a worker
    thread as consumer and calls .read(). This keeps the sync RAM-only and avoids
    writing downloaded files to local storage.
    """

    def __init__(self, q: "queue.Queue[bytes | object | BaseException]") -> None:
        self._q = q
        self._buffer = bytearray()
        self._closed = False

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return False

    def close(self) -> None:
        self._closed = True

    def read(self, size: int = -1) -> bytes:
        if self._closed:
            return b""

        if size is None or size < 0:
            chunks: list[bytes] = []
            if self._buffer:
                chunks.append(bytes(self._buffer))
                self._buffer.clear()
            while True:
                item = self._q.get()
                if item is _SENTINEL:
                    self._closed = True
                    break
                if isinstance(item, BaseException):
                    self._closed = True
                    raise item
                chunks.append(item)  # type: ignore[arg-type]
            return b"".join(chunks)

        while len(self._buffer) < size:
            item = self._q.get()
            if item is _SENTINEL:
                self._closed = True
                break
            if isinstance(item, BaseException):
                self._closed = True
                raise item
            self._buffer.extend(item)  # type: ignore[arg-type]

        if not self._buffer:
            return b""

        out = bytes(self._buffer[:size])
        del self._buffer[:size]
        return out


@dataclass(slots=True)
class GameRecord:
    appid: str
    name: str

    @property
    def safe_name(self) -> str:
        return _safe_game_name(self.name)


@dataclass(slots=True)
class RemoteFile:
    url: str
    relative_path: str
    filename: str
    target_key: str
    appid: str
    game_name: str
    extension: str
    size: int | None = None


@dataclass(slots=True)
class SyncState:
    cursor: int = 0
    completed_cycles: int = 0
    last_run_at: float = 0.0


@dataclass(slots=True)
class SyncSummary:
    mode: str
    started_at: float = field(default_factory=time.time)
    games_total: int = 0
    games_checked: int = 0
    cursor_start: int = 0
    cursor_next: int = 0
    full_cycle_completed: bool = False
    directories_scanned: int = 0
    indexed_files: int = 0
    candidates_checked: int = 0
    remote_matches: int = 0
    files_seen: int = 0
    files_existing: int = 0
    files_uploaded: int = 0
    files_skipped: int = 0
    no_match: int = 0
    bytes_uploaded: int = 0
    cleaned_objects: int = 0
    cleaned_files: int = 0
    api_transient_failures: int = 0
    errors: list[str] = field(default_factory=list)
    samples: list[str] = field(default_factory=list)

    @property
    def elapsed_seconds(self) -> float:
        return max(0.0, time.time() - self.started_at)

    @property
    def has_errors(self) -> bool:
        return bool(self.errors)

    def add_error(self, message: str) -> None:
        safe = str(message).replace("\n", " ")[:500]
        self.errors.append(safe)

    def add_sample(self, message: str) -> None:
        if len(self.samples) < 12:
            self.samples.append(str(message).replace("\n", " ")[:300])

    def to_fields(self) -> dict[str, str]:
        return {
            "Mode": self.mode,
            "Games total": str(self.games_total),
            "Games checked": str(self.games_checked),
            "Cursor": f"{self.cursor_start} -> {self.cursor_next}",
            "Cycle completed": str(self.full_cycle_completed),
            "Directories": str(self.directories_scanned),
            "Indexed files": str(self.indexed_files),
            "Candidates checked": str(self.candidates_checked),
            "Remote matches": str(self.remote_matches),
            "Already existed": str(self.files_existing),
            "Uploaded": str(self.files_uploaded),
            "Skipped": str(self.files_skipped),
            "No match": str(self.no_match),
            "Bytes uploaded": str(self.bytes_uploaded),
            "Cleaned objects": str(self.cleaned_objects),
            "Cleaned files": str(self.cleaned_files),
            "API transient": str(self.api_transient_failures),
            "Errors": str(len(self.errors)),
            "Elapsed": f"{self.elapsed_seconds:.1f}s",
        }


class OpenDirSync(commands.Cog):
    """Background SQLite-driven OpenDir sync cog."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._task: asyncio.Task | None = None
        self._lock = asyncio.Lock()
        self._priority_lock = asyncio.Lock()
        self._initial_done = False
        self._priority_pending: set[str] = set()
        self._priority_tasks: set[asyncio.Task] = set()

        self.enabled = _cfg_bool("OPENDIR_SYNC_ENABLED", False)
        self.base_url = self._normalize_base_url(_cfg_str("OPENDIR_BASE_URL", ""))
        self.r2_prefix = self._normalize_r2_prefix(_cfg_str("OPENDIR_R2_PREFIX", "Database/"))
        self.source_mode = _cfg_str("OPENDIR_SOURCE_MODE", "api").lower() or "api"
        if self.source_mode in {"manifest", "manifest_api", "generate", "generator"}:
            self.source_mode = "api"
        if self.source_mode in {"opendir", "directory", "dir", "direct"}:
            self.source_mode = "directory"
        if self.source_mode not in {"api", "directory"}:
            log.warning("Unknown OPENDIR_SOURCE_MODE=%r; falling back to api", self.source_mode)
            self.source_mode = "api"

        self.api_base_url = self._normalize_base_url(_cfg_str("OPENDIR_API_BASE_URL", self.base_url))
        self.api_search_path = _cfg_str("OPENDIR_API_SEARCH_PATH", "/api/search") or "/api/search"
        self.api_generate_path = _cfg_str("OPENDIR_API_GENERATE_PATH", "/api/generate") or "/api/generate"
        self.api_default_manifest_id = _cfg_str("OPENDIR_API_DEFAULT_MANIFEST_ID", "7884779798207988041")
        self.api_branch = _cfg_str("OPENDIR_API_BRANCH", "public") or "public"
        self.api_depot_key = _cfg_str("OPENDIR_API_DEPOT_KEY", "")
        self.api_use_ryuu_api = _cfg_bool("OPENDIR_API_USE_RYUU_API", True)
        self.api_lookup_before_generate = _cfg_bool("OPENDIR_API_LOOKUP_BEFORE_GENERATE", True)
        self.api_clean_before_upload = _cfg_bool(
            "OPENDIR_API_CLEAN_BEFORE_UPLOAD",
            _cfg_bool("R2_MAINTENANCE_CLEAN_COMMENTS", True),
        )
        self.api_generate_retries = max(1, _cfg_int("OPENDIR_API_GENERATE_RETRIES", 3))
        self.api_retry_delay = max(0.0, _cfg_float("OPENDIR_API_RETRY_DELAY_SECONDS", 2.0))

        self.state_path = _cfg_path(
            "OPENDIR_STATE_PATH",
            Path(getattr(bot_config, "DATA_DIR", Path("data"))) / "opendir_sync_state.json",
        )

        self.target_extensions = {
            ext.lower().lstrip(".")
            for ext in _cfg_list("OPENDIR_TARGET_EXTENSIONS", "zip")
            if ext.strip()
        } or {"zip"}
        allowed_extensions = _cfg_list("OPENDIR_ALLOWED_EXTENSIONS", ",".join(sorted(self.target_extensions)))
        self.allowed_extensions = {ext.lower().lstrip(".") for ext in allowed_extensions if ext}

        allowed_hosts = _cfg_list("OPENDIR_ALLOWED_HOSTS", "")
        self.allowed_hosts = {host.lower() for host in allowed_hosts if host}

        self.index_scan_enabled = _cfg_bool("OPENDIR_INDEX_SCAN_ENABLED", True)
        self.direct_probe_enabled = _cfg_bool("OPENDIR_DIRECT_PROBE_ENABLED", True)
        self.use_head = _cfg_bool("OPENDIR_USE_HEAD", True)
        self.fallback_get_probe = _cfg_bool("OPENDIR_FALLBACK_GET_PROBE", True)
        self.run_on_start = _cfg_bool("OPENDIR_RUN_ON_START", True)
        self.notify_on_success = _cfg_bool("OPENDIR_NOTIFY_ON_SUCCESS", False)

        self.max_depth = max(0, _cfg_int("OPENDIR_MAX_DEPTH", 3))
        self.max_games_per_run = max(0, _cfg_int("OPENDIR_MAX_GAMES_PER_RUN", 500))
        self.max_files_per_run = max(0, _cfg_int("OPENDIR_MAX_FILES_PER_RUN", 20))
        self.max_file_bytes = max(1, _cfg_int("OPENDIR_MAX_FILE_MB", 1024)) * 1024 * 1024
        default_buffer_mb = min(
            max(1, _cfg_int("OPENDIR_MAX_FILE_MB", 1024)),
            max(1, _cfg_int("R2_MAINTENANCE_MAX_ZIP_MB", 50)),
        )
        self.api_buffer_max_bytes = max(
            1,
            _cfg_int("OPENDIR_API_BUFFER_MAX_MB", default_buffer_mb),
        ) * 1024 * 1024
        self.concurrency = max(1, _cfg_int("OPENDIR_CONCURRENCY", 2))
        self.queue_chunks = max(1, _cfg_int("OPENDIR_QUEUE_CHUNKS", 8))
        self.chunk_size = max(64 * 1024, _cfg_int("OPENDIR_CHUNK_SIZE_BYTES", _DEFAULT_CHUNK_SIZE))
        self.interval_seconds = max(0.1, _cfg_float("OPENDIR_INTERVAL_HOURS", 6.0)) * 3600
        self.start_delay = max(0.0, _cfg_float("OPENDIR_START_DELAY_SECONDS", 20.0))
        self.user_agent = _cfg_str("OPENDIR_USER_AGENT", "TriadBot OpenDirSync/1.0")

        self.source_patterns = _cfg_list(
            "OPENDIR_SOURCE_PATTERNS",
            # Try common Open Directory layouts. The depot source normally exposes
            # files under Database/, while some mirrors expose files at root.
            "{appid}.{ext},"
            "{appid}/{appid}.{ext},"
            "Database/{appid}.{ext},"
            "Database/{target_filename},"
            "{target_filename},"
            "{safe_name}.{ext},"
            "{safe_name} ({appid}).{ext},"
            "Database/{safe_name} ({appid}).{ext}",
        )

        self.request_timeout = aiohttp.ClientTimeout(
            total=max(5.0, _cfg_float("OPENDIR_REQUEST_TIMEOUT_SECONDS", 300.0)),
            connect=max(3.0, _cfg_float("OPENDIR_CONNECT_TIMEOUT_SECONDS", 30.0)),
            sock_read=max(5.0, _cfg_float("OPENDIR_READ_TIMEOUT_SECONDS", 120.0)),
        )
        self.api_generate_timeout = aiohttp.ClientTimeout(
            total=max(30.0, _cfg_float("OPENDIR_API_REQUEST_TIMEOUT_SECONDS", _cfg_float("OPENDIR_REQUEST_TIMEOUT_SECONDS", 900.0))),
            connect=max(3.0, _cfg_float("OPENDIR_API_CONNECT_TIMEOUT_SECONDS", _cfg_float("OPENDIR_CONNECT_TIMEOUT_SECONDS", 30.0))),
            sock_read=max(30.0, _cfg_float("OPENDIR_API_READ_TIMEOUT_SECONDS", 300.0)),
        )
        self.priority_timeout_seconds = max(30.0, _cfg_float("OPENDIR_PRIORITY_TIMEOUT_SECONDS", 360.0))
        self.priority_api_generate_retries = max(
            1,
            _cfg_int("OPENDIR_PRIORITY_API_GENERATE_RETRIES", min(2, self.api_generate_retries)),
        )
        self.priority_api_generate_timeout = aiohttp.ClientTimeout(
            total=max(30.0, _cfg_float("OPENDIR_PRIORITY_API_REQUEST_TIMEOUT_SECONDS", 180.0)),
            connect=max(3.0, _cfg_float("OPENDIR_API_CONNECT_TIMEOUT_SECONDS", _cfg_float("OPENDIR_CONNECT_TIMEOUT_SECONDS", 30.0))),
            sock_read=max(30.0, _cfg_float("OPENDIR_PRIORITY_API_READ_TIMEOUT_SECONDS", 90.0)),
        )

        self.s3 = None
        multipart_size = max(5 * 1024 * 1024, self.chunk_size * 8)
        self.transfer_config = TransferConfig(
            multipart_threshold=multipart_size,
            multipart_chunksize=multipart_size,
            max_concurrency=1,
            use_threads=False,
        )

    def schedule_priority_sync(self, appid: str, *, source: str = "manual") -> bool:
        appid = str(appid or "").strip()
        if not appid.isdigit():
            return False

        self._priority_pending.add(appid)
        task = asyncio.create_task(
            self._run_scheduled_priority_sync(appid, source=source),
            name=f"opendir-priority-{appid}",
        )
        self._priority_tasks.add(task)
        task.add_done_callback(self._priority_tasks.discard)
        log.info("OpenDir priority sync scheduled for %s (source=%s)", appid, source)
        return True

    async def _run_scheduled_priority_sync(self, appid: str, *, source: str) -> None:
        try:
            log.info("OpenDir priority sync starting for %s (source=%s)", appid, source)
            summary = await asyncio.wait_for(
                self.run_sync_once(mode=source, priority_appid=appid),
                timeout=self.priority_timeout_seconds,
            )
            log.info(
                "OpenDir priority sync finished for %s: uploaded=%d existing=%d skipped=%d errors=%d elapsed=%.1fs",
                appid,
                summary.files_uploaded,
                summary.files_existing,
                summary.files_skipped,
                len(summary.errors),
                summary.elapsed_seconds,
            )
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError:
            log.warning(
                "OpenDir priority sync timed out for %s after %.0fs",
                appid,
                self.priority_timeout_seconds,
            )
            summary = SyncSummary(mode="targeted")
            summary.games_total = 1
            summary.games_checked = 1
            summary.files_skipped = 1
            summary.add_error(
                f"targeted appid {appid} timed out after {self.priority_timeout_seconds:.0f}s"
            )
            summary.add_sample("priority timed out while waiting for OpenDir API generate/upload")
            await self._report_summary(summary)
        except Exception:
            log.exception("OpenDir priority sync failed for %s", appid)
        finally:
            self._priority_pending.discard(appid)

    @asynccontextmanager
    async def _run_lock(self, *, priority: bool):
        if priority:
            async with self._priority_lock:
                yield
            return
        async with self._lock:
            yield

    async def cog_load(self) -> None:
        if not self.enabled:
            log.info("OpenDir SQLite sync disabled. Set OPENDIR_SYNC_ENABLED=true to enable.")
            return
        if self.source_mode == "api" and not self.api_base_url:
            log.warning("OpenDir API sync enabled but OPENDIR_API_BASE_URL/OPENDIR_BASE_URL is empty; task will not start.")
            return
        if self.source_mode == "directory" and not self.base_url:
            log.warning("OpenDir directory sync enabled but OPENDIR_BASE_URL is empty; task will not start.")
            return
        if not self._r2_configured():
            log.warning("OpenDir sync enabled but R2 credentials/bucket are incomplete; task will not start.")
            return

        base_host = urlparse(self.base_url).hostname
        if base_host and not self.allowed_hosts:
            self.allowed_hosts.add(base_host.lower())
        api_host = urlparse(self.api_base_url).hostname
        if api_host and not self.allowed_hosts:
            self.allowed_hosts.add(api_host.lower())

        self.s3 = self._make_r2_client()
        self._task = asyncio.create_task(self._sync_loop(), name="opendir-sqlite-sync-loop")
        log.info(
            "OpenDir SQLite sync task enabled for %s (mode=%s)",
            self._redact_url(self.api_base_url if self.source_mode == "api" else self.base_url),
            self.source_mode,
        )

    async def cog_unload(self) -> None:
        if not self._task:
            return
        self._task.cancel()
        with suppress(asyncio.CancelledError):
            await self._task

    async def _wait_for_sqlite_ready(self) -> bool:
        """Wait briefly for /data/games.db to contain game rows before first sync."""
        import sqlite3 as _sqlite3

        timeout = max(0.0, _cfg_float("OPENDIR_SQLITE_WAIT_TIMEOUT_SECONDS", 600.0))
        poll_interval = 5.0
        deadline = time.monotonic() + timeout

        while True:
            games_count = 0
            db_path = Path(_SQLITE_PATH) if _SQLITE_PATH else Path(
                getattr(bot_config, "SQLITE_PATH",
                        Path(getattr(bot_config, "DATA_DIR", Path("data"))) / "games.db")
            )
            if not db_path.is_absolute():
                db_path = Path(getattr(bot_config, "DATA_DIR", Path("data"))) / db_path

            try:
                with _sqlite3.connect(db_path) as conn:
                    games_count = int(conn.execute(
                        """
                        SELECT COUNT(*)
                        FROM games
                        WHERE appid IS NOT NULL
                          AND TRIM(CAST(appid AS TEXT)) != ''
                          AND name IS NOT NULL
                          AND TRIM(name) != ''
                        """
                    ).fetchone()[0])
            except Exception:
                games_count = 0

            if games_count > 0:
                log.info("OpenDir: SQLite ready with %d games — starting sync", games_count)
                return True

            if time.monotonic() >= deadline:
                log.warning("OpenDir: SQLite was not ready after %.0fs; continuing with normal error handling", timeout)
                return False

            await asyncio.sleep(poll_interval)

    async def _sync_loop(self) -> None:
        await self.bot.wait_until_ready()
        if self.start_delay > 0:
            await asyncio.sleep(self.start_delay)
        if not self.run_on_start:
            await asyncio.sleep(self.interval_seconds)

        if not self._initial_done:
            await self._wait_for_sqlite_ready()

        while not self.bot.is_closed():
            mode = "initial" if not self._initial_done else "monitor"
            try:
                summary = await self.run_sync_once(mode=mode)
                self._initial_done = True
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.exception("OpenDir SQLite sync loop failed")
                summary = SyncSummary(mode=mode)
                summary.add_error(repr(exc))
                await self._report_summary(summary)

            await asyncio.sleep(self.interval_seconds)

    async def run_sync_once(
        self,
        *,
        mode: str = "manual",
        priority_appid: str | None = None,
    ) -> SyncSummary:
        """
        Run one sync pass.

        If *priority_appid* is set the cog does a fast targeted lookup for that
        single game only and returns immediately after — the regular cursor-based
        window is skipped entirely.  The caller is responsible for reporting the
        summary if needed; this method never calls _report_summary internally so
        the loop can handle reporting consistently.
        """
        async with self._run_lock(priority=priority_appid is not None):
            summary = SyncSummary(mode="targeted" if priority_appid else mode)
            state = self._load_state()

            # ── Ensure R2 SQLite cache is fresh before any key lookups ──
            await self._ensure_r2_cache()

            existing_keys = await self._list_r2_keys()

            connector = aiohttp.TCPConnector(
                limit=max(4, self.concurrency * 2),
                ttl_dns_cache=300,
            )
            async with aiohttp.ClientSession(
                timeout=self.request_timeout,
                connector=connector,
                headers={"User-Agent": self.user_agent},
                raise_for_status=False,
            ) as session:

                # ── Index scan (shared for both modes) ──────────────────
                indexed_files: list[RemoteFile] = []
                if self.source_mode == "directory" and self.index_scan_enabled:
                    try:
                        indexed_files = await self._scan_index(session, summary)
                    except Exception as exc:
                        summary.add_error(f"index scan error: {exc!r}")
                summary.indexed_files = len(indexed_files)
                indexed_by_name = self._build_remote_index(indexed_files)

                # ── TARGETED PRIORITY SYNC ──────────────────────────────
                if priority_appid:
                    game_record = None
                    try:
                        raw = self.bot.db.get_game(priority_appid)
                        if raw:
                            appid = str(raw.get("appid") or raw.get("id") or "").strip()
                            name = str(raw.get("name") or raw.get("title") or "").strip()
                            if appid and name:
                                game_record = GameRecord(appid=appid, name=name)
                    except Exception as exc:
                        summary.add_error(f"db lookup failed for {priority_appid}: {exc!r}")

                    if game_record:
                        log.info("⚡ TARGETED SYNC: %s (%s)", game_record.name, game_record.appid)
                        summary.games_total = 1
                        summary.games_checked = 1
                        await self._sync_one_game(
                            session, game_record, indexed_by_name, existing_keys, summary, priority=True
                        )
                    else:
                        summary.add_error(f"targeted appid {priority_appid!r} not found in DB")

                    # Invalidate cache if we uploaded anything
                    if summary.files_uploaded:
                        try:
                            invalidate_r2_inventory_cache()
                        except Exception:
                            pass

                    await self._report_summary(summary)
                    return summary

                # ── NORMAL CURSOR-BASED SYNC ────────────────────────────
                games = self._load_games()
                summary.games_total = len(games)
                summary.cursor_start = (
                    min(max(0, state.cursor), max(0, len(games) - 1)) if games else 0
                )

                if not games:
                    summary.add_error("SQLite games table has no valid appid/name records")
                    await self._report_summary(summary)
                    return summary

                await self._sync_games_window(
                    session, games, indexed_by_name, existing_keys, state, summary
                )

            if summary.files_uploaded:
                try:
                    invalidate_r2_inventory_cache()
                except Exception:
                    pass

            state.last_run_at = time.time()
            self._save_state(state)
            await self._report_summary(summary)
            return summary

    async def _sync_games_window(
        self,
        session: aiohttp.ClientSession,
        games: list[GameRecord],
        indexed_by_name: dict[str, list[RemoteFile]],
        existing_keys: set[str],
        state: SyncState,
        summary: SyncSummary,
    ) -> None:
        total = len(games)
        start = min(max(0, state.cursor), total - 1)
        max_games = total if self.max_games_per_run <= 0 else min(self.max_games_per_run, total)
        checked = 0
        current = start

        while checked < max_games:
            game = games[current]
            summary.games_checked += 1
            checked += 1

            if checked % 100 == 0:
                log.info(
                    "🔍 OpenDir progress: checked %d/%d games (AppID: %s)",
                    checked, max_games, game.appid,
                )

            await self._sync_one_game(session, game, indexed_by_name, existing_keys, summary)

            current = (current + 1) % total

            if self.max_files_per_run and summary.files_uploaded >= self.max_files_per_run:
                log.info(
                    "OpenDir: reached max_files_per_run=%d — stopping window early",
                    self.max_files_per_run,
                )
                break
            if self._priority_pending:
                pending = ", ".join(sorted(self._priority_pending)[:5])
                log.info(
                    "OpenDir: priority sync pending (%s) - pausing normal window",
                    pending,
                )
                summary.add_sample(f"priority pending: {pending}; normal window paused")
                break
            if current == start:
                # Wrapped around — full cycle done
                break

        state.cursor = current
        summary.cursor_next = current

        # Full cycle completed if we wrapped around or covered every game
        if checked >= total or current == start:
            state.completed_cycles += 1
            summary.full_cycle_completed = True

    async def _sync_one_game(
        self,
        session: aiohttp.ClientSession,
        game: GameRecord,
        indexed_by_name: dict[str, list[RemoteFile]],
        existing_keys: set[str],
        summary: SyncSummary,
        *,
        priority: bool = False,
    ) -> None:
        for ext in sorted(self.target_extensions):
            target_key = self._target_key(game, ext)
            if target_key in existing_keys:
                summary.files_existing += 1
                continue

            if self.source_mode == "api":
                if ext.lower().lstrip(".") != "zip":
                    summary.files_skipped += 1
                    continue
                await self._sync_one_game_from_api(
                    session,
                    game,
                    target_key,
                    existing_keys,
                    summary,
                    priority=priority,
                )
                continue

            remote = self._match_indexed_file(game, ext, target_key, indexed_by_name)
            if remote is None and self.direct_probe_enabled:
                remote = await self._probe_game_candidates(session, game, ext, target_key, summary)

            if remote is None:
                summary.no_match += 1
                continue

            remote = replace(
                remote,
                target_key=target_key,
                appid=game.appid,
                game_name=game.name,
                extension=ext,
            )
            summary.remote_matches += 1
            await self._upload_if_needed(session, remote, existing_keys, summary)

    async def _sync_one_game_from_api(
        self,
        session: aiohttp.ClientSession,
        game: GameRecord,
        target_key: str,
        existing_keys: set[str],
        summary: SyncSummary,
        *,
        priority: bool = False,
    ) -> None:
        summary.files_seen += 1
        if target_key in existing_keys:
            summary.files_existing += 1
            if priority:
                log.info("OpenDir priority %s: target already exists at %s", game.appid, target_key)
            return

        try:
            if priority:
                log.info("OpenDir priority %s: API lookup started", game.appid)
            game_info = await self._api_lookup_game(session, game, summary)
            if self.api_lookup_before_generate and game_info is None:
                if priority:
                    log.info("OpenDir priority %s: API lookup found no downloadable game", game.appid)
                summary.no_match += 1
                return

            name = str((game_info or {}).get("name") or game.name).strip() or game.name
            depot_id = str((game_info or {}).get("depot_id") or self._default_depot_id(game.appid))
            remote_file = RemoteFile(
                url=self._api_url(self.api_generate_path),
                relative_path=f"api:{game.appid}",
                filename=f"{game.appid}.zip",
                target_key=target_key,
                appid=game.appid,
                game_name=name,
                extension="zip",
            )

            payload = {
                "app_id": game.appid,
                "depot_id": depot_id,
                "manifest_id": self.api_default_manifest_id,
                "depot_key": self.api_depot_key,
                "branch": self.api_branch,
                "game_name": name,
                "use_ryuu_api": self.api_use_ryuu_api,
            }

            summary.candidates_checked += 1
            if priority:
                log.info(
                    "OpenDir priority %s: API generate started (depot=%s, target=%s)",
                    game.appid,
                    depot_id,
                    target_key,
                )
            data = await self._download_api_zip(session, payload, summary, priority=priority)
            if data is None:
                if priority:
                    log.info("OpenDir priority %s: API generate returned no ZIP", game.appid)
                summary.no_match += 1
                return

            summary.remote_matches += 1
            if priority:
                log.info(
                    "OpenDir priority %s: API generate returned %d bytes; uploading to R2",
                    game.appid,
                    len(data),
                )
            uploaded_bytes, cleaned_files = await self._upload_api_zip_bytes(
                data,
                target_key,
                remote_file,
            )
            existing_keys.add(target_key)

            if _r2_inventory_db is not None:
                _r2_inventory_db.mark_uploaded(target_key, uploaded_bytes)

            summary.files_uploaded += 1
            summary.bytes_uploaded += uploaded_bytes
            if cleaned_files:
                summary.cleaned_objects += 1
                summary.cleaned_files += cleaned_files
            sample = f"api+upload {game.appid} -> {target_key}"
            if cleaned_files:
                sample += f" (cleaned {cleaned_files})"
            summary.add_sample(sample)
            log.info("OpenDir API uploaded %s -> %s (%d bytes)", game.appid, target_key, uploaded_bytes)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            summary.files_skipped += 1
            summary.add_error(f"{game.appid}/api-generate: {exc!r}")
            log.warning("OpenDir API failed for %s (%s): %r", game.name, game.appid, exc)

    def _api_url(self, path: str) -> str:
        path = (path or "").strip() or "/"
        if path.startswith(("http://", "https://")):
            return path
        return urljoin(self.api_base_url, path.lstrip("/"))

    async def _api_lookup_game(
        self,
        session: aiohttp.ClientSession,
        game: GameRecord,
        summary: SyncSummary,
    ) -> dict[str, Any] | None:
        if not self.api_lookup_before_generate:
            return None

        url = self._api_url(self.api_search_path)
        async with session.get(
            url,
            params={"q": game.appid},
            headers={"Accept": "application/json"},
            allow_redirects=True,
        ) as response:
            text = await response.text(errors="replace")
            if response.status >= 500:
                raise OpenDirSyncError(
                    f"API search failed with HTTP {response.status}: {self._short_error_text(text)}"
                )
            if response.status >= 400:
                return None
            if self._looks_like_html(response.headers.get("Content-Type", "")) or self._looks_like_html_text(text):
                raise OpenDirSyncError("API search returned HTML instead of JSON")
            try:
                data = json.loads(text)
            except Exception as exc:
                raise OpenDirSyncError(f"API search returned invalid JSON: {exc}") from exc

        if data.get("success") and isinstance(data.get("game"), dict):
            return data["game"]
        error = str(data.get("error") or data.get("message") or "").strip()
        if error and len(summary.samples) < 3:
            summary.add_sample(f"api no match {game.appid}: {error[:120]}")
        return None

    async def _download_api_zip(
        self,
        session: aiohttp.ClientSession,
        payload: dict[str, Any],
        summary: SyncSummary,
        *,
        priority: bool = False,
    ) -> bytes | None:
        appid = str(payload.get("app_id") or "")
        last_exc: BaseException | None = None
        retries = self.priority_api_generate_retries if priority else self.api_generate_retries
        timeout = self.priority_api_generate_timeout if priority else self.api_generate_timeout
        retry_delay = self.api_retry_delay
        for attempt in range(1, retries + 1):
            try:
                log.info(
                    "OpenDir API%s generate attempt %d/%d for appid=%s",
                    " priority" if priority else "",
                    attempt,
                    retries,
                    appid,
                )
                return await self._download_api_zip_once(
                    session,
                    payload,
                    summary,
                    timeout=timeout,
                )
            except (
                asyncio.TimeoutError,
                TimeoutError,
                aiohttp.ServerTimeoutError,
                aiohttp.ClientPayloadError,
                aiohttp.ClientOSError,
                aiohttp.ClientConnectionError,
            ) as exc:
                last_exc = exc
                if attempt < retries:
                    log.info(
                        "OpenDir API%s generate retry for appid=%s after %s: %r",
                        " priority" if priority else "",
                        appid,
                        type(exc).__name__,
                        exc,
                    )
                    await asyncio.sleep(retry_delay * attempt)
                    continue

        summary.api_transient_failures += 1
        summary.add_sample(
            f"api timeout {appid}: {type(last_exc).__name__ if last_exc else 'timeout'}; retry next run"
        )
        log.info(
            "OpenDir API transient timeout for appid=%s after %d attempt(s): %r",
            appid,
            retries,
            last_exc,
        )
        return None

    async def _download_api_zip_once(
        self,
        session: aiohttp.ClientSession,
        payload: dict[str, Any],
        summary: SyncSummary,
        *,
        timeout: aiohttp.ClientTimeout,
    ) -> bytes | None:
        url = self._api_url(self.api_generate_path)
        async with session.post(
            url,
            json=payload,
            headers={"Accept": "application/zip, application/octet-stream, application/json"},
            allow_redirects=True,
            timeout=timeout,
        ) as response:
            content_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
            if response.status >= 400:
                text = await response.text(errors="replace")
                if response.status >= 500:
                    raise OpenDirSyncError(
                        f"API generate failed with HTTP {response.status}: {self._short_error_text(text)}"
                    )
                if len(summary.samples) < 3:
                    summary.add_sample(
                        f"api no zip {payload.get('app_id')}: HTTP {response.status} {self._short_error_text(text)[:120]}"
                    )
                return None

            if self._looks_like_html(content_type):
                text = await response.text(errors="replace")
                raise OpenDirSyncError(
                    f"API generate returned HTML instead of ZIP: {self._short_error_text(text)}"
                )

            if content_type == "application/json" or content_type.endswith("+json"):
                text = await response.text(errors="replace")
                try:
                    data = json.loads(text)
                except Exception:
                    raise OpenDirSyncError(f"API generate returned invalid JSON: {self._short_error_text(text)}")
                if data.get("success") is False or data.get("error") or data.get("message"):
                    if len(summary.samples) < 3:
                        summary.add_sample(
                            f"api no zip {payload.get('app_id')}: {str(data.get('error') or data.get('message'))[:120]}"
                        )
                    return None
                raise OpenDirSyncError("API generate returned JSON, not a ZIP blob")

            raw_size = response.headers.get("Content-Length")
            if raw_size and raw_size.isdigit():
                declared = int(raw_size)
                if declared > self.max_file_bytes:
                    raise OpenDirSyncError(f"API ZIP exceeds OPENDIR_MAX_FILE_MB ({declared} bytes)")
                if declared > self.api_buffer_max_bytes:
                    raise OpenDirSyncError(f"API ZIP exceeds OPENDIR_API_BUFFER_MAX_MB ({declared} bytes)")

            chunks: list[bytes] = []
            total = 0
            async for chunk in response.content.iter_chunked(self.chunk_size):
                if not chunk:
                    continue
                total += len(chunk)
                if total > self.max_file_bytes:
                    raise OpenDirSyncError(f"API ZIP exceeds OPENDIR_MAX_FILE_MB ({total} bytes)")
                if total > self.api_buffer_max_bytes:
                    raise OpenDirSyncError(f"API ZIP exceeds OPENDIR_API_BUFFER_MAX_MB ({total} bytes)")
                chunks.append(chunk)

        if not chunks:
            raise OpenDirSyncError("API generate returned an empty ZIP response")
        return b"".join(chunks)

    async def _upload_api_zip_bytes(
        self,
        data: bytes,
        r2_key: str,
        remote_file: RemoteFile,
    ) -> tuple[int, int]:
        cleaned_files = 0
        body = data
        if self.api_clean_before_upload and clean_zip_comments is not None:
            result = clean_zip_comments(data)
            body = result.data
            cleaned_files = int(getattr(result, "files_cleaned", 0) or 0)
        elif self.api_clean_before_upload and clean_zip_comments is None:
            log.warning("OpenDir API clean-before-upload requested but r2_maintenance cleaner is unavailable")

        await asyncio.to_thread(self._put_bytes_to_r2_sync, body, r2_key, remote_file)
        return len(body), cleaned_files

    def _put_bytes_to_r2_sync(self, data: bytes, r2_key: str, remote_file: RemoteFile) -> None:
        if self.s3 is None:
            raise OpenDirSyncError("R2 client is not initialized")
        self.s3.put_object(
            Bucket=_cfg_str("R2_BUCKET_NAME"),
            Key=r2_key,
            Body=data,
            ContentType="application/zip",
            Metadata={
                "source": "opendir-api-sync",
                "synced-by": "triadbot",
                "appid": _safe_metadata_value(remote_file.appid, max_len=40),
                "game-name": _safe_metadata_value(remote_file.game_name),
            },
        )

    @staticmethod
    def _default_depot_id(appid: str) -> str:
        try:
            return str(int(appid) + 1)
        except Exception:
            return str(appid)

    @staticmethod
    def _looks_like_html_text(text: str) -> bool:
        sample = str(text or "").strip().lower()[:120]
        return sample.startswith("<!doctype") or sample.startswith("<html") or "<body" in sample

    @staticmethod
    def _short_error_text(text: str) -> str:
        safe = str(text or "").replace("\r", " ").replace("\n", " ").strip()
        return safe[:240] or "(empty response)"

    def _load_games(self) -> list[GameRecord]:
        """
        Load the OpenDir candidate list from the persistent SQLite database.

        SQLite is the single runtime source of truth for appid/name records.
        """
        import sqlite3 as _sqlite3

        db_path = Path(_SQLITE_PATH) if _SQLITE_PATH else Path(
            getattr(bot_config, "SQLITE_PATH",
                    Path(getattr(bot_config, "DATA_DIR", Path("data"))) / "games.db")
        )
        if not db_path.is_absolute():
            db_path = Path(getattr(bot_config, "DATA_DIR", Path("data"))) / db_path

        records: list[GameRecord] = []
        seen: set[str] = set()

        try:
            with _sqlite3.connect(db_path) as conn:
                conn.row_factory = _sqlite3.Row
                rows = conn.execute(
                    """
                    SELECT appid, name
                    FROM games
                    WHERE appid IS NOT NULL
                      AND TRIM(CAST(appid AS TEXT)) != ''
                      AND name IS NOT NULL
                      AND TRIM(name) != ''
                    ORDER BY CAST(appid AS INTEGER) ASC
                    """
                ).fetchall()

            for row in rows:
                appid = str(row["appid"] or "").strip()
                name = str(row["name"] or "").strip()
                if not appid.isdigit() or not name or appid in seen:
                    continue
                seen.add(appid)
                records.append(GameRecord(appid=appid, name=name))

            if records:
                log.info("OpenDir loaded %d games from SQLite: %s", len(records), db_path)
                return records

            log.warning("OpenDir: SQLite games table is empty at %s", db_path)
            return []
        except Exception as exc:
            log.exception("OpenDir: failed to load games from SQLite at %s: %s", db_path, exc)
            return []

    def _load_state(self) -> SyncState:
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
            return SyncState(
                cursor=max(0, int(data.get("cursor", 0))),
                completed_cycles=max(0, int(data.get("completed_cycles", 0))),
                last_run_at=float(data.get("last_run_at", 0.0)),
            )
        except Exception:
            return SyncState()

    def _save_state(self, state: SyncState) -> None:
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "cursor": state.cursor,
                "completed_cycles": state.completed_cycles,
                "last_run_at": state.last_run_at,
            }
            self.state_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        except Exception as exc:
            log.warning("Cannot save OpenDir sync state: %r", exc)

    async def _scan_index(self, session: aiohttp.ClientSession, summary: SyncSummary) -> list[RemoteFile]:
        visited: set[str] = set()
        return await self._scan_directory(session, self.base_url, depth=0, summary=summary, visited_dirs=visited)

    async def _scan_directory(
        self,
        session: aiohttp.ClientSession,
        directory_url: str,
        *,
        depth: int,
        summary: SyncSummary,
        visited_dirs: set[str],
    ) -> list[RemoteFile]:
        if depth > self.max_depth:
            return []

        directory_url = self._normalize_directory_url(directory_url)
        if directory_url in visited_dirs:
            return []
        visited_dirs.add(directory_url)

        try:
            html = await self._fetch_text(session, directory_url)
        except Exception as exc:
            summary.add_error(f"index scan failed at {self._redact_url(directory_url)}: {exc!r}")
            return []

        soup = BeautifulSoup(html, "html.parser")
        summary.directories_scanned += 1

        child_dirs: list[str] = []
        files: list[RemoteFile] = []

        for href in self._iter_hrefs(soup):
            resolved = self._resolve_in_scope(directory_url, href)
            if not resolved:
                continue

            parsed_path = urlparse(resolved).path
            is_dir = resolved.endswith("/") or parsed_path.endswith("/")
            if is_dir:
                if depth < self.max_depth:
                    child_dirs.append(self._normalize_directory_url(resolved))
                continue

            rel = self._relative_path_from_url(resolved)
            if not rel:
                continue

            filename = posixpath.basename(rel)
            extension = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
            if self.allowed_extensions and extension not in self.allowed_extensions:
                continue

            files.append(
                RemoteFile(
                    url=resolved,
                    relative_path=rel,
                    filename=filename,
                    target_key="",
                    appid="",
                    game_name="",
                    extension=extension,
                )
            )

        if child_dirs:
            nested = await asyncio.gather(
                *(
                    self._scan_directory(
                        session, url, depth=depth + 1, summary=summary, visited_dirs=visited_dirs
                    )
                    for url in child_dirs
                ),
                return_exceptions=True,
            )
            for item in nested:
                if isinstance(item, BaseException):
                    summary.add_error(f"directory scan failed: {item!r}")
                else:
                    files.extend(item)

        return files

    def _build_remote_index(self, files: list[RemoteFile]) -> dict[str, list[RemoteFile]]:
        index: dict[str, list[RemoteFile]] = {}
        for remote in files:
            tokens = {remote.filename.lower(), posixpath.basename(remote.relative_path).lower()}
            stem = remote.filename.rsplit(".", 1)[0].lower()
            tokens.add(stem)
            for appid in _APPID_RE.findall(remote.filename):
                tokens.add(appid)
                tokens.add(f"{appid}.{remote.extension}")
            for token in tokens:
                index.setdefault(token, []).append(remote)
        return index

    def _match_indexed_file(
        self,
        game: GameRecord,
        ext: str,
        target_key: str,
        indexed_by_name: dict[str, list[RemoteFile]],
    ) -> RemoteFile | None:
        target_filename = posixpath.basename(target_key)
        names = [
            f"{game.appid}.{ext}",
            f"[{game.appid}].{ext}",
            f"{game.safe_name}.{ext}",
            f"{game.safe_name} ({game.appid}).{ext}",
            target_filename,
            game.appid,
        ]

        for name in names:
            candidates = indexed_by_name.get(name.lower())
            if not candidates:
                continue
            for remote in candidates:
                if remote.extension.lower() == ext:
                    return replace(remote, target_key=target_key, appid=game.appid, game_name=game.name)

        return None

    async def _probe_game_candidates(
        self,
        session: aiohttp.ClientSession,
        game: GameRecord,
        ext: str,
        target_key: str,
        summary: SyncSummary,
    ) -> RemoteFile | None:
        seen: set[str] = set()
        target_filename = posixpath.basename(target_key)
        for pattern in self.source_patterns:
            raw_path = self._format_source_pattern(pattern, game, ext, target_filename)
            if not raw_path:
                continue
            url = self._join_source_url(raw_path)
            if not url or url in seen:
                continue
            seen.add(url)
            summary.candidates_checked += 1
            found = await self._probe_source_url(session, url, game, ext, target_key)
            if found:
                return found
        return None

    def _format_source_pattern(self, pattern: str, game: GameRecord, ext: str, target_filename: str) -> str:
        try:
            return pattern.format(
                appid=game.appid,
                id=game.appid,
                name=game.name,
                safe_name=game.safe_name,
                ext=ext,
                target_filename=target_filename,
            ).strip().lstrip("/")
        except Exception:
            return ""

    def _join_source_url(self, raw_path: str) -> str | None:
        if raw_path.startswith(("http://", "https://")):
            url = raw_path
        else:
            encoded = "/".join(quote(unquote(part), safe="-_.()[]") for part in raw_path.split("/"))
            url = urljoin(self.base_url, encoded)
        return self._resolve_in_scope(self.base_url, url)

    async def _probe_source_url(
        self,
        session: aiohttp.ClientSession,
        url: str,
        game: GameRecord,
        ext: str,
        target_key: str,
    ) -> RemoteFile | None:
        size: int | None = None

        if self.use_head:
            try:
                async with session.head(url, allow_redirects=True) as response:
                    if response.status in {200, 206}:
                        content_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
                        if self._looks_like_html(content_type):
                            return None
                        raw_size = response.headers.get("Content-Length")
                        size = int(raw_size) if raw_size and raw_size.isdigit() else None
                        if size is not None and size > self.max_file_bytes:
                            return None
                        return self._remote_from_url(url, game, ext, target_key, size=size)
                    if response.status not in {403, 405, 406}:
                        return None
            except Exception:
                pass

        if not self.fallback_get_probe:
            return None

        try:
            async with session.get(url, headers={"Range": "bytes=0-0"}, allow_redirects=True) as response:
                if response.status not in {200, 206}:
                    return None
                content_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
                if self._looks_like_html(content_type):
                    return None
                raw_size = response.headers.get("Content-Range") or response.headers.get("Content-Length")
                size = self._parse_size_header(raw_size)
                if size is not None and size > self.max_file_bytes:
                    return None
                await response.content.read(1)
                return self._remote_from_url(url, game, ext, target_key, size=size)
        except Exception:
            return None

    def _remote_from_url(
        self,
        url: str,
        game: GameRecord,
        ext: str,
        target_key: str,
        *,
        size: int | None = None,
    ) -> RemoteFile:
        rel = self._relative_path_from_url(url) or posixpath.basename(urlparse(url).path)
        filename = posixpath.basename(rel) or f"{game.appid}.{ext}"
        return RemoteFile(
            url=url,
            relative_path=rel,
            filename=filename,
            target_key=target_key,
            appid=game.appid,
            game_name=game.name,
            extension=ext,
            size=size,
        )

    async def _fetch_text(self, session: aiohttp.ClientSession, url: str) -> str:
        async with session.get(url, allow_redirects=True) as response:
            if response.status >= 400:
                raise OpenDirSyncError(f"GET {self._redact_url(url)} failed with HTTP {response.status}")
            return await response.text(errors="replace")

    async def _upload_if_needed(
        self,
        session: aiohttp.ClientSession,
        remote_file: RemoteFile,
        existing_keys: set[str],
        summary: SyncSummary,
    ) -> None:
        summary.files_seen += 1
        r2_key = remote_file.target_key
        if not r2_key:
            summary.files_skipped += 1
            summary.add_error(f"missing target key for {remote_file.relative_path}")
            return

        if r2_key in existing_keys:
            summary.files_existing += 1
            return

        try:
            if remote_file.size is None:
                remote_file.size = await self._remote_size(session, remote_file.url)
            if remote_file.size is not None and remote_file.size > self.max_file_bytes:
                summary.files_skipped += 1
                summary.add_error(
                    f"skip too large: {remote_file.relative_path} ({remote_file.size} bytes)"
                )
                return

            uploaded_bytes = await self._stream_url_to_r2(session, remote_file.url, r2_key, remote_file)
            existing_keys.add(r2_key)

            # Keep SQLite R2 cache in sync immediately after upload
            if _r2_inventory_db is not None:
                _r2_inventory_db.mark_uploaded(r2_key, uploaded_bytes)

            summary.files_uploaded += 1
            summary.bytes_uploaded += uploaded_bytes
            summary.add_sample(f"uploaded {remote_file.filename} -> {r2_key}")
            log.info(
                "OpenDir uploaded %s -> %s (%d bytes)",
                remote_file.url, r2_key, uploaded_bytes,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            summary.files_skipped += 1
            summary.add_error(f"{remote_file.appid}/{remote_file.filename}: {exc!r}")
            log.warning("OpenDir failed to upload %s: %r", remote_file.url, exc)

    async def _remote_size(self, session: aiohttp.ClientSession, url: str) -> int | None:
        if not self.use_head:
            return None
        try:
            async with session.head(url, allow_redirects=True) as response:
                if response.status >= 400:
                    return None
                raw = response.headers.get("Content-Length")
                return int(raw) if raw and raw.isdigit() else None
        except Exception:
            return None

    async def _queue_put_abortable(
        self,
        q: "queue.Queue[bytes | object | BaseException]",
        item: bytes | object | BaseException,
        upload_task: asyncio.Task,
    ) -> None:
        while True:
            if upload_task.done():
                await upload_task
                return
            try:
                await asyncio.to_thread(q.put, item, True, 0.5)
                return
            except queue.Full:
                continue

    async def _stream_url_to_r2(
        self,
        session: aiohttp.ClientSession,
        url: str,
        r2_key: str,
        remote_file: RemoteFile,
    ) -> int:
        if self.s3 is None:
            raise OpenDirSyncError("R2 client is not initialized")

        q: "queue.Queue[bytes | object | BaseException]" = queue.Queue(maxsize=self.queue_chunks)
        stream = QueueReadStream(q)
        upload_task = asyncio.create_task(
            asyncio.to_thread(self._upload_stream_sync, stream, r2_key, remote_file)
        )
        total = 0

        try:
            async with session.get(url, allow_redirects=True) as response:
                if response.status >= 400:
                    raise OpenDirSyncError(f"GET file failed with HTTP {response.status}")
                content_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
                if self._looks_like_html(content_type):
                    raise OpenDirSyncError(
                        f"source returned HTML instead of .{remote_file.extension} file"
                    )

                async for chunk in response.content.iter_chunked(self.chunk_size):
                    if not chunk:
                        continue
                    total += len(chunk)
                    if total > self.max_file_bytes:
                        raise OpenDirSyncError(
                            f"file exceeds OPENDIR_MAX_FILE_MB={_cfg_int('OPENDIR_MAX_FILE_MB', 1024)}"
                        )
                    await self._queue_put_abortable(q, chunk, upload_task)
        except BaseException as exc:
            with suppress(Exception):
                await self._queue_put_abortable(q, exc, upload_task)
            with suppress(Exception):
                await upload_task
            raise
        else:
            await self._queue_put_abortable(q, _SENTINEL, upload_task)
            await upload_task
            return total

    def _upload_stream_sync(self, stream: QueueReadStream, r2_key: str, remote_file: RemoteFile) -> None:
        assert self.s3 is not None
        extra_args = {
            "Metadata": {
                "source": "opendir-sqlite-sync",
                "synced-by": "triadbot",
                "appid": _safe_metadata_value(remote_file.appid, max_len=40),
                "game-name": _safe_metadata_value(remote_file.game_name),
            }
        }
        self.s3.upload_fileobj(
            stream,
            _cfg_str("R2_BUCKET_NAME"),
            r2_key,
            ExtraArgs=extra_args,
            Config=self.transfer_config,
        )

    # ── R2 key cache (SQLite-backed) ──────────────────────────────────────────

    _R2_CACHE_TTL = max(3600.0, _cfg_float("OPENDIR_R2_CACHE_TTL_HOURS", 12.0) * 3600)

    async def _ensure_r2_cache(self) -> None:
        """
        Rebuild the R2 → SQLite inventory if it is stale or empty.
        Called once at the start of every sync run so all subsequent
        ``contains()`` / ``get_all_keys()`` calls are pure SQLite.
        """
        if _r2_inventory_db is None or self.s3 is None:
            return
        last = _r2_inventory_db.last_synced_at(self.r2_prefix)
        age = time.time() - last
        count = _r2_inventory_db.count(self.r2_prefix)
        if count == 0 or age > self._R2_CACHE_TTL:
            log.info(
                "OpenDir: R2 SQLite cache is %s (age=%.0fs, count=%d) — rebuilding from R2 …",
                "empty" if count == 0 else "stale",
                age,
                count,
            )
            bucket = _cfg_str("R2_BUCKET_NAME")
            summary_info = await asyncio.to_thread(
                _r2_inventory_db.rebuild, self.s3, bucket, self.r2_prefix
            )
            log.info("OpenDir: R2 cache rebuilt — %s", summary_info)
        else:
            log.debug(
                "OpenDir: R2 SQLite cache OK (age=%.0fs, count=%d keys)", age, count
            )

    async def _list_r2_keys(self) -> set[str]:
        """
        Return the set of existing R2 keys.

        Uses the SQLite-backed cache (R2InventoryDB) so we avoid listing R2 on
        every sync run.  Falls back to a live R2 list only when the cache is
        unavailable.
        """
        if _r2_inventory_db is not None:
            keys = _r2_inventory_db.get_all_keys(self.r2_prefix)
            log.debug("OpenDir: loaded %d existing keys from SQLite cache", len(keys))
            return keys
        log.warning("OpenDir: R2InventoryDB not available — falling back to live R2 list")
        return await asyncio.to_thread(self._list_r2_keys_sync)

    def _list_r2_keys_sync(self) -> set[str]:
        if self.s3 is None:
            raise OpenDirSyncError("R2 client is not initialized")
        keys: set[str] = set()
        paginator = self.s3.get_paginator("list_objects_v2")
        kwargs = {"Bucket": _cfg_str("R2_BUCKET_NAME"), "Prefix": self.r2_prefix}
        for page in paginator.paginate(**kwargs):
            for item in page.get("Contents", []):
                key = str(item.get("Key") or "")
                if key:
                    keys.add(key)
        return keys

    async def _report_summary(self, summary: SyncSummary) -> None:
        self.bot.last_opendir_sync_summary = summary

        if hasattr(self.bot, "record_ai_event"):
            self.bot.record_ai_event(
                "error" if summary.has_errors else "info",
                "opendir_sync",
                "OpenDir SQLite sync finished.",
                {
                    "fields": summary.to_fields(),
                    "errors": summary.errors[:10],
                    "samples": summary.samples[:10],
                },
            )

        if hasattr(self.bot, "queue_ai_caretaker"):
            self.bot.queue_ai_caretaker(
                "opendir-sync-finished",
                {
                    "errors": len(summary.errors),
                    "uploaded": summary.files_uploaded,
                    "games_checked": summary.games_checked,
                    "mode": summary.mode,
                    "full_cycle_completed": summary.full_cycle_completed,
                },
                force=summary.has_errors,
            )

        notifier = getattr(self.bot, "notify_admins", None)
        if not notifier:
            return
        if not summary.has_errors and not self.notify_on_success and not summary.full_cycle_completed:
            return

        fields = summary.to_fields()
        if summary.samples:
            fields["Samples"] = "\n".join(summary.samples[:8])
        if summary.errors:
            fields["Error detail"] = "\n".join(summary.errors[:8])

        if summary.has_errors:
            title = "OpenDir SQLite sync needs attention"
        elif summary.full_cycle_completed:
            title = "OpenDir SQLite sync full cycle completed"
        else:
            title = "OpenDir SQLite sync completed"

        result = notifier(
            title,
            "The SQLite-driven Open Directory to R2 sync task finished.",
            level="error" if summary.has_errors else "info",
            fields=fields,
            key="opendir-sync-error" if summary.has_errors else "opendir-sync-ok",
            force=summary.has_errors or summary.full_cycle_completed,
        )
        if inspect.isawaitable(result):
            await result

    def _target_key(self, game: GameRecord, ext: str) -> str:
        safe_name = _safe_game_name(game.name, max_len=160)
        filename = f"{safe_name} ({game.appid}).{ext.lower().lstrip('.')}"
        return f"{self.r2_prefix}{filename}"

    def _iter_hrefs(self, soup: BeautifulSoup) -> Iterable[str]:
        for tag in soup.find_all("a", href=True):
            href = str(tag.get("href") or "").strip()
            if not href or href in {"../", "./", "/"}:
                continue
            lowered = href.lower()
            if lowered.startswith(("javascript:", "mailto:", "data:", "tel:")):
                continue
            yield href

    def _resolve_in_scope(self, current_url: str, href_or_url: str) -> str | None:
        clean_href, _fragment = urldefrag(href_or_url)
        resolved = (
            clean_href
            if clean_href.startswith(("http://", "https://"))
            else urljoin(current_url, clean_href)
        )
        parsed = urlparse(resolved)
        base = urlparse(self.base_url)

        if parsed.scheme not in {"http", "https"}:
            return None
        if parsed.netloc.lower() != base.netloc.lower():
            return None
        if self.allowed_hosts and parsed.hostname and parsed.hostname.lower() not in self.allowed_hosts:
            return None

        base_path = unquote(base.path if base.path.endswith("/") else f"{base.path}/")
        parsed_path = unquote(parsed.path)
        if base_path != "/" and not parsed_path.startswith(base_path):
            return None
        return resolved

    def _relative_path_from_url(self, url: str) -> str:
        base_path = unquote(urlparse(self.base_url).path)
        if not base_path.endswith("/"):
            base_path = f"{base_path}/"

        path = unquote(urlparse(url).path)
        if base_path != "/" and not path.startswith(base_path):
            return ""

        rel = path[len(base_path):].strip("/") if base_path != "/" else path.strip("/")
        rel = posixpath.normpath(rel).replace("\\", "/")
        if rel == "." or rel.startswith("../") or "/../" in rel:
            return ""
        return rel

    @staticmethod
    def _parse_size_header(raw: str | None) -> int | None:
        if not raw:
            return None
        raw = raw.strip()
        if "/" in raw:
            tail = raw.rsplit("/", 1)[-1]
            return int(tail) if tail.isdigit() else None
        return int(raw) if raw.isdigit() else None

    @staticmethod
    def _looks_like_html(content_type: str) -> bool:
        content_type = (content_type or "").split(";", 1)[0].strip().lower()
        return content_type in _HTML_CONTENT_TYPES or content_type.endswith("+html")

    def _make_r2_client(self):
        return boto3.client(
            "s3",
            endpoint_url=f"https://{_cfg_str('R2_ACCOUNT_ID')}.r2.cloudflarestorage.com",
            aws_access_key_id=_cfg_str("R2_ACCESS_KEY_ID"),
            aws_secret_access_key=_cfg_str("R2_SECRET_ACCESS_KEY"),
            region_name="auto",
        )

    def _r2_configured(self) -> bool:
        return all(
            [
                _cfg_str("R2_ACCOUNT_ID"),
                _cfg_str("R2_ACCESS_KEY_ID"),
                _cfg_str("R2_SECRET_ACCESS_KEY"),
                _cfg_str("R2_BUCKET_NAME"),
            ]
        )

    @staticmethod
    def _normalize_base_url(url: str) -> str:
        url = (url or "").strip()
        if not url:
            return ""
        if not url.startswith(("http://", "https://")):
            url = f"http://{url}"
        return url if url.endswith("/") else f"{url}/"

    @staticmethod
    def _normalize_directory_url(url: str) -> str:
        return url if url.endswith("/") else f"{url}/"

    @staticmethod
    def _normalize_r2_prefix(prefix: str) -> str:
        prefix = (prefix or "").strip().lstrip("/")
        if prefix and not prefix.endswith("/"):
            prefix = f"{prefix}/"
        return prefix

    @staticmethod
    def _redact_url(url: str) -> str:
        parsed = urlparse(url)
        if not parsed.scheme:
            return url
        return f"{parsed.scheme}://{parsed.netloc}{parsed.path}"


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(OpenDirSync(bot))
