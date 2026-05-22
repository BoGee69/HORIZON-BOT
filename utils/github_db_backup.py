"""GitHub backup helper for Railway SQLite database.

This module creates a safe SQLite snapshot, compresses it, splits it into
GitHub-friendly chunks, and uploads those chunks to a private GitHub repository
using GitHub's Contents API.

Why chunked backup?
GitHub's Contents API rejects large files. A Railway SQLite DB can quickly grow
past that limit, so this helper never tries to PUT games.db directly.
"""
from __future__ import annotations

import asyncio
import base64
import gzip
import hashlib
import json
import logging
import math
import os
import sqlite3
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import aiohttp

log = logging.getLogger(__name__)


@dataclass(slots=True)
class GitHubBackupResult:
    ok: bool
    status: str
    message: str
    uploaded: bool = False
    sha256: str = ""
    size_bytes: int = 0
    compressed_sha256: str = ""
    compressed_size_bytes: int = 0
    chunk_count: int = 0
    html_url: str = ""


class GitHubDatabaseBackup:
    """Backup SQLite DB to GitHub through the REST API."""

    API_BASE = "https://api.github.com"

    def __init__(
        self,
        *,
        token: str,
        repo: str,
        branch: str,
        db_path: Path,
        github_db_path: str,
        metadata_path: str | None = None,
        session: aiohttp.ClientSession | None = None,
        timeout_seconds: int = 180,
        chunk_size_mb: int = 40,
    ) -> None:
        self.token = (token or "").strip()
        self.repo = (repo or "").strip().strip("/")
        self.branch = (branch or "main").strip() or "main"
        self.db_path = Path(db_path)
        self.github_db_path = self._normalize_repo_path(github_db_path or "games.db")
        self.metadata_path = self._normalize_repo_path(metadata_path or f"{self.github_db_path}.meta.json")
        self.manifest_path = self._normalize_repo_path(f"{self.github_db_path}.backup_manifest.json")
        self.session = session
        self.timeout_seconds = int(timeout_seconds)
        self.chunk_size_bytes = max(int(chunk_size_mb), 1) * 1024 * 1024

    @staticmethod
    def _normalize_repo_path(path: str) -> str:
        return str(path or "").strip().strip("/")

    @property
    def enabled(self) -> bool:
        return bool(self.token and self.repo and self.github_db_path and self.db_path)

    def _headers(self) -> dict[str, str]:
        return {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {self.token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "TriadBot-GitHub-DB-Backup",
        }

    def _contents_url(self, path: str) -> str:
        return f"{self.API_BASE}/repos/{self.repo}/contents/{path}"

    async def backup(self, *, force: bool = False, reason: str = "scheduled") -> GitHubBackupResult:
        if not self.enabled:
            return GitHubBackupResult(False, "disabled", "GitHub DB backup is not configured.")
        if not self.db_path.exists():
            return GitHubBackupResult(False, "missing_db", f"SQLite DB not found: {self.db_path}")

        snapshot_path: Path | None = None
        gz_path: Path | None = None

        try:
            snapshot_path = await asyncio.to_thread(self._create_sqlite_snapshot)
            gz_path = await asyncio.to_thread(self._gzip_file, snapshot_path)
        except Exception as exc:
            log.exception("Failed to create/compress SQLite snapshot for GitHub backup")
            return GitHubBackupResult(False, "snapshot_failed", str(exc))

        try:
            sha256 = await asyncio.to_thread(self._sha256_file, snapshot_path)
            compressed_sha256 = await asyncio.to_thread(self._sha256_file, gz_path)
            size_bytes = snapshot_path.stat().st_size
            compressed_size_bytes = gz_path.stat().st_size
            chunk_count = max(1, math.ceil(compressed_size_bytes / self.chunk_size_bytes))

            old_meta = await self._get_json_file(self.metadata_path)
            if not force and isinstance(old_meta, dict) and old_meta.get("sha256") == sha256:
                return GitHubBackupResult(
                    True,
                    "unchanged",
                    "SQLite backup skipped; GitHub already has the same DB checksum.",
                    uploaded=False,
                    sha256=sha256,
                    size_bytes=size_bytes,
                    compressed_sha256=compressed_sha256,
                    compressed_size_bytes=compressed_size_bytes,
                    chunk_count=int(old_meta.get("chunk_count") or chunk_count),
                )

            chunk_paths = await self._upload_gzip_chunks(
                gz_path,
                chunk_count=chunk_count,
                reason=reason,
            )

            previous_chunk_count = 0
            if isinstance(old_meta, dict):
                try:
                    previous_chunk_count = int(old_meta.get("chunk_count") or 0)
                except Exception:
                    previous_chunk_count = 0
            await self._delete_stale_chunks(previous_chunk_count, chunk_count, reason=reason)

            manifest = {
                "format": "sqlite-gzip-split-v1",
                "repo": self.repo,
                "branch": self.branch,
                "source_db_path": str(self.db_path),
                "github_db_path": self.github_db_path,
                "sha256": sha256,
                "size_bytes": size_bytes,
                "compressed_sha256": compressed_sha256,
                "compressed_size_bytes": compressed_size_bytes,
                "chunk_size_bytes": self.chunk_size_bytes,
                "chunk_count": chunk_count,
                "chunks": chunk_paths,
                "restore_hint": "Download parts in order, concatenate them, gzip-decompress, then save as games.db.",
                "reason": reason,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
            manifest_response = await self._put_file(
                self.manifest_path,
                json.dumps(manifest, indent=2, sort_keys=True).encode("utf-8"),
                message=f"backup: update SQLite backup manifest ({reason})",
            )

            html_url = ""
            if isinstance(manifest_response, dict):
                html_url = ((manifest_response.get("content") or {}).get("html_url") or "")

            metadata = {
                **manifest,
                "metadata_path": self.metadata_path,
                "manifest_path": self.manifest_path,
            }
            await self._put_file(
                self.metadata_path,
                json.dumps(metadata, indent=2, sort_keys=True).encode("utf-8"),
                message=f"backup: update SQLite metadata ({reason})",
            )

            return GitHubBackupResult(
                True,
                "uploaded_chunked",
                f"SQLite database backed up to GitHub as gzip split into {chunk_count} chunk(s).",
                uploaded=True,
                sha256=sha256,
                size_bytes=size_bytes,
                compressed_sha256=compressed_sha256,
                compressed_size_bytes=compressed_size_bytes,
                chunk_count=chunk_count,
                html_url=html_url,
            )
        except Exception as exc:
            log.exception("GitHub SQLite backup failed")
            return GitHubBackupResult(False, "upload_failed", str(exc))
        finally:
            for path in (snapshot_path, gz_path):
                try:
                    if path:
                        path.unlink(missing_ok=True)
                except Exception:
                    pass

    def _create_sqlite_snapshot(self) -> Path:
        """Create a consistent SQLite snapshot using sqlite backup API."""
        snapshot_dir = Path(tempfile.gettempdir())
        snapshot_path = snapshot_dir / f"triadbot_games_snapshot_{os.getpid()}.db"
        snapshot_path.unlink(missing_ok=True)

        source = sqlite3.connect(str(self.db_path), timeout=60)
        try:
            try:
                source.execute("PRAGMA wal_checkpoint(PASSIVE)")
            except sqlite3.Error:
                pass

            dest = sqlite3.connect(str(snapshot_path))
            try:
                source.backup(dest)
                dest.commit()
            finally:
                dest.close()
        finally:
            source.close()

        return snapshot_path

    def _gzip_file(self, source_path: Path) -> Path:
        gz_path = source_path.with_suffix(source_path.suffix + ".gz")
        gz_path.unlink(missing_ok=True)
        with source_path.open("rb") as src, gzip.open(gz_path, "wb", compresslevel=6) as dst:
            while True:
                chunk = src.read(1024 * 1024)
                if not chunk:
                    break
                dst.write(chunk)
        return gz_path

    @staticmethod
    def _sha256_file(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as fh:
            for block in iter(lambda: fh.read(1024 * 1024), b""):
                digest.update(block)
        return digest.hexdigest()

    def _chunk_path(self, index: int) -> str:
        return f"{self.github_db_path}.gz.part{index:03d}"

    async def _upload_gzip_chunks(self, gz_path: Path, *, chunk_count: int, reason: str) -> list[str]:
        paths: list[str] = []
        with gz_path.open("rb") as fh:
            for index in range(chunk_count):
                chunk_bytes = fh.read(self.chunk_size_bytes)
                if not chunk_bytes:
                    break
                path = self._chunk_path(index)
                paths.append(path)
                await self._put_file(
                    path,
                    chunk_bytes,
                    message=f"backup: update SQLite chunk {index + 1}/{chunk_count} ({reason})",
                )
        return paths

    async def _delete_stale_chunks(self, previous_count: int, current_count: int, *, reason: str) -> None:
        if previous_count <= current_count:
            return
        for index in range(current_count, previous_count):
            path = self._chunk_path(index)
            try:
                await self._delete_file(path, message=f"backup: remove stale SQLite chunk ({reason})")
            except Exception:
                log.warning("Failed to delete stale GitHub backup chunk %s", path, exc_info=True)

    async def _session(self) -> tuple[aiohttp.ClientSession, bool]:
        if self.session and not self.session.closed:
            return self.session, False
        timeout = aiohttp.ClientTimeout(total=self.timeout_seconds)
        return aiohttp.ClientSession(timeout=timeout), True

    async def _request_json(self, method: str, url: str, **kwargs: Any) -> tuple[int, Any]:
        session, should_close = await self._session()
        try:
            async with session.request(method, url, headers=self._headers(), **kwargs) as resp:
                text = await resp.text()
                try:
                    payload: Any = json.loads(text) if text else None
                except json.JSONDecodeError:
                    payload = text
                return resp.status, payload
        finally:
            if should_close:
                await session.close()

    async def _get_file_sha(self, path: str) -> Optional[str]:
        status, payload = await self._request_json(
            "GET",
            self._contents_url(path),
            params={"ref": self.branch},
        )
        if status == 404:
            return None
        if status >= 400:
            raise RuntimeError(f"GitHub GET {path} failed: HTTP {status}: {payload}")
        if isinstance(payload, dict):
            sha = payload.get("sha")
            return str(sha) if sha else None
        return None

    async def _get_json_file(self, path: str) -> Any:
        status, payload = await self._request_json(
            "GET",
            self._contents_url(path),
            params={"ref": self.branch},
        )
        if status == 404:
            return None
        if status >= 400:
            log.warning("GitHub metadata read failed for %s: HTTP %s: %s", path, status, payload)
            return None
        if not isinstance(payload, dict) or not payload.get("content"):
            return None
        try:
            raw = base64.b64decode(str(payload["content"]).replace("\n", ""))
            return json.loads(raw.decode("utf-8"))
        except Exception:
            return None

    async def _put_file(self, path: str, file_bytes: bytes, *, message: str) -> Any:
        existing_sha = await self._get_file_sha(path)
        body: dict[str, Any] = {
            "message": message,
            "content": base64.b64encode(file_bytes).decode("ascii"),
            "branch": self.branch,
        }
        if existing_sha:
            body["sha"] = existing_sha

        status, payload = await self._request_json("PUT", self._contents_url(path), json=body)
        if status not in (200, 201):
            raise RuntimeError(f"GitHub PUT {path} failed: HTTP {status}: {payload}")
        return payload

    async def _delete_file(self, path: str, *, message: str) -> Any:
        existing_sha = await self._get_file_sha(path)
        if not existing_sha:
            return None
        body: dict[str, Any] = {
            "message": message,
            "sha": existing_sha,
            "branch": self.branch,
        }
        status, payload = await self._request_json("DELETE", self._contents_url(path), json=body)
        if status not in (200, 204):
            raise RuntimeError(f"GitHub DELETE {path} failed: HTTP {status}: {payload}")
        return payload
