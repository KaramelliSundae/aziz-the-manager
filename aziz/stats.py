"""What Kamil did today, for the boss's chart. Counted as it happens and saved once a minute, so a restart loses little."""
from __future__ import annotations

import json
import logging
import os
import time
from collections.abc import Callable
from pathlib import Path

log = logging.getLogger(__name__)

SAVE_SECONDS = 60.0
COUNTERS = ("loads", "jobsDone", "jobsFailed")


class DayStats:
    """Today's numbers. A problem with the file is only logged: the chart must never get in the way of the GPU."""

    def __init__(self, path: Path, clock: Callable[[], float] = time.time) -> None:
        self.path = path
        self.clock = clock
        self.day = self._today()
        self.up_seconds = 0.0
        self.counts = dict.fromkeys(COUNTERS, 0)
        self.saved_at = 0.0
        self._load()

    def _today(self) -> str:
        return time.strftime("%Y-%m-%d", time.localtime(self.clock()))

    def _load(self) -> None:
        try:
            saved = json.loads(self.path.read_text())
            if saved["day"] != self.day:
                return
            self.up_seconds = max(0.0, float(saved["upSeconds"]))
            self.counts = {name: max(0, int(saved.get(name, 0))) for name in COUNTERS}
        except FileNotFoundError:
            pass
        except (OSError, ValueError, TypeError, KeyError, AttributeError) as error:
            log.warning("could not read today's numbers: %s", error)

    def _roll(self) -> None:
        today = self._today()
        if today != self.day:
            self.day = today
            self.up_seconds = 0.0
            self.counts = dict.fromkeys(COUNTERS, 0)

    def _save(self) -> None:
        now = self.clock()
        if now - self.saved_at < SAVE_SECONDS:
            return
        self.saved_at = now
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(".tmp")
            temporary.write_text(json.dumps({"day": self.day, "upSeconds": self.up_seconds, **self.counts}))
            os.replace(temporary, self.path)
        except OSError as error:
            log.warning("could not save today's numbers: %s", error)

    def add_up(self, seconds: float) -> None:
        self._roll()
        self.up_seconds += max(0.0, seconds)
        self._save()

    def count(self, name: str) -> None:
        self._roll()
        self.counts[name] += 1
        self._save()

    def snapshot(self) -> dict:
        self._roll()
        return {"upSeconds": round(self.up_seconds), **self.counts}
