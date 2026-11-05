"""Aziz's logbook: every decision and why, in plain words, kept across restarts.

Two rules from experience. It has a size limit, because a log that grows without end once filled a server's memory.
And it can never hurt the manager: a full disk costs an entry, never a preempt.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections import deque
from pathlib import Path

log = logging.getLogger(__name__)

MAX_BYTES = 512 * 1024  # one full file and one older file, a megabyte at most, which is months of decisions
KEPT_IN_MEMORY = 200
REPEAT_SECONDS = 300.0  # the same line again this soon only counts up: a failure every 2 s must not bury the rest


class Journal:
    def __init__(self, path: Path, now=time.time, max_bytes: int = MAX_BYTES):
        self.path = path
        self.now = now
        self.max_bytes = max_bytes
        self._lock = threading.Lock()
        self.entries: deque[dict] = deque(self._read_tail(), maxlen=KEPT_IN_MEMORY)

    def _read_tail(self) -> list[dict]:
        entries = []
        for source in (self.path.with_suffix(".1"), self.path):
            try:
                lines = source.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                continue
            for line in lines[-KEPT_IN_MEMORY:]:
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue  # a line cut short by a crash
                if isinstance(entry, dict) and isinstance(entry.get("text"), str):
                    entries.append(entry)
        return entries[-KEPT_IN_MEMORY:]

    def add(self, kind: str, text: str) -> None:
        """Never raises."""
        try:
            with self._lock:
                when = self.now()
                last = self.entries[-1] if self.entries else None
                if last and last.get("text") == text and when - last.get("at", 0) < REPEAT_SECONDS:
                    last["count"] = last.get("count", 1) + 1
                    return
                entry = {"at": round(when, 1), "kind": kind, "text": text}
                self.entries.append(entry)
                self._write(entry)
            log.info("%s", text)
        except Exception as error:  # noqa: BLE001 - see the module's second rule
            log.warning("logbook: %s", error)

    def _write(self, entry: dict) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            if self.path.exists() and self.path.stat().st_size >= self.max_bytes:
                self.path.replace(self.path.with_suffix(".1"))
            with self.path.open("a", encoding="utf-8") as file:
                file.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError as error:
            log.warning("could not write the logbook: %s", error)

    def recent(self, limit: int) -> list[dict]:
        with self._lock:
            return [dict(entry) for entry in list(self.entries)[-max(0, limit):]] if limit > 0 else []
