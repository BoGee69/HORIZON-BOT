"""Persistent learning memory for TriadBot owner/admin corrections.

This module does not fine-tune the AI model.  It stores small, auditable
behavior rules learned from trusted owner/admin feedback, then feeds relevant
rules back into routing and prompts so TriadBot can stop repeating the same
mistakes across restarts.
"""
from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import config as bot_config


def sanitize_text(value: Any) -> str:
    """Small local sanitizer to keep learning memory independent from Discord/caretaker imports."""
    text = str(value or "")
    text = text.replace("\x00", "")
    text = re.sub(r"[\x01-\x08\x0b\x0c\x0e-\x1f]+", " ", text)
    return text.strip()

_SECRET_RE = re.compile(
    r"(?i)(discord[_-]?token|github[_-]?token|api[_-]?key|secret|password|passwd|jwt[_-]?secret|webhook|bearer\s+[a-z0-9._\-]{20,}|github_pat_[a-z0-9_]{20,}|[a-z0-9_\-]{32,}\.[a-z0-9_\-]{6,}\.[a-z0-9_\-]{20,})"
)

_UNSAFE_LEARNING_PHRASES = (
    "jangan pakai approval",
    "tanpa approval",
    "skip approval",
    "bypass approval",
    "auto approve",
    "auto-approve",
    "hapus approval",
    "tampilkan token",
    "show token",
    "show secret",
    "print secret",
    "kirim secret",
    "leak secret",
    "ignore safety",
    "abaikan safety",
    "langsung hapus semua",
    "delete everything",
    "hapus semua file",
    "hapus semua r2",
    "edit live code langsung",
    "edit live tanpa approval",
)

_CORRECTION_MARKERS = (
    "itu salah",
    "salah",
    "bukan gitu",
    "bukan begitu",
    "maksud gw",
    "maksud gue",
    "maksud saya",
    "harusnya",
    "seharusnya",
    "ke depannya",
    "kedepannya",
    "next time",
    "lain kali",
    "jangan begitu",
    "jangan gitu",
    "ingat",
    "remember",
    "catat",
    "simpen",
    "simpan",
)

_READONLY_HINTS = (
    "read-only",
    "status",
    "pertanyaan",
    "tanya",
    "jumlah",
    "berapa",
    "sisa",
    "cek",
    "bukan perintah",
    "bukan nyuruh",
    "bukan action",
    "jangan proposal",
    "bukan proposal",
    "tidak perlu approval",
    "nggak perlu approval",
)

_ACTION_HINTS = (
    "perintah",
    "action",
    "proposal",
    "approval",
    "jalankan",
    "rapikan",
    "eksekusi",
    "create pr",
    "buat pr",
)

_STOPWORDS = {
    "yang", "dan", "atau", "untuk", "dari", "dengan", "kalau", "kalo", "gw", "gue",
    "saya", "aku", "itu", "ini", "nya", "harus", "harusnya", "maksud", "bukan",
    "jangan", "jadi", "sebagai", "the", "and", "or", "to", "of", "a", "an", "is",
}


@dataclass(slots=True)
class LearnedRule:
    id: int
    scope: str
    topic: str
    trigger_text: str
    lesson: str
    route_hint: str
    confidence: float
    hits: int
    updated_at: str

    def to_prompt_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "scope": self.scope,
            "topic": self.topic,
            "trigger_text": self.trigger_text,
            "lesson": self.lesson,
            "route_hint": self.route_hint,
            "confidence": self.confidence,
            "hits": self.hits,
            "updated_at": self.updated_at,
        }


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _tokenize(text: str) -> set[str]:
    words = re.findall(r"[a-zA-Z0-9_]+", sanitize_text(text).lower())
    return {w for w in words if len(w) >= 3 and w not in _STOPWORDS}


def _infer_topic(text: str) -> str:
    lower = sanitize_text(text).lower()
    topic_map = [
        ("r2_rename", ("r2", "rename", "renaming", "zip", "appid", "nama game", "storage", "bucket")),
        ("incident_awareness", ("warning", "error", "alert", "ada apa", "kenapa", "terakhir", "incident")),
        ("github_patch", ("github", "patch", "branch", "pull request", "pr", "commit", "repo", "file code")),
        ("database", ("database", "sqlite", "db", "katalog", "catalog")),
        ("opendir", ("opendir", "open dir", "upload", "download", "sync file")),
        ("steam_sync", ("steam", "steam db", "appid", "sync steam")),
        ("security", ("security", "spam", "mention", "link", "timeout", "ban", "kick", "moderation")),
        ("time_date", ("jam", "tanggal", "hari ini", "time", "date")),
        ("identity", ("triadbot", "identitas", "nama", "codex", "siapa")),
        ("conversation_style", ("jawab", "bahasa", "gaya", "context", "konteks", "nyambung")),
    ]
    hits = [(topic, sum(1 for marker in markers if marker in lower)) for topic, markers in topic_map]
    hits = [item for item in hits if item[1] > 0]
    if hits:
        return max(hits, key=lambda item: item[1])[0]
    return "general"


def _infer_route_hint(lesson: str, correction: str, previous_user: str = "") -> str:
    combined = f"{lesson}\n{correction}\n{previous_user}".lower()
    if any(hint in combined for hint in _READONLY_HINTS):
        return "read_only"
    if any(hint in combined for hint in _ACTION_HINTS):
        # Only action if it is not explicitly negated.
        if not any(neg in combined for neg in ("bukan proposal", "jangan proposal", "bukan action", "bukan perintah", "jangan approval")):
            return "action"
    return "context_rule"


def _looks_sensitive_or_unsafe(text: str) -> tuple[bool, str]:
    lower = sanitize_text(text).lower()
    if _SECRET_RE.search(text):
        return True, "mengandung pola token/secret"
    for phrase in _UNSAFE_LEARNING_PHRASES:
        if phrase in lower:
            return True, f"rule mencoba melemahkan safety: {phrase}"
    return False, ""


class AILearningMemory:
    def __init__(self, path: str | Path | None = None):
        configured = path or getattr(bot_config, "AI_LEARNING_MEMORY_PATH", None)
        self.path = Path(configured or (Path(getattr(bot_config, "DATA_DIR", "data")) / "ai_learning_memory.sqlite3"))
        self.enabled = bool(getattr(bot_config, "AI_LEARNING_ENABLED", True))
        self.max_rules = max(10, int(getattr(bot_config, "AI_LEARNING_MAX_RULES", 300) or 300))
        self._ensure_schema()

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(str(self.path), timeout=10)
        con.row_factory = sqlite3.Row
        return con

    def _ensure_schema(self) -> None:
        if not self.enabled:
            return
        try:
            with self._connect() as con:
                con.execute(
                    """
                    CREATE TABLE IF NOT EXISTS ai_memory_rules (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        scope TEXT NOT NULL DEFAULT 'owner',
                        topic TEXT NOT NULL,
                        trigger_text TEXT NOT NULL,
                        lesson TEXT NOT NULL,
                        route_hint TEXT NOT NULL DEFAULT 'context_rule',
                        confidence REAL DEFAULT 0.8,
                        source_user_id TEXT,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        hits INTEGER DEFAULT 0,
                        active INTEGER DEFAULT 1
                    )
                    """
                )
                con.execute(
                    """
                    CREATE TABLE IF NOT EXISTS ai_mistakes (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        user_message TEXT NOT NULL,
                        bot_response TEXT,
                        correction TEXT NOT NULL,
                        lesson_id INTEGER,
                        source_user_id TEXT,
                        created_at TEXT NOT NULL
                    )
                    """
                )
                con.execute("CREATE INDEX IF NOT EXISTS idx_ai_memory_rules_active_topic ON ai_memory_rules(active, topic)")
                con.execute("CREATE INDEX IF NOT EXISTS idx_ai_memory_rules_updated ON ai_memory_rules(updated_at)")
        except Exception:
            # Learning memory is useful, never critical.
            pass

    def correction_signal(self, text: str) -> bool:
        if not self.enabled:
            return False
        lower = sanitize_text(text).lower()
        return any(marker in lower for marker in _CORRECTION_MARKERS)

    def build_lesson_from_correction(
        self,
        *,
        correction_text: str,
        history: Iterable[dict[str, str]] | None = None,
    ) -> dict[str, str] | None:
        correction = sanitize_text(correction_text).strip()
        if not correction or not self.correction_signal(correction):
            return None
        unsafe, reason = _looks_sensitive_or_unsafe(correction)
        if unsafe:
            return {
                "blocked_reason": reason,
                "lesson": "",
                "topic": "blocked",
                "trigger_text": "",
                "route_hint": "blocked",
            }

        previous_user = ""
        previous_assistant = ""
        if history:
            items = list(history)
            for item in reversed(items):
                role = sanitize_text(str(item.get("role") or "")).lower()
                text = sanitize_text(str(item.get("text") or "")).strip()
                if not previous_user and role == "user":
                    previous_user = text
                elif not previous_assistant and role == "assistant":
                    previous_assistant = text
                if previous_user and previous_assistant:
                    break

        trigger = previous_user or correction
        combined = f"{trigger}\n{correction}"
        topic = _infer_topic(combined)
        route_hint = _infer_route_hint(correction, correction, previous_user)

        # Keep the lesson short, operational, and easy to inject into prompts.
        lesson = correction
        lesson = re.sub(r"^(?:itu salah|salah|bukan gitu|bukan begitu)[:,\s-]*", "", lesson, flags=re.I).strip()
        if not lesson:
            lesson = correction
        if previous_user and route_hint == "read_only":
            lesson = (
                f"When the owner says something like `{previous_user[:180]}`, treat it as a read-only/status question. "
                f"Do not create an approval/proposal unless the owner uses a clear action verb. Owner correction: {lesson}"
            )
        elif previous_user:
            lesson = f"For messages similar to `{previous_user[:180]}`, follow this owner correction: {lesson}"
        else:
            lesson = f"Owner preference/correction: {lesson}"

        return {
            "topic": topic,
            "trigger_text": trigger[:500],
            "lesson": lesson[:900],
            "route_hint": route_hint,
        }

    def save_correction(
        self,
        *,
        correction_text: str,
        source_user_id: int | str,
        history: Iterable[dict[str, str]] | None = None,
        scope: str = "owner",
    ) -> tuple[bool, str, LearnedRule | None]:
        if not self.enabled:
            return False, "AI learning memory is disabled.", None
        data = self.build_lesson_from_correction(correction_text=correction_text, history=history)
        if not data:
            return False, "Pesan ini belum cukup jelas sebagai koreksi/pelajaran.", None
        if data.get("blocked_reason"):
            return False, f"Saya tidak menyimpan rule itu karena {data['blocked_reason']}.", None

        unsafe, reason = _looks_sensitive_or_unsafe(json.dumps(data, ensure_ascii=False))
        if unsafe:
            return False, f"Saya tidak menyimpan rule itu karena {reason}.", None

        now = _utc_now()
        try:
            with self._connect() as con:
                # Merge extremely similar active lessons instead of creating endless duplicates.
                existing = con.execute(
                    """
                    SELECT * FROM ai_memory_rules
                    WHERE active=1 AND topic=? AND route_hint=?
                    ORDER BY updated_at DESC
                    LIMIT 25
                    """,
                    (data["topic"], data["route_hint"]),
                ).fetchall()
                trigger_tokens = _tokenize(data["trigger_text"])
                lesson_tokens = _tokenize(data["lesson"])
                best_id = None
                best_score = 0.0
                for row in existing:
                    row_tokens = _tokenize(str(row["trigger_text"] or "")) | _tokenize(str(row["lesson"] or ""))
                    if not row_tokens or not (trigger_tokens or lesson_tokens):
                        continue
                    overlap = len((trigger_tokens | lesson_tokens) & row_tokens)
                    score = overlap / max(1, len((trigger_tokens | lesson_tokens) | row_tokens))
                    if score > best_score:
                        best_score = score
                        best_id = int(row["id"])

                if best_id and best_score >= 0.45:
                    con.execute(
                        """
                        UPDATE ai_memory_rules
                        SET trigger_text=?, lesson=?, confidence=min(confidence + 0.05, 1.0), updated_at=?
                        WHERE id=?
                        """,
                        (data["trigger_text"], data["lesson"], now, best_id),
                    )
                    rule_id = best_id
                else:
                    cur = con.execute(
                        """
                        INSERT INTO ai_memory_rules
                        (scope, topic, trigger_text, lesson, route_hint, confidence, source_user_id, created_at, updated_at, hits, active)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 1)
                        """,
                        (
                            scope,
                            data["topic"],
                            data["trigger_text"],
                            data["lesson"],
                            data["route_hint"],
                            0.82,
                            str(source_user_id),
                            now,
                            now,
                        ),
                    )
                    rule_id = int(cur.lastrowid)

                con.execute(
                    """
                    INSERT INTO ai_mistakes
                    (user_message, bot_response, correction, lesson_id, source_user_id, created_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        data["trigger_text"],
                        self._previous_assistant_from_history(history),
                        sanitize_text(correction_text)[:1000],
                        rule_id,
                        str(source_user_id),
                        now,
                    ),
                )
                self._trim(con)
                row = con.execute("SELECT * FROM ai_memory_rules WHERE id=?", (rule_id,)).fetchone()
                return True, "saved", self._row_to_rule(row) if row else None
        except Exception as exc:
            return False, f"Gagal menyimpan learning memory: {exc!r}", None

    @staticmethod
    def _previous_assistant_from_history(history: Iterable[dict[str, str]] | None) -> str:
        if not history:
            return ""
        for item in reversed(list(history)):
            if sanitize_text(str(item.get("role") or "")).lower() == "assistant":
                return sanitize_text(str(item.get("text") or ""))[:1000]
        return ""

    def _trim(self, con: sqlite3.Connection) -> None:
        max_rules = max(10, self.max_rules)
        rows = con.execute(
            "SELECT id FROM ai_memory_rules WHERE active=1 ORDER BY updated_at DESC, hits DESC LIMIT -1 OFFSET ?",
            (max_rules,),
        ).fetchall()
        if rows:
            con.executemany("UPDATE ai_memory_rules SET active=0 WHERE id=?", [(int(row["id"]),) for row in rows])

    @staticmethod
    def _row_to_rule(row: sqlite3.Row | None) -> LearnedRule | None:
        if not row:
            return None
        return LearnedRule(
            id=int(row["id"]),
            scope=str(row["scope"] or "owner"),
            topic=str(row["topic"] or "general"),
            trigger_text=str(row["trigger_text"] or ""),
            lesson=str(row["lesson"] or ""),
            route_hint=str(row["route_hint"] or "context_rule"),
            confidence=float(row["confidence"] or 0.8),
            hits=int(row["hits"] or 0),
            updated_at=str(row["updated_at"] or ""),
        )

    def relevant_rules(self, text: str, *, limit: int | None = None) -> list[LearnedRule]:
        if not self.enabled:
            return []
        clean = sanitize_text(text).strip()
        if not clean:
            return []
        limit = max(1, int(limit or getattr(bot_config, "AI_LEARNING_PROMPT_RULE_LIMIT", 8) or 8))
        text_tokens = _tokenize(clean)
        topic = _infer_topic(clean)
        min_score = float(getattr(bot_config, "AI_LEARNING_MIN_MATCH_SCORE", 0.12) or 0.12)
        try:
            with self._connect() as con:
                rows = con.execute(
                    """
                    SELECT * FROM ai_memory_rules
                    WHERE active=1
                    ORDER BY updated_at DESC, hits DESC
                    LIMIT 200
                    """
                ).fetchall()
                scored: list[tuple[float, LearnedRule]] = []
                for row in rows:
                    rule = self._row_to_rule(row)
                    if not rule:
                        continue
                    rule_blob = f"{rule.topic} {rule.trigger_text} {rule.lesson} {rule.route_hint}"
                    rule_tokens = _tokenize(rule_blob)
                    score = 0.0
                    if rule.topic == topic:
                        score += 0.30
                    if rule.trigger_text and rule.trigger_text.lower() in clean.lower():
                        score += 0.60
                    if clean.lower() in rule.trigger_text.lower():
                        score += 0.40
                    if text_tokens and rule_tokens:
                        overlap = len(text_tokens & rule_tokens)
                        score += overlap / max(4, len(text_tokens | rule_tokens))
                    if rule.route_hint == "read_only" and any(h in clean.lower() for h in ("berapa", "jumlah", "sisa", "status", "cek", "warning", "ada apa")):
                        score += 0.15
                    score *= max(0.5, min(1.2, rule.confidence + 0.2))
                    if score >= min_score:
                        scored.append((score, rule))

                scored.sort(key=lambda item: item[0], reverse=True)
                picked = [rule for _, rule in scored[:limit]]
                if picked:
                    con.executemany(
                        "UPDATE ai_memory_rules SET hits=hits+1, updated_at=? WHERE id=?",
                        [( _utc_now(), rule.id) for rule in picked],
                    )
                return picked
        except Exception:
            return []

    def route_hint_for_text(self, text: str) -> str:
        rules = self.relevant_rules(text, limit=3)
        if not rules:
            return ""
        # Learned read-only corrections should be allowed to override keyword action routing.
        readonly = [rule for rule in rules if rule.route_hint == "read_only" and rule.confidence >= 0.70]
        if readonly:
            return "read_only"
        action = [rule for rule in rules if rule.route_hint == "action" and rule.confidence >= 0.85]
        if action:
            return "action"
        return "context_rule"

    def list_recent(self, *, limit: int = 10) -> list[LearnedRule]:
        if not self.enabled:
            return []
        try:
            with self._connect() as con:
                rows = con.execute(
                    "SELECT * FROM ai_memory_rules WHERE active=1 ORDER BY updated_at DESC LIMIT ?",
                    (max(1, int(limit)),),
                ).fetchall()
                return [rule for row in rows if (rule := self._row_to_rule(row))]
        except Exception:
            return []


def get_learning_memory() -> AILearningMemory | None:
    if not bool(getattr(bot_config, "AI_LEARNING_ENABLED", True)):
        return None
    try:
        return AILearningMemory()
    except Exception:
        return None


def route_hint_from_learning(text: str) -> str:
    memory = get_learning_memory()
    if not memory:
        return ""
    return memory.route_hint_for_text(text)


def relevant_learning_rules_for_prompt(text: str, *, limit: int | None = None) -> list[dict[str, Any]]:
    memory = get_learning_memory()
    if not memory:
        return []
    return [rule.to_prompt_dict() for rule in memory.relevant_rules(text, limit=limit)]
