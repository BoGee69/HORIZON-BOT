"""
Daily /gen usage limiter.

Limits are counted in UTC days and reset at 00:00 UTC.
"""
import json
import logging
import threading
from datetime import datetime, time, timezone, timedelta
from pathlib import Path
from typing import Dict, Tuple

from config import GEN_DAILY_LIMIT, GEN_USAGE_PATH

log = logging.getLogger(__name__)


class DailyGenLimiter:
    def __init__(self, path: Path = GEN_USAGE_PATH, limit: int = GEN_DAILY_LIMIT):
        self.path = path
        self.limit = limit
        self._lock = threading.RLock()
        self.usage_date = self._today_key()
        self.counts: Dict[str, int] = {}
        self.load()

    def load(self):
        with self._lock:
            if not self.path.exists():
                return
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                if data.get("date") == self.usage_date:
                    self.counts = {str(k): int(v) for k, v in data.get("counts", {}).items()}
                else:
                    self.save()
            except Exception as exc:
                log.warning("Failed to load gen usage state: %s", exc)
                self.counts = {}

    def check(self, user_id: int) -> Tuple[bool, int, int]:
        with self._lock:
            self._rollover_if_needed()
            used = self.counts.get(str(user_id), 0)
            remaining = max(0, self.limit - used)
            return used < self.limit, used, remaining

    def check_and_consume(self, user_id: int) -> Tuple[bool, int, int]:
        """Atomically check the limit and consume one use if allowed.

        Returns (allowed, used_after, remaining_after).
        If not allowed, counts are not modified and remaining is 0.
        """
        with self._lock:
            self._rollover_if_needed()
            key = str(user_id)
            used = self.counts.get(key, 0)
            if used >= self.limit:
                return False, used, 0
            used += 1
            self.counts[key] = used
            self.save()
            return True, used, max(0, self.limit - used)

    def get_usage(self, user_id: int) -> Tuple[int, int]:
        with self._lock:
            self._rollover_if_needed()
            used = self.counts.get(str(user_id), 0)
            return used, max(0, self.limit - used)

    def consume(self, user_id: int) -> Tuple[int, int]:
        with self._lock:
            self._rollover_if_needed()
            key = str(user_id)
            used = self.counts.get(key, 0) + 1
            self.counts[key] = used
            self.save()
            return used, max(0, self.limit - used)

    def reset_user(self, user_id: int) -> Tuple[int, int]:
        with self._lock:
            self._rollover_if_needed()
            key = str(user_id)
            previous = self.counts.pop(key, 0)
            self.save()
            return previous, self.limit

    def reset_all(self) -> int:
        with self._lock:
            self._rollover_if_needed()
            previous = len(self.counts)
            self.counts = {}
            self.save()
            return previous

    def reset_at_utc(self) -> datetime:
        now = datetime.now(timezone.utc)
        tomorrow = now.date() + timedelta(days=1)
        return datetime.combine(tomorrow, time.min, tzinfo=timezone.utc)

    def save(self):
        with self._lock:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                payload = {"date": self.usage_date, "counts": self.counts}
                tmp_path = self.path.with_suffix(self.path.suffix + ".tmp")
                tmp_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
                tmp_path.replace(self.path)
            except Exception as exc:
                log.error("Failed to save gen usage state: %s", exc)

    def _rollover_if_needed(self):
        with self._lock:
            today = self._today_key()
            if today != self.usage_date:
                self.usage_date = today
                self.counts = {}
                self.save()

    @staticmethod
    def _today_key() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")
