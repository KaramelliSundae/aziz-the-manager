"""Real threads over real HTTP: requests from every side at once, with the watch loop running."""

from __future__ import annotations

import random
import threading
import time

import httpx

from aziz.manager import Aziz
from aziz.server import make_server

from .test_manager import FakeHost


def test_requests_from_every_side_at_once(tmp_path):
    host = FakeHost()
    aziz = Aziz(host, tmp_path / "gpu-mode")
    server = make_server(aziz, port=0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    stop = threading.Event()
    failures: list[str] = []

    def watch():
        while not stop.is_set():
            try:
                aziz.tick()
                for unit in list(host.job_states):
                    if random.random() < 0.3:
                        host.job_states[unit] = "inactive"
            except Exception as error:  # noqa: BLE001 - reported below
                failures.append(f"tick: {error!r}")
            time.sleep(0.005)

    def caller(seed):
        rng = random.Random(seed)
        with httpx.Client(timeout=30) as client:
            while not stop.is_set():
                number = rng.randrange(6)
                choice = rng.randrange(8)
                try:
                    if choice == 0:
                        answer = client.post(f"{base}/jobs", json={"id": f"j{number}", "gpu": rng.random() < 0.6, "nextGpu": "likely"})
                    elif choice == 1:
                        answer = client.post(f"{base}/jobs/j{number}/cancel")
                    elif choice == 2:
                        answer = client.post(f"{base}/priority", json={"on": rng.random() < 0.5})
                    elif choice == 3:
                        answer = client.post(f"{base}/mode", json={"mode": rng.choice(("assistant", "assistant", "compute"))})
                    elif choice == 4:
                        answer = client.post(f"{base}/forecast", json={"nextGpu": "unlikely"})
                    elif choice == 5:
                        answer = client.get(f"{base}/jobs/j{number}")
                    else:
                        answer = client.get(f"{base}/state")
                    if answer.status_code >= 500:
                        failures.append(f"{answer.request.url}: {answer.text}")
                except httpx.HTTPError as error:
                    failures.append(repr(error))

    threads = [threading.Thread(target=watch)] + [threading.Thread(target=caller, args=(n,)) for n in range(8)]
    for thread in threads:
        thread.start()
    time.sleep(3)
    stop.set()
    for thread in threads:
        thread.join(timeout=35)
    server.shutdown()
    assert not failures, failures[:5]
    names = [job.id for job in aziz.jobs if job.status in ("queued", "running")]
    assert len(names) == len(set(names))
