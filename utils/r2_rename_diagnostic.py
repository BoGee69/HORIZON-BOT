"""
Read-only evidence collector for R2 ZIP rename leftovers.

This module does not copy, delete, upload, or rewrite R2 objects. It only reads
R2 inventory/cache and SQLite/cache metadata so TriadBot can answer owner
questions like "kenapa masih ada yang belum di-rename?" with evidence instead
of assumptions.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

import config as bot_config
from utils.ai_caretaker import sanitize_text
from utils.database import R2InventoryDB
from utils.r2_presign import _BUCKET, _PRESIGN_ENABLED, _make_client
from utils.r2_maintenance import (
    _build_name_map,
    _is_blacklisted,
    _load_state,
    _list_zip_objects,
    _object_exists,
    _target_key,
    _zip_stem_from_key,
)
from utils.rename_database_files import parse_appid_from_stem

log = logging.getLogger(__name__)


_REASON_LABELS = {
    "no_appid": "AppID tidak bisa diparse dari nama file",
    "missing_game_name": "Nama game tidak ditemukan di SQLite/cache/Steam cache",
    "unsafe_game_name": "Nama game ada, tapi tidak aman/valid untuk nama file",
    "target_exists": "Target nama baru sudah ada di R2",
    "steam_blacklisted": "AppID masuk blacklist Steam lookup sementara",
    "steam_failed_before": "AppID punya riwayat Steam lookup gagal",
    "rename_possible_now": "Secara data sekarang bisa di-rename; kemungkinan belum kena batch/apply run",
    "already_formatted": "Sudah format rapi saat diagnostic berjalan",
    "not_zip": "Bukan ZIP",
    "error": "Error saat diagnostic",
}


@dataclass
class R2RenameDiagnostic:
    prefix: str
    source: str
    sample_limit: int
    total_zip_known: int = 0
    already_formatted_known: int = 0
    appid_only_known: int = 0
    unknown_format_known: int = 0
    pending_known: int = 0
    pending_checked: int = 0
    checked_keys: int = 0
    target_exists_checks: int = 0
    target_exists_checks_limited: bool = False
    full_inventory_available: bool = False
    reason_counts: dict[str, int] = field(default_factory=dict)
    samples: dict[str, list[str]] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

    def add_reason(self, reason: str, sample: str | None = None, limit: int = 6) -> None:
        reason = sanitize_text(reason).strip().lower().replace(" ", "_") or "error"
        self.reason_counts[reason] = int(self.reason_counts.get(reason, 0) or 0) + 1
        if sample:
            bucket = self.samples.setdefault(reason, [])
            if len(bucket) < limit:
                bucket.append(sanitize_text(sample)[:260])

    def add_error(self, message: str) -> None:
        self.add_reason("error", message)
        if len(self.errors) < 8:
            self.errors.append(sanitize_text(message)[:400])

    def to_fields(self) -> dict[str, str]:
        fields = {
            "Mode": "READ-ONLY DIAGNOSTIC",
            "Prefix": self.prefix or "(bucket root)",
            "Source": self.source,
            "Sample limit": str(self.sample_limit),
            "Total ZIP known": str(self.total_zip_known),
            "Already formatted known": str(self.already_formatted_known),
            "AppID-only known": str(self.appid_only_known),
            "Unknown format known": str(self.unknown_format_known),
            "Pending known": str(self.pending_known),
            "Pending checked": str(self.pending_checked),
            "Target exists checks": str(self.target_exists_checks),
            "Full inventory available": str(self.full_inventory_available),
            "Errors": str(len(self.errors)),
        }
        for reason, count in sorted(self.reason_counts.items()):
            fields[f"Reason {reason}"] = str(count)
        return fields


def _load_zip_keys_from_inventory_cache(prefix: str) -> tuple[set[str], str, bool]:
    try:
        db = R2InventoryDB()
        keys = {key for key in db.get_all_keys(prefix) if key.lower().endswith(".zip")}
        if keys:
            return keys, "sqlite-r2-inventory-cache", True
    except Exception as exc:
        log.debug("R2 rename diagnostic cache read failed: %r", exc)
    return set(), "", False


def _load_zip_keys_live(prefix: str, max_objects: int) -> tuple[set[str], str, bool, list[str]]:
    if not _PRESIGN_ENABLED:
        return set(), "unavailable", False, ["R2 credentials are incomplete."]
    try:
        client = _make_client()
        objects, _last_key, reached_end = _list_zip_objects(client, prefix, max_objects=max(1, max_objects), start_after="")
        return {str(item.get("Key") or "") for item in objects if str(item.get("Key") or "").lower().endswith(".zip")}, "live-r2-list-objects", reached_end, []
    except Exception as exc:
        return set(), "live-r2-list-objects-error", False, [f"Failed to list R2 objects: {exc}"]


def _classify_zip_key(key: str) -> tuple[str, str | None, str | None]:
    stem = _zip_stem_from_key(key)
    if not stem:
        return "not_zip", None, None
    appid, parsed_name = parse_appid_from_stem(stem)
    if appid and parsed_name:
        return "already_formatted", appid, parsed_name
    if appid:
        return "appid_only", appid, None
    return "unknown_format", None, None


def diagnose_r2_rename_leftovers(
    *,
    prefix: str | None = None,
    sample_limit: int | None = None,
    use_inventory_cache: bool = True,
    live_scan_fallback: bool = True,
    live_scan_limit: int | None = None,
    target_exists_check_limit: int | None = None,
) -> R2RenameDiagnostic:
    """Return a read-only evidence report for ZIPs that are not yet renamed.

    The diagnostic proves only what it can read. If it can only sample a limited
    live listing, the report says so through ``full_inventory_available=False``.
    """
    prefix = (prefix if prefix is not None else bot_config.R2_MAINTENANCE_PREFIX).lstrip("/")
    sample_limit = max(1, int(sample_limit if sample_limit is not None else bot_config.AI_CHAT_R2_DIAGNOSTIC_LIMIT))
    live_scan_limit = max(sample_limit, int(live_scan_limit if live_scan_limit is not None else bot_config.AI_CHAT_R2_DIAGNOSTIC_LIVE_SCAN_LIMIT))
    target_exists_check_limit = max(0, int(target_exists_check_limit if target_exists_check_limit is not None else bot_config.AI_CHAT_R2_DIAGNOSTIC_TARGET_EXISTS_CHECKS))

    keys: set[str] = set()
    source = "unavailable"
    full_inventory = False
    errors: list[str] = []

    if use_inventory_cache:
        keys, source, full_inventory = _load_zip_keys_from_inventory_cache(prefix)

    if not keys and live_scan_fallback:
        keys, source, full_inventory, errors = _load_zip_keys_live(prefix, live_scan_limit)

    diag = R2RenameDiagnostic(
        prefix=prefix,
        source=source,
        sample_limit=sample_limit,
        full_inventory_available=full_inventory,
    )
    for error in errors:
        diag.add_error(error)
    if not keys:
        return diag

    diag.total_zip_known = len(keys)
    classified: list[tuple[str, str, str | None, str | None]] = []
    pending_keys: list[tuple[str, str, str | None, str | None]] = []

    for key in sorted(keys):
        category, appid, parsed_name = _classify_zip_key(key)
        classified.append((key, category, appid, parsed_name))
        if category == "already_formatted":
            diag.already_formatted_known += 1
        elif category == "appid_only":
            diag.appid_only_known += 1
            pending_keys.append((key, category, appid, parsed_name))
        elif category == "unknown_format":
            diag.unknown_format_known += 1
            pending_keys.append((key, category, appid, parsed_name))

    diag.pending_known = diag.appid_only_known + diag.unknown_format_known
    diag.checked_keys = len(classified)

    # Build evidence map from current R2 object names, Steam cache, and SQLite.
    # This is read-only and gives proof whether a clean target name is known.
    try:
        name_map = _build_name_map(keys)
    except Exception as exc:
        name_map = {}
        diag.add_error(f"Failed to build name map from SQLite/cache/R2 names: {exc}")

    try:
        state = _load_state()
    except Exception:
        state = {"steam_failures": {}}

    client = None
    if source != "sqlite-r2-inventory-cache" and _PRESIGN_ENABLED and target_exists_check_limit > 0:
        try:
            client = _make_client()
        except Exception:
            client = None

    for key, category, appid, _parsed_name in pending_keys[:sample_limit]:
        diag.pending_checked += 1
        if category == "unknown_format" or not appid:
            diag.add_reason("no_appid", f"{key} → tidak ada AppID valid dari nama file")
            continue

        name_info = name_map.get(appid)
        if not name_info:
            failure_record = (state.get("steam_failures") or {}).get(str(appid)) if isinstance(state, dict) else None
            if _is_blacklisted(state, str(appid), int(bot_config.R2_MAINTENANCE_BLACKLIST_THRESHOLD)):
                diag.add_reason("steam_blacklisted", f"{key} → AppID {appid} masuk blacklist lookup sementara")
            elif isinstance(failure_record, dict) and int(failure_record.get("count", 0) or 0) > 0:
                diag.add_reason("steam_failed_before", f"{key} → AppID {appid} punya riwayat Steam lookup gagal {int(failure_record.get('count', 0) or 0)}x")
            else:
                diag.add_reason("missing_game_name", f"{key} → AppID {appid} tidak punya nama di SQLite/cache/R2 name map")
            continue

        target_key = _target_key(key, str(appid), str(name_info[0]))
        if not target_key:
            diag.add_reason("unsafe_game_name", f"{key} → nama `{sanitize_text(str(name_info[0]))[:80]}` tidak valid untuk target file")
            continue

        target_exists = target_key in keys
        if not target_exists and client is not None and diag.target_exists_checks < target_exists_check_limit:
            try:
                diag.target_exists_checks += 1
                target_exists = _object_exists(client, target_key)
            except Exception as exc:
                diag.add_error(f"Failed checking target existence for {target_key}: {exc}")
        elif client is not None and diag.target_exists_checks >= target_exists_check_limit:
            diag.target_exists_checks_limited = True

        if target_exists:
            diag.add_reason("target_exists", f"{key} → target sudah ada: {target_key}")
            continue

        source_name = sanitize_text(str(name_info[1] if len(name_info) > 1 else "unknown"))
        diag.add_reason("rename_possible_now", f"{key} → bisa menjadi {target_key} (name source: {source_name})")

    return diag


async def diagnose_r2_rename_leftovers_async(**kwargs) -> R2RenameDiagnostic:
    return await asyncio.to_thread(diagnose_r2_rename_leftovers, **kwargs)


def format_r2_rename_diagnostic(diag: R2RenameDiagnostic, *, detailed: bool = False) -> list[str]:
    """Format diagnostic results for chat.

    Default output is intentionally short and human-readable. Detailed file
    examples/log-like evidence are shown only when the owner asks for detail.
    """
    brief_enabled = bool(getattr(bot_config, "AI_CHAT_BRIEF_DIAGNOSTIC", True)) and not detailed
    show_samples = bool(getattr(bot_config, "AI_CHAT_SHOW_EVIDENCE_SAMPLES", False)) or detailed
    max_reasons = max(1, int(getattr(bot_config, "AI_CHAT_DIAGNOSTIC_MAX_REASONS", 4) or 4))

    pending_left_unchecked = max(0, int(diag.pending_known or 0) - int(diag.pending_checked or 0))
    sorted_reasons = sorted(diag.reason_counts.items(), key=lambda item: item[1], reverse=True)

    possible = int(diag.reason_counts.get("rename_possible_now", 0) or 0)
    blocking = sum(
        int(diag.reason_counts.get(key, 0) or 0)
        for key in (
            "no_appid", "missing_game_name", "unsafe_game_name", "target_exists",
            "steam_blacklisted", "steam_failed_before", "error",
        )
    )

    if brief_enabled:
        lines: list[str] = ["**Jawaban singkatnya:**"]
        if sorted_reasons:
            top_reason, top_count = sorted_reasons[0]
            top_label = _REASON_LABELS.get(top_reason, top_reason.replace("_", " "))
            lines.append(
                f"Dari `{diag.pending_checked:,}` file yang saya cek, penyebab terbesar adalah **{top_label}**: `{top_count:,}` file."
            )
        else:
            lines.append("Saya belum punya alasan detail yang terbukti dari sample diagnostic ini.")

        if blocking and possible:
            lines.append("Jadi masalahnya campuran: ada yang memang kena blocker, ada juga yang sebenarnya sudah bisa di-rename tapi belum kena batch/apply.")
        elif blocking:
            lines.append("Jadi ini bukan sekadar belum diproses; sample yang dicek memang punya blocker data.")
        elif possible:
            lines.append("Jadi kemungkinan besarnya bukan data rusak, tapi belum kena batch/apply run.")

        if sorted_reasons:
            lines.append("")
            lines.append("Bukti ringkas:")
            for reason, count in sorted_reasons[:max_reasons]:
                label = _REASON_LABELS.get(reason, reason.replace("_", " "))
                lines.append(f"- {label}: `{count:,}`")

        if pending_left_unchecked:
            lines.append(f"- Batas analisis: masih ada `{pending_left_unchecked:,}` pending yang belum dicek detail karena limit sample.")
        lines.append("Kalau mau bukti file/log-nya, tanya: `detail diagnostic rename`.")
        return lines

    lines = []
    lines.append("**Diagnostic sisa rename R2 (read-only)**")
    lines.append("- Mode: `read-only`, tidak copy/delete/upload file.")
    lines.append(f"- Source: `{diag.source}`.")
    lines.append(f"- Total ZIP yang diketahui: `{diag.total_zip_known:,}`.")
    lines.append(f"- Sudah format rapi: `{diag.already_formatted_known:,}`.")
    lines.append(f"- Belum rapi yang diketahui: `{diag.pending_known:,}`.")
    lines.append(f"  - AppID-only: `{diag.appid_only_known:,}`.")
    lines.append(f"  - Format tidak bisa diparse: `{diag.unknown_format_known:,}`.")
    lines.append(f"- Pending yang dianalisis detail: `{diag.pending_checked:,}` dari batas sample `{diag.sample_limit:,}`.")
    if not diag.full_inventory_available:
        lines.append("- Batas bukti: inventory penuh belum tersedia; diagnostic ini memakai listing/sample terbatas.")
    if diag.target_exists_checks_limited:
        lines.append("- Batas bukti: pengecekan target-exists dibatasi agar chat tidak lambat.")

    lines.append("")
    lines.append("**Alasan yang terbukti dari diagnostic**")
    if sorted_reasons:
        for reason, count in sorted_reasons:
            label = _REASON_LABELS.get(reason, reason.replace("_", " "))
            lines.append(f"- {label}: `{count:,}`")
    else:
        lines.append("- Belum ada reason detail yang bisa dibuktikan dari sample ini.")

    lines.append("")
    lines.append("**Kesimpulan berbasis bukti**")
    if possible and not blocking:
        lines.append("- Sample yang dicek tidak punya blocker data. Ini menunjukkan sisa rename kemungkinan besar belum kena batch/apply run, bukan karena datanya rusak.")
    elif possible and blocking:
        lines.append("- Ada dua kelompok: sebagian bisa di-rename sekarang, sebagian punya blocker yang terbukti dari diagnostic.")
    elif blocking:
        lines.append("- Sample yang dicek punya blocker data; lihat breakdown alasan di atas.")
    else:
        lines.append("- Bukti belum cukup untuk menyimpulkan penyebab detail. Jalankan diagnostic dengan sample lebih besar atau rebuild inventory R2 dulu.")

    if pending_left_unchecked:
        lines.append(f"- Masih ada `{pending_left_unchecked:,}` pending known yang belum dianalisis detail karena batas sample.")

    if show_samples:
        sample_lines: list[str] = []
        for reason, samples in sorted(diag.samples.items(), key=lambda item: len(item[1]), reverse=True):
            label = _REASON_LABELS.get(reason, reason.replace("_", " "))
            for sample in samples[:2]:
                sample_lines.append(f"- {label}: `{sample}`")
            if len(sample_lines) >= 6:
                break
        if sample_lines:
            lines.append("")
            lines.append("**Contoh bukti file**")
            lines.extend(sample_lines[:6])

    if diag.errors:
        lines.append("")
        lines.append("**Error diagnostic**")
        for error in diag.errors[:4]:
            lines.append(f"- `{sanitize_text(error)[:260]}`")

    return lines
