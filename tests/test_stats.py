from __future__ import annotations

import json
import time

from aziz.stats import DayStats


class Clock:
    def __init__(self):
        self.now = time.mktime((2026, 11, 5, 12, 0, 0, 0, 0, -1))

    def __call__(self):
        return self.now


def test_counts_and_time_add_up(tmp_path):
    stats = DayStats(tmp_path / "stats.json", Clock())
    stats.add_up(90)
    stats.add_up(30.4)
    stats.count("loads")
    stats.count("jobsDone")
    stats.count("jobsDone")
    assert stats.snapshot() == {"upSeconds": 120, "loads": 1, "jobsDone": 2, "jobsFailed": 0}


def test_a_new_day_starts_from_zero(tmp_path):
    clock = Clock()
    stats = DayStats(tmp_path / "stats.json", clock)
    stats.add_up(500)
    stats.count("jobsFailed")
    clock.now += 24 * 3600
    assert stats.snapshot() == {"upSeconds": 0, "loads": 0, "jobsDone": 0, "jobsFailed": 0}


def test_a_restart_keeps_today_but_not_yesterday(tmp_path):
    clock = Clock()
    path = tmp_path / "stats.json"
    first = DayStats(path, clock)
    first.count("loads")
    assert DayStats(path, clock).snapshot()["loads"] == 1
    clock.now += 24 * 3600
    assert DayStats(path, clock).snapshot()["loads"] == 0


def test_time_is_saved_only_now_and_then(tmp_path):
    clock = Clock()
    path = tmp_path / "stats.json"
    stats = DayStats(path, clock)
    stats.add_up(2)
    first = json.loads(path.read_text())["upSeconds"]
    clock.now += 2
    stats.add_up(2)
    assert json.loads(path.read_text())["upSeconds"] == first
    clock.now += 61
    stats.add_up(2)
    assert json.loads(path.read_text())["upSeconds"] == 6


def test_a_damaged_or_unwritable_file_does_no_harm(tmp_path):
    clock = Clock()
    path = tmp_path / "stats.json"
    path.write_text("{not json")
    stats = DayStats(path, clock)
    stats.count("loads")
    assert stats.snapshot()["loads"] == 1
    path.write_text(json.dumps({"day": stats.day, "upSeconds": "many"}))
    assert DayStats(path, clock).snapshot()["upSeconds"] == 0
    blocked = tmp_path / "file"
    blocked.write_text("")
    trapped = DayStats(blocked / "stats.json", clock)
    trapped.count("loads")
    assert trapped.snapshot()["loads"] == 1
