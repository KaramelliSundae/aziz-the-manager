"""One Aziz living a very long time, as the service does. Not a test by default: run it on the server with deploy/capped.sh.

Prints memory as it goes, so a leak shows as a line that keeps rising.
"""
from __future__ import annotations

import logging
import random
import resource
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from aziz import manager  # noqa: E402
from aziz.manager import Aziz  # noqa: E402
from tests.test_simulation import Clock, Machine  # noqa: E402

ROUNDS = int(sys.argv[1]) if len(sys.argv) > 1 else 300_000
logging.disable(logging.CRITICAL)


def rss_mib() -> float:
    for line in Path("/proc/self/status").read_text().splitlines():
        if line.startswith("VmRSS:"):
            return int(line.split()[1]) / 1024
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024


rng = random.Random(7)
clock = Clock()
machine = Machine(rng, clock)
manager.time.sleep = machine.advance
with tempfile.TemporaryDirectory() as folder:
    aziz = Aziz(machine, Path(folder) / "gpu-mode", clock)
    readings = []
    started = time.time()
    for number in range(ROUNDS):
        machine.faults = rng.random() < 0.05
        try:  # the service's request handler and watch loop catch these and go on
            if rng.random() < 0.02:
                try:
                    aziz.submit(f"j{number % 40}", f"iş {number}", rng.random() < 0.5, rng.random() < 0.3)
                except ValueError:
                    pass
            if rng.random() < 0.002:
                aziz.preempt()
                machine.image_app_busy = True
            if rng.random() < 0.002:
                aziz.set_priority(rng.random() < 0.5)
            if rng.random() < 0.01:
                aziz.acquire()
            if rng.random() < 0.01:
                aziz.state()
        except subprocess.SubprocessError:
            pass
        if rng.random() < 0.01:
            machine.image_app_busy = False
        machine.advance(rng.choice((2, 2, 2, 30)))
        try:
            aziz.tick()
        except subprocess.SubprocessError:
            pass
        if number % (ROUNDS // 6) == 0:
            readings.append(rss_mib())
            print(f"round {number:>8}: {readings[-1]:6.1f} MiB, {len(aziz.jobs)} jobs kept, {len(machine.units)} units, journal {aziz.journal.path.stat().st_size // 1024} KiB", flush=True)
    print(f"{ROUNDS} rounds = {clock.now / 86400:.0f} simulated days, in {time.time() - started:.0f} s")
    print(f"memory from first to last reading: {readings[0]:.1f} -> {rss_mib():.1f} MiB")
