"""Random days in the machine's life, with faults, checked against what must always hold.

After every round, on a machine without active faults:
  - at most one GPU job runs, and never beside the model
  - while the image app works, neither a GPU job nor the model holds the GPU
Once the chaos ends, however it went:
  - every job reaches an end, nothing keeps waiting for ever, and the model is back
"""

from __future__ import annotations

import logging
import os
import random
import subprocess

import pytest

from aziz import manager
from aziz.host import ImageApp
from aziz.manager import KEEP_OFF_SECONDS, PRIORITY_SECONDS, Aziz

GPU_PREFIX = "offload-gpu-job@"
FAULT_MEMORY = 4  # rounds after a fault before the rules must hold again


class Machine:
    def __init__(self, rng, clock):
        self.rng = rng
        self.clock = clock
        self.units: dict[str, list] = {}  # unit -> [state, seconds left]
        self.model = False
        self.model_since = 0.0
        self.model_vram_lingers = 0
        self.image_app_busy = False
        self.image_app_up = True
        self.image_app_hangs = False
        self.ram = 17.0
        self.faults = False
        self.boot = "boot-0"
        self.foreign_program = 0
        self.fault_rounds_ago = 99

    def _fault(self):
        if self.faults and self.rng.random() < 0.3:
            self.fault_rounds_ago = 0
            # A new one each time: re-raising one object grows its traceback for ever.
            raise subprocess.CalledProcessError(1, "systemctl")

    def image_app(self):
        if self.image_app_hangs:  # up and working, but its health check times out
            return ImageApp(reachable=False, running=True)
        if not self.image_app_up:
            return ImageApp(reachable=False)
        return ImageApp(reachable=True, hooked=True, busy=self.image_app_busy, running=True)

    def _gpu_jobs(self):
        return [u for u, (state, _) in self.units.items() if u.startswith(GPU_PREFIX) and state in ("active", "deactivating")]

    def foreign_gpu_mib(self):
        self._fault()
        return 2000 * len(self._gpu_jobs()) + self.foreign_program

    def llm_vram_mib(self):
        self._fault()
        return 6000 if self.model or self.model_vram_lingers > 0 else 0

    def vram_used_mib(self):
        return 900 + self.llm_vram_mib() + self.foreign_gpu_mib() + (3000 if self.image_app_busy else 0)

    def available_ram_gib(self):
        return self.ram

    def llm_active(self):
        return self.model

    def llm_ready(self):
        return self.model and self.clock.now - self.model_since >= 20

    def start_llm(self):
        self._fault()
        if not self.model:
            self.model, self.model_since = True, self.clock.now

    def stop_llm(self):
        self._fault()
        self.model = False
        self.model_vram_lingers = self.rng.choice((0, 0, 1, 3))

    def llm_busy(self):
        return self.model and self.rng.random() < 0.2

    def start_job(self, unit):
        self._fault()
        self.units[unit] = ["active", self.rng.choice((1, 30, 300, 900))]

    def stop_job(self, unit):
        self._fault()
        if unit in self.units and self.units[unit][0] == "active":
            self.units[unit] = ["deactivating", self.rng.choice((0, 2, 5))]

    def job_state(self, unit):
        if self.faults and self.rng.random() < 0.1:
            self.fault_rounds_ago = 0
            return self.rng.choice(("", "unknown"))
        return self.units.get(unit, ["inactive"])[0]

    def job_known(self, unit):
        return True

    def system(self):
        self._fault()
        return {"ramAvailableGib": self.ram}

    def boot_id(self):
        return self.boot

    def advance(self, seconds):
        self.clock.now += seconds
        if self.model_vram_lingers:
            self.model_vram_lingers -= 1
        for unit, entry in self.units.items():
            if entry[0] in ("active", "deactivating"):
                entry[1] -= seconds
                if entry[1] <= 0:
                    was_stopping = entry[0] == "deactivating"
                    entry[0] = "inactive" if was_stopping or self.rng.random() < 0.8 else "failed"

    def reboot(self):
        self.units.clear()
        self.model = False
        self.model_vram_lingers = 0
        self.boot = f"boot-{self.rng.random()}"


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def check_rules(machine, aziz, seed, step):
    assert machine.units is not None
    if machine.fault_rounds_ago < FAULT_MEMORY:
        return
    where = f"seed {seed}, step {step}"
    gpu_jobs = [u for u, (state, _) in machine.units.items() if u.startswith(GPU_PREFIX) and state == "active"]
    assert len(gpu_jobs) <= 1, f"two GPU jobs, {where}"
    if gpu_jobs:
        assert not machine.model, f"model beside a GPU job, {where}"
    if machine.image_app_busy:
        assert not gpu_jobs, f"GPU job beside the image app, {where}"
        assert not machine.model, f"model beside the image app, {where}"
    if aziz.mode == "compute":
        assert not machine.model, f"model in compute mode, {where}"
    names = [job.id for job in aziz.jobs if job.status in ("queued", "running")]
    assert len(names) == len(set(names)), f"a job listed twice, {where}"


def live(seed, tmp_path, monkeypatch, steps):
    rng = random.Random(seed)
    clock = Clock()
    machine = Machine(rng, clock)
    monkeypatch.setattr(manager.time, "sleep", lambda seconds: machine.advance(seconds))
    aziz = Aziz(machine, tmp_path / "gpu-mode", clock)
    number = 0

    def submit(gpu):
        nonlocal number
        number += 1
        try:
            aziz.submit(f"j{number % 12}", "iş", gpu, rng.random() < 0.4, rng.random() < 0.8)
        except ValueError:
            pass

    def cancel():
        jobs = [job.id for job in aziz.jobs]
        if jobs:
            try:
                aziz.cancel(rng.choice(jobs))
            except subprocess.SubprocessError:
                pass

    def priority(on):
        try:
            aziz.set_priority(on)
        except (ValueError, subprocess.SubprocessError):
            pass

    def preempt():
        if machine.image_app_up:
            aziz.preempt()
            machine.image_app_busy = True

    def image_app_goes(up):
        machine.image_app_up = up
        if not up:  # stopped, so it holds nothing
            machine.image_app_busy = machine.image_app_hangs = False

    def image_app_hang():
        machine.image_app_hangs = machine.image_app_busy and rng.random() < 0.7

    def question():
        try:
            aziz.acquire()
        except subprocess.SubprocessError:
            pass

    def restart():
        nonlocal aziz
        aziz = Aziz(machine, tmp_path / "gpu-mode", clock)

    def reboot():
        nonlocal aziz
        machine.reboot()
        machine.image_app_busy = False
        aziz = Aziz(machine, tmp_path / "gpu-mode", clock)

    def mode(which):
        try:
            aziz.set_mode(which)
        except subprocess.SubprocessError:
            pass

    actions = [
        (lambda: submit(True), 6), (lambda: submit(False), 3), (cancel, 2), (lambda: priority(True), 1),
        (lambda: priority(False), 1), (preempt, 2),
        (lambda: (setattr(machine, "image_app_busy", False), setattr(machine, "image_app_hangs", False)), 4), (image_app_hang, 1),
        (question, 4), (lambda: mode("compute"), 1), (lambda: mode("assistant"), 3), (restart, 1), (reboot, 0.3),
        (lambda: setattr(machine, "ram", rng.choice((1.5, 2.5, 3.5, 6.0, 17.0, 17.0, 17.0))), 2),
        (lambda: setattr(machine, "faults", rng.random() < 0.3), 1),
        (lambda: image_app_goes(rng.random() < 0.8), 1),
        (lambda: setattr(machine, "foreign_program", rng.choice((0, 0, 0, 3000))), 1),
        (lambda: None, 10),
    ]
    moves, weights = zip(*actions)
    for step in range(steps):
        rng.choices(moves, weights)[0]()
        machine.advance(rng.choice((2, 2, 2, 30, 120)))
        machine.fault_rounds_ago += 1
        try:
            aziz.tick()
        except subprocess.SubprocessError:
            machine.fault_rounds_ago = 0  # the service loop logs this and goes on
        check_rules(machine, aziz, seed, step)

    # The chaos ends. However it went, everything must settle.
    machine.faults = False
    machine.image_app_busy = False
    machine.image_app_up = True
    machine.image_app_hangs = False
    machine.ram = 17.0
    machine.foreign_program = 0
    aziz.set_mode("assistant")
    aziz.set_priority(False)
    machine.fault_rounds_ago = 99
    for _ in range(2000):
        machine.advance(5)
        aziz.tick()
        check_rules(machine, aziz, seed, "settling")
        if machine.model and not aziz._with_status("queued", "running") and aziz.claim is None:
            break
    left = [(job.id, job.status, job.note) for job in aziz._with_status("queued", "running")]
    assert not left, f"jobs stuck, seed {seed}: {left}"
    assert aziz.claim is None, f"claim stuck, seed {seed}"
    assert machine.model, f"the model never came back, seed {seed}: {aziz.state()['blocked']}"
    return aziz


@pytest.fixture(autouse=True)
def quiet():
    """Thousands of logged faults, kept by pytest for its report, once filled a server's memory."""
    logging.disable(logging.CRITICAL)
    yield
    logging.disable(logging.NOTSET)


# Light by default. The server runs thousands: AZIZ_SIM_SEEDS=2000 deploy/capped.sh pytest tests/test_simulation.py
FIRST_SEED = int(os.environ.get("AZIZ_SIM_START", "0"))


@pytest.mark.parametrize("seed", range(FIRST_SEED, FIRST_SEED + int(os.environ.get("AZIZ_SIM_SEEDS", "20"))))
def test_a_random_life(seed, tmp_path, monkeypatch):
    live(seed, tmp_path, monkeypatch, steps=250)


def test_settling_covers_the_longest_waits():
    assert 2000 * 5 > KEEP_OFF_SECONDS and 2000 * 5 > PRIORITY_SECONDS
