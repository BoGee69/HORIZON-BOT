"""GitHub backup helper for Railway SQLite database.

This module pushes a safe SQLite snapshot to a private GitHub repository using
GitHub's Contents API. It does not require git CLI and it does not upload the
live SQLite file directly.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
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
        timeout_seconds: int = 120,
    ) -> None:
        self.token = (token or "").strip()
        self.repo = (repo or "").strip().strip("/")
        self.branch = (branch or "main").strip() or "main"
        self.db_path = Path(db_path)
        self.github_db_path = self._normalize_repo_path(github_db_path or "games.db")
        self.metadata_path = self._normalize_repo_path(metadata_path or f"{self.github_db_path}.meta.json")
        self.session = session
        self.timeout_seconds = int(timeout_seconds)

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

        try:
            snapshot_path = await asyncio.to_thread(self._create_sqlite_snapshot)
        except Exception as exc:
            log.exception("Failed to create SQLite snapshot for GitHub backup")
            return GitHubBackupResult(False, "snapshot_failed", str(exc))

        try:
            file_bytes = await asyncio.to_thread(snapshot_path.read_bytes)
            sha256 = hashlib.sha256(file_bytes).hexdigest()
            size_bytes = len(file_bytes)

            old_meta = await self._get_json_file(self.metadata_path)
            if not force and isinstance(old_meta, dict) and old_meta.get("sha256") == sha256:
                return GitHubBackupResult(
                    True,
                    "unchanged",
                    "SQLite backup skipped; GitHub already has the same DB checksum.",
                    uploaded=False,
                    sha256=sha256,
                    size_bytes=size_bytes,
                )

            db_response = await self._put_file(
                self.github_db_path,
                file_bytes,
                message=f"backup: update SQLite database ({reason})",
            )

            html_url = ""
            if isinstance(db_response, dict):
                html_url = ((db_response.get("content") or {}).get("html_url") or "")

            metadata = {
                "repo": self.repo,
                "branch": self.branch,
                "db_path": str(self.db_path),
                "github_db_path": self.github_db_path,
                "sha256": sha256,
                "size_bytes": size_bytes,
                "reason": reason,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
            await self._put_file(
                self.metadata_path,
                json.dumps(metadata, indent=2, sort_keys=True).encode("utf-8"),
                message=f"backup: update SQLite metadata ({reason})",
            )

            return GitHubBackupResult(
                True,
                "uploaded",
                "SQLite database backed up to GitHub.",
                uploaded=True,
                sha256=sha256,
                size_bytes=size_bytes,
                html_url=html_url,
            )
        except Exception as exc:
            log.exception("GitHub SQLite backup failed")
            return GitHubBackupResult(False, "upload_failed", str(exc))
        finally:
            try:
                snapshot_path.unlink(missing_ok=True)
            except Exception:
                pass

    def _create_sqlite_snapshot(self) -> Path:
        """Create a consistent SQLite snapshot using sqlite backup API."""
        snapshot_dir = Path(tempfile.gettempdir())
        snapshot_path = snapshot_dir / f"triadbot_games_snapshot_{os.getpid()}.db"
        snapshot_path.unlink(missing_ok=True)

        source = sqlite3.connect(str(self.db_path), timeout=60)
        try:
            # Flush WAL frames when possible. If another writer is active, backup() still
            # gives a consistent snapshot, so checkpoint failure is not fatal.
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
