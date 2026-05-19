"""
Open Directory -> Cloudflare R2 sync cog for TriadBot.

Purpose:
- Scan an authorized Open Directory.
- Upload missing files to Cloudflare R2.
- Run once on startup, then keep monitoring on a configured interval.
- Stream download -> upload through RAM only. No local file writes.

Safety defaults:
- Disabled unless OPENDIR_SYNC_ENABLED=true.
- Limited by max depth, max files per run, max file size, allowed extensions,
  host scope, request timeout, and upload concurrency.
"""
from __future__ import annotations

import asyncio
import inspect
import logging
import posixpath
import queue
import time
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any, Iterable
from urllib.parse import unquote, urldefrag, urljoin, urlparse

import aiohttp
import boto3
from boto3.s3.transfer import TransferConfig
from bs4 import BeautifulSoup
from discord.ext import commands

import config as bot_config

try:
    from utils.r2_inventory import invalidate_r2_inventory_cache
except Exception:  # pragma: no cover - keep cog loadable if helper is unavailable
    def invalidate_r2_inventory_cache() -> None:
        return None


log = logging.getLogger(__name__)

_SENTINEL = object()
_DEFAULT_CHUNK_SIZE = 1024 * 1024


class OpenDirSyncError(RuntimeError):
    """Recoverable sync failure."""


def _cfg(name: str, default: Any = None) -> Any:
    return getattr(bot_config, name, default)


def _cfg_bool(name: str, default: bool = False) -> bool:
    value = _cfg(name, default)
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on", "enable", "enabled"}


def _cfg_int(name: str, default: int) -> int:
    value = _cfg(name, default)
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _cfg_float(name: str, default: float) -> float:
    value = _cfg(name, default)
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _cfg_str(name: str, default: str = "") -> str:
    value = _cfg(name, default)
    return "" if value is None else str(value)


def _cfg_list(name: str, default: Iterable[str] | str = "") -> list[str]:
    value = _cfg(name, default)
    if value is None:
        return []
    if isinstance(value, str):
        return [part.strip() for part in value.split(",") if part.strip()]
    try:
        return [str(part).strip() for part in value if str(part).strip()]
    except TypeError:
        return [str(value).strip()] if str(value).strip() else []


class QueueReadStream:
    """
    Blocking file-like object backed by a queue.

    aiohttp runs in the asyncio event loop as producer.
    boto3 runs in a worker thread as consumer and calls .read().
    This avoids writing files to disk while still giving boto3 a sync stream.
    """

    def __init__(self, q: "queue.Queue[bytes | object | BaseException]") -> None:
        self._q = q
        self._buffer = bytearray()
        self._closed = False

    def readable(self) -> bool:
        return True

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
class RemoteFile:
    url: str
    relative_path: str
    filename: str
    size: int | None = None


@dataclass(slots=True)
class SyncSummary:
    mode: str
    started_at: float = field(default_factory=time.time)
    directories_scanned: int = 0
    files_seen: int = 0
    files_existing: int = 0
    files_uploaded: int = 0
    files_skipped: int = 0
    bytes_uploaded: int = 0
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
            "Directories": str(self.directories_scanned),
            "Files seen": str(self.files_seen),
            "Already existed": str(self.files_existing),
            "Uploaded": str(self.files_uploaded),
            "Skipped": str(self.files_skipped),
            "Bytes uploaded": str(self.bytes_uploaded),
            "Errors": str(len(self.errors)),
            "Elapsed": f"{self.elapsed_seconds:.1f}s",
        }


class OpenDirSync(commands.Cog):
    """Background Open Directory sync cog."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._task: asyncio.Task | None = None
        self._lock = asyncio.Lock()
        self._initial_done = False

        self.enabled = _cfg_bool("OPENDIR_SYNC_ENABLED", False)
        self.base_url = self._normalize_base_url(_cfg_str("OPENDIR_BASE_URL", ""))
        self.r2_prefix = self._normalize_r2_prefix(_cfg_str("OPENDIR_R2_PREFIX", "Database/"))

        allowed_extensions = _cfg_list("OPENDIR_ALLOWED_EXTENSIONS", "zip,manifest,lua,acf,vdf")
        self.allowed_extensions = {ext.lower().lstrip(".") for ext in allowed_extensions if ext}

        allowed_hosts = _cfg_list("OPENDIR_ALLOWED_HOSTS", "")
        self.allowed_hosts = {host.lower() for host in allowed_hosts if host}

        self.max_depth = max(0, _cfg_int("OPENDIR_MAX_DEPTH", 3))
        self.max_files_per_run = max(0, _cfg_int("OPENDIR_MAX_FILES_PER_RUN", 20))
        self.max_file_bytes = max(1, _cfg_int("OPENDIR_MAX_FILE_MB", 1024)) * 1024 * 1024
        self.concurrency = max(1, _cfg_int("OPENDIR_CONCURRENCY", 2))
        self.queue_chunks = max(1, _cfg_int("OPENDIR_QUEUE_CHUNKS", 8))
        self.chunk_size = max(64 * 1024, _cfg_int("OPENDIR_CHUNK_SIZE_BYTES", _DEFAULT_CHUNK_SIZE))
        self.interval_seconds = max(0.1, _cfg_float("OPENDIR_INTERVAL_HOURS", 6.0)) * 3600
        self.start_delay = max(0.0, _cfg_float("OPENDIR_START_DELAY_SECONDS", 20.0))
        self.run_on_start = _cfg_bool("OPENDIR_RUN_ON_START", True)
        self.use_head = _cfg_bool("OPENDIR_USE_HEAD", True)
        self.flatten_r2_keys = _cfg_bool("OPENDIR_FLATTEN_R2_KEYS", False)
        self.notify_on_success = _cfg_bool("OPENDIR_NOTIFY_ON_SUCCESS", False)
        self.user_agent = _cfg_str("OPENDIR_USER_AGENT", "TriadBot OpenDirSync/1.0")

        self.request_timeout = aiohttp.ClientTimeout(
            total=max(5.0, _cfg_float("OPENDIR_REQUEST_TIMEOUT_SECONDS", 120.0)),
            connect=max(3.0, _cfg_float("OPENDIR_CONNECT_TIMEOUT_SECONDS", 15.0)),
            sock_read=max(5.0, _cfg_float("OPENDIR_READ_TIMEOUT_SECONDS", 60.0)),
        )

        self.s3 = None
        multipart_size = max(5 * 1024 * 1024, self.chunk_size * 8)
        self.transfer_config = TransferConfig(
            multipart_threshold=multipart_size,
            multipart_chunksize=multipart_size,
            max_concurrency=1,
            use_threads=False,
        )

    async def cog_load(self) -> None:
        if not self.enabled:
            log.info("OpenDir sync disabled. Set OPENDIR_SYNC_ENABLED=true to enable.")
            return

        if not self.base_url:
            log.warning("OpenDir sync enabled but OPENDIR_BASE_URL is empty; task will not start.")
            return

        if not self._r2_configured():
            log.warning("OpenDir sync enabled but R2 credentials/bucket are incomplete; task will not start.")
            return

        base_host = urlparse(self.base_url).hostname
        if base_host and not self.allowed_hosts:
            # Default to same-host only. Extra hosts must be explicitly configured.
            self.allowed_hosts.add(base_host.lower())

        self.s3 = self._make_r2_client()
        self._task = asyncio.create_task(self._sync_loop(), name="opendir-sync-loop")
        log.info("OpenDir sync background task enabled for %s", self._redact_url(self.base_url))

    async def cog_unload(self) -> None:
        if not self._task:
            return
        self._task.cancel()
        with suppress(asyncio.CancelledError):
            await self._task

    async def _sync_loop(self) -> None:
        await self.bot.wait_until_ready()

        if self.start_delay > 0:
            await asyncio.sleep(self.start_delay)

        if not self.run_on_start:
            await asyncio.sleep(self.interval_seconds)

        while not self.bot.is_closed():
            mode = "initial" if not self._initial_done else "monitor"

            try:
                summary = await self.run_sync_once(mode=mode)
                self._initial_done = True
                await self._report_summary(summary)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.exception("OpenDir sync loop failed")
                summary = SyncSummary(mode=mode)
                summary.add_error(repr(exc))
                await self._report_summary(summary)

            await asyncio.sleep(self.interval_seconds)

    async def run_sync_once(self, *, mode: str = "manual") -> SyncSummary:
        async with self._lock:
            summary = SyncSummary(mode=mode)
            existing_keys = await self._list_r2_keys()

            connector = aiohttp.TCPConnector(limit=max(4, self.concurrency * 2), ttl_dns_cache=300)
            headers = {"User-Agent": self.user_agent}

            async with aiohttp.ClientSession(
                timeout=self.request_timeout,
                connector=connector,
                headers=headers,
                raise_for_status=False,
            ) as session:
                visited_dirs: set[str] = set()
                files = await self._scan_directory(
                    session,
                    self.base_url,
                    depth=0,
                    summary=summary,
                    visited_dirs=visited_dirs,
                )

                if self.max_files_per_run:
                    files = files[: self.max_files_per_run]

                semaphore = asyncio.Semaphore(self.concurrency)
                tasks = [
                    asyncio.create_task(
                        self._upload_if_needed(session, remote_file, existing_keys, semaphore, summary)
                    )
                    for remote_file in files
                ]

                if tasks:
                    await asyncio.gather(*tasks)

            if summary.files_uploaded:
                invalidate_r2_inventory_cache()

            return summary

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

        html = await self._fetch_text(session, directory_url)
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

            files.append(RemoteFile(url=resolved, relative_path=rel, filename=filename))

        if child_dirs:
            nested = await asyncio.gather(
                *(
                    self._scan_directory(
                        session,
                        child_url,
                        depth=depth + 1,
                        summary=summary,
                        visited_dirs=visited_dirs,
                    )
                    for child_url in child_dirs
                ),
                return_exceptions=True,
            )
            for item in nested:
                if isinstance(item, BaseException):
                    summary.add_error(f"directory scan failed: {item!r}")
                else:
                    files.extend(item)

        return files

    async def _fetch_text(self, session: aiohttp.ClientSession, url: str) -> str:
        async with session.get(url, allow_redirects=True) as response:
            if response.status >= 400:
                raise OpenDirSyncError(f"GET {self._redact_url(url)} failed with HTTP {response.status}")

            content_type = response.headers.get("Content-Type", "")
            if content_type and "text" not in content_type and "html" not in content_type:
                log.debug("OpenDir page %s returned content-type %s", self._redact_url(url), content_type)

            return await response.text(errors="replace")

    async def _upload_if_needed(
        self,
        session: aiohttp.ClientSession,
        remote_file: RemoteFile,
        existing_keys: set[str],
        semaphore: asyncio.Semaphore,
        summary: SyncSummary,
    ) -> None:
        async with semaphore:
            summary.files_seen += 1
            r2_key = self._r2_key_for(remote_file.relative_path)

            if r2_key in existing_keys:
                summary.files_existing += 1
                return

            try:
                size = await self._remote_size(session, remote_file.url)
                remote_file.size = size

                if size is not None and size > self.max_file_bytes:
                    summary.files_skipped += 1
                    summary.add_error(f"skip too large: {remote_file.relative_path} ({size} bytes)")
                    return

                uploaded_bytes = await self._stream_url_to_r2(session, remote_file.url, r2_key)

                existing_keys.add(r2_key)
                summary.files_uploaded += 1
                summary.bytes_uploaded += uploaded_bytes
                summary.add_sample(f"uploaded {remote_file.relative_path} -> {r2_key}")
                log.info("OpenDir uploaded %s -> %s (%s bytes)", remote_file.relative_path, r2_key, uploaded_bytes)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                summary.files_skipped += 1
                summary.add_error(f"{remote_file.relative_path}: {exc!r}")
                log.warning("OpenDir failed to upload %s: %r", remote_file.relative_path, exc)

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

    async def _stream_url_to_r2(self, session: aiohttp.ClientSession, url: str, r2_key: str) -> int:
        if self.s3 is None:
            raise OpenDirSyncError("R2 client is not initialized")

        q: "queue.Queue[bytes | object | BaseException]" = queue.Queue(maxsize=self.queue_chunks)
        stream = QueueReadStream(q)
        upload_task = asyncio.create_task(asyncio.to_thread(self._upload_stream_sync, stream, r2_key))
        total = 0

        try:
            async with session.get(url, allow_redirects=True) as response:
                if response.status >= 400:
                    raise OpenDirSyncError(f"GET file failed with HTTP {response.status}")

                async for chunk in response.content.iter_chunked(self.chunk_size):
                    if not chunk:
                        continue

                    total += len(chunk)
                    if total > self.max_file_bytes:
                        raise OpenDirSyncError(f"file exceeds OPENDIR_MAX_FILE_MB={_cfg_int('OPENDIR_MAX_FILE_MB', 1024)}")

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

    def _upload_stream_sync(self, stream: QueueReadStream, r2_key: str) -> None:
        assert self.s3 is not None

        extra_args = {
            "Metadata": {
                "source": "opendir-sync",
                "synced-by": "triadbot",
            }
        }

        self.s3.upload_fileobj(
            stream,
            _cfg_str("R2_BUCKET_NAME"),
            r2_key,
            ExtraArgs=extra_args,
            Config=self.transfer_config,
        )

    async def _list_r2_keys(self) -> set[str]:
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
                "OpenDir sync finished.",
                {
                    "fields": summary.to_fields(),
                    "errors": summary.errors[:10],
                    "samples": summary.samples[:10],
                },
            )

        if hasattr(self.bot, "queue_ai_caretaker"):
            self.bot.queue_ai_caretaker(
                "opendir-sync-finished",
                {"errors": len(summary.errors), "uploaded": summary.files_uploaded, "mode": summary.mode},
                force=summary.has_errors,
            )

        notifier = getattr(self.bot, "notify_admins", None)
        if not notifier:
            return

        if not summary.has_errors and not self.notify_on_success:
            return

        fields = summary.to_fields()
        if summary.samples:
            fields["Samples"] = "\n".join(summary.samples[:8])
        if summary.errors:
            fields["Errors"] = "\n".join(summary.errors[:8])

        result = notifier(
            "OpenDir sync needs attention" if summary.has_errors else "OpenDir sync completed",
            "The Open Directory to R2 sync task finished.",
            level="error" if summary.has_errors else "info",
            fields=fields,
            key="opendir-sync-error" if summary.has_errors else "opendir-sync-ok",
            force=summary.has_errors,
        )
        if inspect.isawaitable(result):
            await result

    def _iter_hrefs(self, soup: BeautifulSoup) -> Iterable[str]:
        for tag in soup.find_all("a", href=True):
            href = str(tag.get("href") or "").strip()
            if not href or href in {"../", "./", "/"}:
                continue

            lowered = href.lower()
            if lowered.startswith(("javascript:", "mailto:", "data:", "tel:")):
                continue

            yield href

    def _resolve_in_scope(self, current_url: str, href: str) -> str | None:
        clean_href, _fragment = urldefrag(href)
        resolved = urljoin(current_url, clean_href)
        parsed = urlparse(resolved)
        base = urlparse(self.base_url)

        if parsed.scheme not in {"http", "https"}:
            return None

        if parsed.netloc.lower() != base.netloc.lower():
            return None

        if self.allowed_hosts and parsed.hostname and parsed.hostname.lower() not in self.allowed_hosts:
            return None

        base_path = base.path if base.path.endswith("/") else f"{base.path}/"
        parsed_path = unquote(parsed.path)

        if not parsed_path.startswith(base_path):
            return None

        return resolved

    def _relative_path_from_url(self, url: str) -> str:
        base_path = unquote(urlparse(self.base_url).path)
        if not base_path.endswith("/"):
            base_path = f"{base_path}/"

        path = unquote(urlparse(url).path)
        if not path.startswith(base_path):
            return ""

        rel = path[len(base_path):].strip("/")
        rel = posixpath.normpath(rel).replace("\\", "/")

        if rel == "." or rel.startswith("../") or "/../" in rel:
            return ""

        return rel

    def _r2_key_for(self, relative_path: str) -> str:
        safe_rel = posixpath.normpath(relative_path).strip("/").replace("\\", "/")

        if safe_rel == "." or safe_rel.startswith("../") or "/../" in safe_rel:
            raise OpenDirSyncError(f"unsafe remote path: {relative_path!r}")

        if self.flatten_r2_keys:
            safe_rel = posixpath.basename(safe_rel)

        return f"{self.r2_prefix}{safe_rel}"

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
