"""
R2 database maintenance tools.

This module can normalize ZIP object names to "Game Name (appid).zip" and
optionally rewrite Lua/manifest files inside the ZIPs with comments removed.
"""
from __future__ import annotations

import argparse
import logging
import time
import zipfile
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path
from typing import Any, Iterable, Optional

from botocore.exceptions import ClientError

import config as bot_config
from utils.lua_cleaner import clean_lua_bytes
from utils.manifest_cleaner import clean_manifest_bytes
from utils.r2_presign import _BUCKET, _PRESIGN_ENABLED, _make_client
from utils.rename_database_files import (
    DEFAULT_CACHE_JSON,
    DEFAULT_DB_JSON,
    fetch_steam_name,
    load_json,
    parse_appid_from_stem,
    sanitize_game_name,
    save_json,
    set_name,
)

log = logging.getLogger(__name__)


@dataclass
class ZipCleanResult:
    data: bytes
    changed: bool
    files_checked: int = 0
    files_cleaned: int = 0
    lua_files_checked: int = 0
    lua_files_cleaned: int = 0
    manifest_files_checked: int = 0
    manifest_files_cleaned: int = 0


@dataclass
class R2MaintenanceSummary:
    dry_run: bool
    prefix: str
    scanned: int = 0
    processed: int = 0
    rename_planned: int = 0
    rename_applied: int = 0
    comment_objects_changed: int = 0
    comment_files_checked: int = 0
    comment_files_cleaned: int = 0
    lua_files_cleaned: int = 0
    manifest_files_cleaned: int = 0
    uploaded: int = 0
    copied: int = 0
    deleted: int = 0
    steam_lookups: int = 0
    steam_lookup_failed: int = 0
    blacklisted: int = 0
    fallback_renames: int = 0
    queue_resets: int = 0
    skipped: int = 0
    total_rename_applied: int = 0
    total_comment_files_cleaned: int = 0
    r2_zip_objects_counted: int = 0
    r2_named_zip_objects_counted: int = 0
    r2_appid_only_zip_objects_counted: int = 0
    r2_unknown_zip_objects_counted: int = 0
    errors: list[str] = field(default_factory=list)
    samples: list[str] = field(default_factory=list)
    applied_samples: list[str] = field(default_factory=list)

    @property
    def has_changes(self) -> bool:
        return bool(
            self.rename_planned
            or self.rename_applied
            or self.comment_objects_changed
            or self.comment_files_cleaned
            or self.lua_files_cleaned
            or self.manifest_files_cleaned
            or self.uploaded
            or self.copied
            or self.deleted
            or self.fallback_renames
        )

    def add_sample(self, message: str, limit: int = 8) -> None:
        if len(self.samples) < limit:
            self.samples.append(message[:300])

    def add_applied_sample(self, message: str, limit: int = 12) -> None:
        if len(self.applied_samples) < limit:
            self.applied_samples.append(message[:300])

    def add_error(self, message: str, limit: int = 10) -> None:
        log.error(message)
        if len(self.errors) < limit:
            self.errors.append(message[:500])

    def to_fields(self) -> dict[str, str]:
        return {
            "Mode": "DRY-RUN" if self.dry_run else "APPLY",
            "Prefix": self.prefix or "(bucket root)",
            "Scanned": str(self.scanned),
            "Processed": str(self.processed),
            "Rename planned": str(self.rename_planned),
            "Rename applied": str(self.rename_applied),
            "Comment files checked": str(self.comment_files_checked),
            "Comment files cleaned": str(self.comment_files_cleaned),
            "Lua files cleaned": str(self.lua_files_cleaned),
            "Manifest files cleaned": str(self.manifest_files_cleaned),
            "Objects uploaded": str(self.uploaded),
            "Steam lookups": str(self.steam_lookups),
            "Steam lookup failed": str(self.steam_lookup_failed),
            "Blacklisted skips": str(self.blacklisted),
            "Fallback renames": str(self.fallback_renames),
            "Queue resets": str(self.queue_resets),
            "Skipped": str(self.skipped),
            "Total rename applied": str(self.total_rename_applied),
            "Total comment files cleaned": str(self.total_comment_files_cleaned),
            "R2 ZIP objects counted": str(self.r2_zip_objects_counted),
            "R2 ZIP already formatted": str(self.r2_named_zip_objects_counted),
            "R2 ZIP AppID-only": str(self.r2_appid_only_zip_objects_counted),
            "R2 ZIP unknown format": str(self.r2_unknown_zip_objects_counted),
            "Errors": str(len(self.errors)),
        }


def _split_key(key: str) -> tuple[str, str]:
    if "/" not in key:
        return "", key
    prefix, filename = key.rsplit("/", 1)
    return f"{prefix}/", filename


def _zip_stem_from_key(key: str) -> Optional[str]:
    _, filename = _split_key(key)
    if not filename.lower().endswith(".zip"):
        return None
    return filename[:-4]


def _target_key(source_key: str, appid: str, name: str) -> Optional[str]:
    safe_name = sanitize_game_name(name)
    if not safe_name:
        return None
    prefix, _ = _split_key(source_key)
    return f"{prefix}{safe_name} ({appid}).zip"


def _fallback_key(source_key: str, appid: str) -> str:
    prefix, _ = _split_key(source_key)
    return f"{prefix}{appid}.zip"


def _copy_zip_info(info: zipfile.ZipInfo) -> zipfile.ZipInfo:
    copied = zipfile.ZipInfo(info.filename, info.date_time)
    copied.compress_type = info.compress_type
    copied.comment = info.comment
    copied.extra = info.extra
    copied.internal_attr = info.internal_attr
    copied.external_attr = info.external_attr
    copied.create_system = info.create_system
    return copied


def _cleanable_extension(filename: str) -> str:
    name = filename.rsplit("/", 1)[-1].lower()
    if "." not in name:
        return ""
    return name.rsplit(".", 1)[-1]


def clean_zip_comments(
    data: bytes,
    clean_extensions: set[str] | None = None,
) -> ZipCleanResult:
    changed = False
    files_checked = 0
    files_cleaned = 0
    lua_files_checked = 0
    lua_files_cleaned = 0
    manifest_files_checked = 0
    manifest_files_cleaned = 0
    out_buffer = BytesIO()
    extensions = clean_extensions or bot_config.R2_MAINTENANCE_CLEAN_EXTENSIONS

    with zipfile.ZipFile(BytesIO(data), "r") as source_zip:
        with zipfile.ZipFile(out_buffer, "w") as target_zip:
            for item in source_zip.infolist():
                item_data = b"" if item.is_dir() else source_zip.read(item.filename)
                ext = _cleanable_extension(item.filename)
                if not item.is_dir() and ext in extensions:
                    files_checked += 1
                    if ext == "lua":
                        lua_files_checked += 1
                        cleaned = clean_lua_bytes(item_data)
                        if cleaned.changed:
                            lua_files_cleaned += 1
                    else:
                        manifest_files_checked += 1
                        cleaned = clean_manifest_bytes(item_data)
                        if cleaned.changed:
                            manifest_files_cleaned += 1

                    if cleaned.changed:
                        item_data = cleaned.data
                        changed = True
                        files_cleaned += 1

                target_zip.writestr(_copy_zip_info(item), item_data)

    if not changed:
        return ZipCleanResult(
            data=data,
            changed=False,
            files_checked=files_checked,
            files_cleaned=0,
            lua_files_checked=lua_files_checked,
            lua_files_cleaned=0,
            manifest_files_checked=manifest_files_checked,
            manifest_files_cleaned=0,
        )

    return ZipCleanResult(
        data=out_buffer.getvalue(),
        changed=True,
        files_checked=files_checked,
        files_cleaned=files_cleaned,
        lua_files_checked=lua_files_checked,
        lua_files_cleaned=lua_files_cleaned,
        manifest_files_checked=manifest_files_checked,
        manifest_files_cleaned=manifest_files_cleaned,
    )


def clean_zip_lua_comments(data: bytes) -> ZipCleanResult:
    return clean_zip_comments(data, {"lua"})


def _load_games_from_json(path: Path = DEFAULT_DB_JSON) -> list[dict[str, Any]]:
    payload = load_json(path, [])
    return payload if isinstance(payload, list) else []


def _build_name_map(
    keys: Iterable[str],
    games: Optional[Iterable[dict[str, Any]]] = None,
    cache_json: Path = DEFAULT_CACHE_JSON,
) -> dict[str, tuple[str, str, int]]:
    name_map: dict[str, tuple[str, str, int]] = {}

    for key in keys:
        stem = _zip_stem_from_key(key)
        if not stem:
            continue
        appid, file_name = parse_appid_from_stem(stem)
        if appid and file_name:
            set_name(name_map, appid, file_name, "existing R2 object name", 30)

    cache = load_json(cache_json, {})
    if isinstance(cache, dict):
        for appid, name in cache.items():
            set_name(name_map, str(appid), name, "steam cache", 20)

    source_games = list(games) if games is not None else _load_games_from_json()
    for item in source_games:
        if not isinstance(item, dict):
            continue
        appid = str(item.get("id", "")).strip()
        if appid.isdigit():
            set_name(name_map, appid, item.get("name"), "games.json", 10)

    return name_map


def _load_state(path: Path = bot_config.R2_MAINTENANCE_STATE_PATH) -> dict[str, Any]:
    state = load_json(path, {})
    if not isinstance(state, dict):
        state = {}
    state.setdefault("version", 1)
    state.setdefault("last_key_by_prefix", {})
    state.setdefault("steam_failures", {})
    state.setdefault("totals", {})
    return state


def _save_state(state: dict[str, Any], path: Path = bot_config.R2_MAINTENANCE_STATE_PATH) -> None:
    save_json(path, state)


def _reconcile_rename_totals_from_r2(
    state: dict[str, Any],
    summary: R2MaintenanceSummary,
    prefix: str,
) -> bool:
    if summary.dry_run:
        return False
    try:
        from utils.r2_inventory import get_r2_inventory_snapshot, invalidate_r2_inventory_cache

        invalidate_r2_inventory_cache()
        inventory = get_r2_inventory_snapshot(
            prefix=prefix,
            cache_seconds=0,
            max_pages=bot_config.AI_CHAT_R2_STATS_MAX_PAGES,
        )
    except Exception:
        log.debug("Could not reconcile R2 rename totals from live inventory", exc_info=True)
        return False

    if inventory.get("error"):
        log.debug("Could not reconcile R2 rename totals: %s", inventory.get("error"))
        return False

    total_zip = int(inventory.get("zip_objects_counted") or 0)
    named_zip = int(inventory.get("named_zip_objects_counted") or 0)
    appid_only = int(inventory.get("appid_only_zip_objects_counted") or 0)
    unknown = int(inventory.get("unknown_zip_objects_counted") or 0)

    summary.r2_zip_objects_counted = total_zip
    summary.r2_named_zip_objects_counted = named_zip
    summary.r2_appid_only_zip_objects_counted = appid_only
    summary.r2_unknown_zip_objects_counted = unknown

    totals = state.setdefault("totals", {})
    current = int(totals.get("rename_applied", 0) or 0)
    reconciled = max(current, named_zip) if inventory.get("truncated") else named_zip
    summary.total_rename_applied = reconciled
    if current == reconciled:
        return False
    totals["rename_applied"] = reconciled
    return True


def _failure_record(state: dict[str, Any], appid: str) -> dict[str, Any]:
    failures = state.setdefault("steam_failures", {})
    record = failures.get(appid)
    if not isinstance(record, dict):
        record = {"count": int(record or 0)}
        failures[appid] = record
    record.setdefault("count", 0)
    return record


def _is_blacklisted(state: dict[str, Any], appid: str, threshold: int) -> bool:
    if threshold <= 0:
        return False
    return int(_failure_record(state, appid).get("count", 0) or 0) >= threshold


def _record_steam_success(state: dict[str, Any], appid: str) -> None:
    state.setdefault("steam_failures", {}).pop(appid, None)


def _record_steam_failure(state: dict[str, Any], appid: str) -> None:
    record = _failure_record(state, appid)
    record["count"] = int(record.get("count", 0) or 0) + 1
    record["last_failed_at"] = int(time.time())


def _list_zip_objects(
    client,
    prefix: str,
    max_objects: int,
    *,
    start_after: str = "",
) -> tuple[list[dict[str, Any]], str, bool]:
    objects: list[dict[str, Any]] = []
    next_start_after = start_after
    reached_end = False
    remaining = max(0, max_objects)

    while True:
        kwargs: dict[str, Any] = {
            "Bucket": _BUCKET,
            "Prefix": prefix or "",
            "MaxKeys": min(1000, remaining or 1000),
        }
        if next_start_after:
            kwargs["StartAfter"] = next_start_after

        page = client.list_objects_v2(**kwargs)
        for item in page.get("Contents", []):
            key = item.get("Key", "")
            if not key or key.endswith("/") or not key.lower().endswith(".zip"):
                continue
            objects.append({"Key": key, "Size": int(item.get("Size") or 0)})
            if max_objects > 0 and len(objects) >= max_objects:
                return objects, key, False

        if not page.get("IsTruncated"):
            reached_end = True
            break

        next_start_after = str(page.get("Contents", [{}])[-1].get("Key", "") or next_start_after)
        if not next_start_after:
            break
        if max_objects > 0:
            remaining = max_objects - len(objects)
            if remaining <= 0:
                break

    last_key = objects[-1]["Key"] if objects else start_after
    return objects, last_key, reached_end


def _object_exists(client, key: str) -> bool:
    try:
        client.head_object(Bucket=_BUCKET, Key=key)
        return True
    except ClientError as exc:
        code = str(exc.response.get("Error", {}).get("Code", ""))
        if code in {"404", "NoSuchKey", "NotFound"}:
            return False
        raise


def _client_error_code(exc: ClientError) -> str:
    return str(exc.response.get("Error", {}).get("Code", ""))


def _is_access_denied(exc: ClientError) -> bool:
    code = _client_error_code(exc).lower()
    return code in {"accessdenied", "403", "forbidden"}


def _delete_source_if_needed(client, source_key: str, target_key: str, summary: R2MaintenanceSummary) -> None:
    if source_key == target_key:
        return
    client.delete_object(Bucket=_BUCKET, Key=source_key)
    summary.deleted += 1


def _fill_missing_names_from_steam(
    objects: list[dict[str, Any]],
    name_map: dict[str, tuple[str, str, int]],
    max_lookups: int,
    delay_seconds: float,
    cache_json: Path,
    state: dict[str, Any],
    summary: R2MaintenanceSummary,
    blacklist_threshold: int,
    ignore_blacklist: bool = False,
) -> None:
    if max_lookups == 0:
        return

    cache = load_json(cache_json, {})
    if not isinstance(cache, dict):
        cache = {}

    fetched_count = 0
    changed = False
    for item in objects:
        if max_lookups > 0 and fetched_count >= max_lookups:
            break
        stem = _zip_stem_from_key(item["Key"])
        if not stem:
            continue
        appid, _ = parse_appid_from_stem(stem)
        if not appid or appid in name_map:
            continue
        if not ignore_blacklist and _is_blacklisted(state, appid, blacklist_threshold):
            summary.blacklisted += 1
            continue

        fetched_count += 1
        summary.steam_lookups += 1
        name = fetch_steam_name(appid)
        if name:
            cache[appid] = name
            set_name(name_map, appid, name, "steam api", 25)
            _record_steam_success(state, appid)
            changed = True
        else:
            summary.steam_lookup_failed += 1
            _record_steam_failure(state, appid)
        if delay_seconds > 0:
            time.sleep(delay_seconds)

    if changed:
        save_json(cache_json, cache)


def run_r2_maintenance(
    *,
    apply: bool = False,
    prefix: str = bot_config.R2_MAINTENANCE_PREFIX,
    limit: int = bot_config.R2_MAINTENANCE_MAX_OBJECTS,
    games: Optional[Iterable[dict[str, Any]]] = None,
    rename_objects: bool = bot_config.R2_MAINTENANCE_RENAME_OBJECTS,
    clean_lua_comments: bool = bot_config.R2_MAINTENANCE_CLEAN_LUA_COMMENTS,
    use_steam: bool = bot_config.R2_MAINTENANCE_STEAM_LOOKUPS,
    max_steam_lookups: int = bot_config.R2_MAINTENANCE_MAX_STEAM_LOOKUPS,
    max_zip_mb: int = bot_config.R2_MAINTENANCE_MAX_ZIP_MB,
    steam_delay_seconds: float = bot_config.R2_MAINTENANCE_STEAM_DELAY_SECONDS,
    clean_extensions: set[str] | None = None,
    use_queue: bool = bot_config.R2_MAINTENANCE_QUEUE_ENABLED,
    fallback_to_appid: bool = bot_config.R2_MAINTENANCE_FALLBACK_TO_APPID,
    blacklist_threshold: int = bot_config.R2_MAINTENANCE_BLACKLIST_THRESHOLD,
    ignore_blacklist: bool = False,
) -> R2MaintenanceSummary:
    summary = R2MaintenanceSummary(dry_run=not apply, prefix=prefix or "")
    if not _PRESIGN_ENABLED:
        summary.add_error("R2 credentials are incomplete; maintenance cannot run.")
        return summary

    client = _make_client()
    max_zip_bytes = max(1, max_zip_mb) * 1024 * 1024
    state = _load_state()
    totals = state.setdefault("totals", {})
    summary.total_rename_applied = int(totals.get("rename_applied", 0) or 0)
    summary.total_comment_files_cleaned = int(totals.get("comment_files_cleaned", 0) or 0)
    start_after = ""
    if use_queue:
        start_after = str(state.get("last_key_by_prefix", {}).get(prefix or "", "") or "")

    try:
        objects, next_start_after, reached_end = _list_zip_objects(
            client,
            prefix or "",
            max(0, limit),
            start_after=start_after,
        )
        if not objects and start_after:
            summary.queue_resets += 1
            objects, next_start_after, reached_end = _list_zip_objects(
                client,
                prefix or "",
                max(0, limit),
                start_after="",
            )
    except Exception as exc:
        summary.add_error(f"Failed to list R2 objects: {exc}")
        return summary

    summary.scanned = len(objects)
    object_keys = {item["Key"] for item in objects}
    name_map = _build_name_map(object_keys, games)

    if rename_objects and use_steam:
        try:
            _fill_missing_names_from_steam(
                objects,
                name_map,
                max_steam_lookups,
                steam_delay_seconds,
                DEFAULT_CACHE_JSON,
                state,
                summary,
                blacklist_threshold,
                ignore_blacklist,
            )
        except Exception as exc:
            summary.add_error(f"Steam name lookup failed: {exc}")

    for item in objects:
        source_key = item["Key"]
        source_size = int(item.get("Size") or 0)
        target_key = source_key
        needs_rename = False
        zip_result: ZipCleanResult | None = None
        summary.processed += 1

        try:
            stem = _zip_stem_from_key(source_key)
            appid, _ = parse_appid_from_stem(stem or "") if stem else (None, None)

            if rename_objects:
                if not appid:
                    summary.skipped += 1
                    summary.add_sample(f"skip rename: {source_key} (no appid found)")
                else:
                    name_info = name_map.get(appid)
                    if not name_info:
                        fallback_key = _fallback_key(source_key, appid)
                        if fallback_to_appid and fallback_key != source_key:
                            needs_rename = True
                            target_key = fallback_key
                            summary.rename_planned += 1
                            summary.fallback_renames += 1
                        elif not fallback_to_appid:
                            summary.skipped += 1
                            summary.add_sample(f"skip rename: {source_key} (missing game name)")
                    else:
                        planned_key = _target_key(source_key, appid, name_info[0])
                        if not planned_key:
                            summary.skipped += 1
                            summary.add_sample(f"skip rename: {source_key} (unsafe game name)")
                        elif planned_key != source_key:
                            needs_rename = True
                            target_key = planned_key
                            summary.rename_planned += 1

            if needs_rename and target_key in object_keys:
                summary.skipped += 1
                summary.add_sample(f"skip object: {source_key} -> {target_key} (target exists)")
                continue

            if apply and needs_rename and _object_exists(client, target_key):
                summary.skipped += 1
                summary.add_sample(f"skip object: {source_key} -> {target_key} (target exists)")
                continue

            if clean_lua_comments:
                if source_size > max_zip_bytes:
                    summary.skipped += 1
                    summary.add_sample(f"skip comment clean: {source_key} ({source_size} bytes exceeds limit)")
                else:
                    response = client.get_object(Bucket=_BUCKET, Key=source_key)
                    body = response["Body"].read()
                    zip_result = clean_zip_comments(body, clean_extensions)
                    summary.comment_files_checked += zip_result.files_checked
                    summary.comment_files_cleaned += zip_result.files_cleaned
                    summary.lua_files_cleaned += zip_result.lua_files_cleaned
                    summary.manifest_files_cleaned += zip_result.manifest_files_cleaned
                    if zip_result.changed:
                        summary.comment_objects_changed += 1

            if summary.dry_run:
                if needs_rename:
                    summary.add_sample(f"rename: {source_key} -> {target_key}")
                if zip_result and zip_result.changed:
                    summary.add_sample(f"clean comments: {source_key} ({zip_result.files_cleaned} file(s))")
                continue

            if zip_result and zip_result.changed:
                client.put_object(
                    Bucket=_BUCKET,
                    Key=target_key,
                    Body=zip_result.data,
                    ContentType="application/zip",
                )
                summary.uploaded += 1
                if needs_rename:
                    summary.rename_applied += 1
                    summary.add_applied_sample(f"rename+upload: {source_key} -> {target_key}")
                else:
                    summary.add_applied_sample(f"clean+upload: {source_key}")
                _delete_source_if_needed(client, source_key, target_key, summary)
                continue

            if needs_rename:
                client.copy_object(
                    Bucket=_BUCKET,
                    Key=target_key,
                    CopySource={"Bucket": _BUCKET, "Key": source_key},
                    MetadataDirective="COPY",
                )
                summary.copied += 1
                summary.rename_applied += 1
                summary.add_applied_sample(f"rename: {source_key} -> {target_key}")
                _delete_source_if_needed(client, source_key, target_key, summary)

        except zipfile.BadZipFile:
            summary.add_error(f"Bad ZIP file: {source_key}")
        except ClientError as exc:
            if _is_access_denied(exc):
                summary.add_error(
                    "R2 write permission denied. The current R2 access key can read/list objects, "
                    "but it cannot write, copy, or delete objects. Create a Cloudflare R2 API token "
                    "with Object Read & Write permission for this bucket, then update "
                    "R2_ACCESS_KEY_ID and R2_SECRET_ACCESS_KEY in Railway."
                )
                break
            summary.add_error(f"Failed processing {source_key}: {exc}")
        except Exception as exc:
            summary.add_error(f"Failed processing {source_key}: {exc}")

    state_changed = False
    if apply:
        if summary.rename_applied:
            totals["rename_applied"] = int(totals.get("rename_applied", 0) or 0) + summary.rename_applied
            summary.total_rename_applied = int(totals["rename_applied"])
            state_changed = True
        if summary.comment_files_cleaned:
            totals["comment_files_cleaned"] = (
                int(totals.get("comment_files_cleaned", 0) or 0) + summary.comment_files_cleaned
            )
            summary.total_comment_files_cleaned = int(totals["comment_files_cleaned"])
            state_changed = True
        state_changed = _reconcile_rename_totals_from_r2(state, summary, prefix) or state_changed

    if use_queue and apply and not summary.errors:
        last_key_by_prefix = state.setdefault("last_key_by_prefix", {})
        if reached_end:
            last_key_by_prefix[prefix or ""] = ""
            summary.queue_resets += 1
        elif objects:
            last_key_by_prefix[prefix or ""] = next_start_after
        state_changed = True
    elif apply and (summary.steam_lookups or summary.steam_lookup_failed or summary.blacklisted):
        state_changed = True

    if state_changed:
        _save_state(state)

    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description="Maintain ZIP objects in Cloudflare R2.")
    parser.add_argument("--apply", action="store_true", help="Write changes to R2. Default is dry-run.")
    parser.add_argument("--prefix", default=bot_config.R2_MAINTENANCE_PREFIX)
    parser.add_argument("--limit", type=int, default=bot_config.R2_MAINTENANCE_MAX_OBJECTS)
    parser.add_argument("--no-rename", action="store_true")
    parser.add_argument("--no-clean-lua", "--no-clean-comments", action="store_true")
    parser.add_argument(
        "--clean-extensions",
        default=",".join(sorted(bot_config.R2_MAINTENANCE_CLEAN_EXTENSIONS)),
        help="Comma-separated extensions to clean inside ZIP files.",
    )
    parser.add_argument("--steam", action="store_true", help="Fetch missing names from Steam.")
    parser.add_argument("--max-steam", type=int, default=bot_config.R2_MAINTENANCE_MAX_STEAM_LOOKUPS)
    parser.add_argument("--steam-delay", type=float, default=bot_config.R2_MAINTENANCE_STEAM_DELAY_SECONDS)
    parser.add_argument("--no-queue", action="store_true", help="Do not continue from saved scan position.")
    parser.add_argument("--no-fallback", action="store_true", help="Skip missing Steam names instead of using AppID.zip.")
    parser.add_argument("--ignore-blacklist", action="store_true", help="Retry AppIDs with repeated Steam lookup failures.")
    args = parser.parse_args()

    summary = run_r2_maintenance(
        apply=args.apply,
        prefix=args.prefix,
        limit=args.limit,
        rename_objects=not args.no_rename,
        clean_lua_comments=not args.no_clean_lua,
        use_steam=args.steam,
        max_steam_lookups=args.max_steam,
        steam_delay_seconds=args.steam_delay,
        clean_extensions={item.strip().lower().lstrip(".") for item in args.clean_extensions.split(",") if item.strip()},
        use_queue=not args.no_queue,
        fallback_to_appid=not args.no_fallback,
        ignore_blacklist=args.ignore_blacklist,
    )

    print("R2 maintenance summary")
    for name, value in summary.to_fields().items():
        print(f"{name}: {value}")
    if summary.samples:
        print("\nSamples")
        for sample in summary.samples:
            print(f"- {sample}")
    if summary.applied_samples:
        print("\nApplied samples")
        for sample in summary.applied_samples:
            print(f"- {sample}")
    if summary.errors:
        print("\nErrors")
        for error in summary.errors:
            print(f"- {error}")
    return 1 if summary.errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
