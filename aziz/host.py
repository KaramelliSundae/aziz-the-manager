"""Everything Aziz touches on the machine: systemd, the GPU, the model and the image app. Standard library only, so the
service needs no virtual environment and survives upgrades of the projects it manages."""

from __future__ import annotations

import http.client
import json
import os
import subprocess
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

LLM_URL = "http://127.0.0.1:8422"
# The program that always comes first on the GPU, and how to reach it. Set in /etc/aziz/env.
IMAGE_APP_HEALTH = os.environ.get("AZIZ_IMAGE_APP_HEALTH", "http://127.0.0.1:8000/v1/health")
LLM_UNIT = "ai-ders-llm.service"
IMAGE_APP_UNIT = os.environ.get("AZIZ_IMAGE_APP_UNIT", "image-app.service")
# What to call it in what the phone shows. Empty leaves the wording to the app.
IMAGE_APP_NAME = os.environ.get("AZIZ_IMAGE_APP_NAME", "")
FREE_SECONDS = 15.0  # the image app waits 20 s for a preempt

# Calls to these local services must never go through a proxy.
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
NET_ERRORS = (OSError, http.client.HTTPException, ValueError)


@dataclass(frozen=True)
class ImageApp:
    reachable: bool
    hooked: bool = False
    busy: bool = False
    running: bool = False  # its service is up, whatever its health check says


class Host(Protocol):
    def image_app(self) -> ImageApp: ...
    def foreign_gpu_mib(self) -> int: ...
    def llm_vram_mib(self) -> int: ...
    def vram_used_mib(self) -> int: ...
    def available_ram_gib(self) -> float: ...
    def llm_active(self) -> bool: ...
    def llm_ready(self) -> bool: ...
    def start_llm(self) -> None: ...
    def stop_llm(self) -> None: ...
    def llm_busy(self) -> bool: ...
    def start_job(self, unit: str) -> None: ...
    def stop_job(self, unit: str) -> None: ...
    def job_state(self, unit: str) -> str: ...
    def job_known(self, unit: str) -> bool: ...
    def system(self) -> dict: ...
    def boot_id(self) -> str: ...


def _get(url: str, timeout: float) -> tuple[int, bytes]:
    with _opener.open(url, timeout=timeout) as response:
        return response.status, response.read()


def _nvidia_smi(query: str) -> str:
    command = ["nvidia-smi", query, "--format=csv,noheader,nounits"]
    return subprocess.run(command, capture_output=True, text=True, timeout=10, check=True).stdout


def _gpu_processes() -> list[tuple[int, int]]:
    rows = []
    for line in _nvidia_smi("--query-compute-apps=pid,used_memory").splitlines():
        pid, _, memory = line.partition(",")
        if pid.strip().isdigit() and memory.strip().isdigit():
            rows.append((int(pid), int(memory)))
    return rows


def _unit_of(pid: int) -> str:
    try:
        return Path(f"/proc/{pid}/cgroup").read_text()
    except OSError:
        return ""  # it just exited


class SystemHost:
    def image_app(self) -> ImageApp:
        try:
            body = json.loads(_get(IMAGE_APP_HEALTH, 2.0)[1])
        except NET_ERRORS:
            # A slow health check during a job must not read as "the image app is off".
            return ImageApp(reachable=False, running=self._image_app_service_up())
        queue = body.get("queue") or {}
        busy = bool(body.get("loading") or queue.get("running") or queue.get("waiting"))
        return ImageApp(reachable=True, hooked=body.get("gpuGate") is True, busy=busy, running=True)

    def _image_app_service_up(self) -> bool:
        try:
            return subprocess.run(["systemctl", "is-active", "--quiet", IMAGE_APP_UNIT], timeout=10).returncode == 0
        except (OSError, subprocess.SubprocessError):
            return True  # not knowing counts as up: a job waits, the image app is never at risk

    def foreign_gpu_mib(self) -> int:
        """Memory of GPU processes that are neither the model nor the image app, whose work the queue shows."""
        total = 0
        for pid, memory in _gpu_processes():
            unit = _unit_of(pid)
            if LLM_UNIT not in unit and IMAGE_APP_UNIT not in unit:
                total += memory
        return total

    def llm_vram_mib(self) -> int:
        return sum(memory for pid, memory in _gpu_processes() if LLM_UNIT in _unit_of(pid))

    def vram_used_mib(self) -> int:
        return int(_nvidia_smi("--query-gpu=memory.used").splitlines()[0])

    def available_ram_gib(self) -> float:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) / 2**20
        return 0.0

    def llm_active(self) -> bool:
        return subprocess.run(["systemctl", "is-active", "--quiet", LLM_UNIT], timeout=10).returncode == 0

    def llm_ready(self) -> bool:
        try:
            return _get(f"{LLM_URL}/health", 2.0)[0] == 200
        except NET_ERRORS:
            return False

    def start_llm(self) -> None:
        subprocess.run(["systemctl", "start", "--no-block", LLM_UNIT], check=True, timeout=10)

    def stop_llm(self) -> None:
        subprocess.run(["systemctl", "stop", LLM_UNIT], check=True, timeout=FREE_SECONDS)

    def llm_busy(self) -> bool:
        try:
            slots = json.loads(_get(f"{LLM_URL}/slots", 2.0)[1])
            return any(slot.get("is_processing") for slot in slots)
        except (*NET_ERRORS, AttributeError, TypeError):
            return False

    def start_job(self, unit: str) -> None:
        subprocess.run(["systemctl", "start", "--no-block", unit], check=True, capture_output=True, timeout=10)

    def stop_job(self, unit: str) -> None:
        subprocess.run(["systemctl", "stop", "--no-block", unit], check=True, capture_output=True, timeout=10)

    def job_state(self, unit: str) -> str:
        return subprocess.run(["systemctl", "is-active", unit], capture_output=True, text=True, timeout=10).stdout.strip()

    def job_known(self, unit: str) -> bool:
        return subprocess.run(["systemctl", "cat", "--quiet", unit], capture_output=True, timeout=10).returncode == 0

    def system(self) -> dict:
        info: dict = {"load": round(os.getloadavg()[0], 1), "cores": os.cpu_count()}
        memory = {}
        for line in Path("/proc/meminfo").read_text().splitlines():
            key, _, value = line.partition(":")
            if value.split():
                memory[key] = int(value.split()[0]) / 2**20
        info["ramTotalGib"] = round(memory["MemTotal"], 1)
        info["ramAvailableGib"] = round(memory["MemAvailable"], 1)
        try:  # the memory numbers still count when the GPU driver is having a bad day
            total, temperature = _nvidia_smi("--query-gpu=memory.total,temperature.gpu").splitlines()[0].split(",")
            info["vramTotalMib"] = int(total)
            info["vramUsedMib"] = self.vram_used_mib()
            info["gpuTempC"] = int(temperature)
        except (OSError, subprocess.SubprocessError, ValueError, IndexError):
            pass
        return info

    def boot_id(self) -> str:
        return Path("/proc/sys/kernel/random/boot_id").read_text().strip()
