"""Compute holds two kinds of work. A job ends by itself; a server stays up until someone says stop, with the model on disk."""

from __future__ import annotations

import httpx
import pytest

from aziz.host import ImageApp
from aziz.manager import HOLD_SECONDS, JOB_GUARD_RAM_GIB, JOB_START_SECONDS, KEEP_OFF_SECONDS, Aziz, NeedsConfirm
from aziz.server import make_server

from .test_manager import Clock, FakeHost


IDLE = ImageApp(reachable=True, hooked=True, running=True)
WORKING = ImageApp(reachable=True, hooked=True, busy=True, running=True)
OFF = ImageApp(reachable=False)
WEEK = 7 * 24 * 3600
GPU_SERVER = "offload-gpu-server@api.service"
CPU_SERVER = "offload-server@api.service"
GPU_JOB = "offload-gpu-job@render.service"


@pytest.fixture
def host():
    host = FakeHost()
    host.image_app_is_known = True
    return host


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def aziz(host, clock, tmp_path):
    return Aziz(host, tmp_path / "gpu-mode", clock)


def settled(aziz, clock, rounds=2):
    for _ in range(rounds):
        clock.now += JOB_START_SECONDS + 1
        aziz.tick()


# The image app


def test_it_is_off_until_asked_and_the_model_runs(aziz, host):
    aziz.tick()
    assert host.image_app_starts == 0 and host.active is True
    state = aziz.state()
    assert state["mode"] == "assistant" and state["server"]["up"] is False and state["gpuOwner"] == "kamil"


def test_starting_it_puts_the_model_on_disk_first(aziz, host):
    aziz.tick()
    aziz.set_server(True)
    assert host.active is False and host.image_app_starts == 1
    state = aziz.state()
    assert state["mode"] == "compute" and aziz.mode == "assistant" and state["server"]["up"] is True
    assert state["gpuOwner"] == "image" and "sunucusu açık" in state["blocked"]


def test_nothing_stops_it_but_a_stop(aziz, host, clock):
    aziz.set_server(True)
    clock.now += WEEK
    host.ram = 1.0
    aziz.tick()
    aziz.acquire()
    aziz.submit("render", "işle", True, False)
    aziz.tick()
    assert host.image_app_stops == 0 and host.active is False


def test_a_question_is_told_why_and_does_not_end_it(aziz, host):
    aziz.set_server(True)
    assert "sunucusu açık" in aziz.acquire()
    assert host.active is False and host.image_app_stops == 0


def test_priority_is_refused_while_it_is_up(aziz):
    aziz.set_server(True)
    with pytest.raises(ValueError, match="kapat"):
        aziz.set_priority(True)


def test_stopping_it_brings_the_model_back(aziz, host):
    aziz.set_server(True)
    aziz.tick()
    aziz.set_server(False)
    aziz.tick()
    assert host.image_app_stops == 1 and host.active is True
    assert aziz.state()["mode"] == "assistant"


def test_stopping_it_mid_work_needs_a_firm_word(aziz, host):
    host.image_app_state = WORKING
    with pytest.raises(ValueError, match="onayla"):
        aziz.set_server(False)
    assert host.image_app_stops == 0
    aziz.set_server(False, force=True)
    assert host.image_app_stops == 1


def test_asking_twice_does_nothing_more(aziz, host):
    aziz.set_server(False)
    assert host.image_app_stops == 0
    aziz.set_server(True)
    aziz.set_server(True)
    assert host.image_app_starts == 1


def test_a_machine_without_one_refuses(aziz, host):
    host.image_app_is_known = False
    with pytest.raises(ValueError, match="tanımlı değil"):
        aziz.set_server(True)
    assert host.image_app_starts == 0 and aziz.state()["server"]["known"] is False


def test_started_by_hand_it_counts_all_the_same(aziz, host):
    aziz.tick()
    assert host.active is True
    host.image_app_state = IDLE
    aziz.tick()
    assert host.active is False
    assert any("açıldı" in entry["text"] for entry in aziz.journal.recent(5))


def test_if_it_dies_the_model_comes_back_and_the_log_says_so(aziz, host, clock):
    aziz.set_server(True)
    aziz.tick()
    host.image_app_state = OFF
    clock.now += HOLD_SECONDS + 1
    aziz.tick()
    assert host.active is True
    assert any("kapandı" in entry["text"] for entry in aziz.journal.recent(5))


def test_starting_it_moves_a_gpu_job_back_in_the_queue(aziz, host, clock):
    aziz.submit("render", "işle", True, False)
    aziz.tick()
    assert host.started_jobs == [GPU_JOB]
    host.job_states[GPU_JOB] = "active"
    clock.now += JOB_START_SECONDS + 1
    aziz.set_server(True)
    assert host.stopped_jobs == [GPU_JOB] and aziz.job("render").status == "queued"


def test_a_gpu_job_runs_beside_it_while_it_is_idle(aziz, host):
    host.image_app_state = IDLE
    aziz.submit("render", "işle", True, False)
    aziz.tick()
    assert host.started_jobs == [GPU_JOB]


def test_choosing_compute_opens_it_and_saves_the_choice(aziz, host, tmp_path):
    aziz.tick()
    aziz.set_mode("compute")
    assert host.image_app_starts == 1 and host.active is False and (tmp_path / "gpu-mode").read_text() == "compute"


def test_choosing_kamil_with_it_up_needs_a_second_word(aziz, host):
    aziz.set_mode("compute")
    with pytest.raises(NeedsConfirm, match="onayla"):
        aziz.set_mode("assistant")
    assert host.image_app_stops == 0 and aziz.mode == "compute"
    aziz.set_mode("assistant", confirm=True)
    aziz.tick()
    assert host.image_app_stops == 1 and aziz.mode == "assistant" and host.active is True


def test_choosing_kamil_with_nothing_up_needs_no_second_word(aziz, host):
    aziz.mode = "compute"
    aziz.set_mode("assistant")
    assert aziz.mode == "assistant" and host.image_app_stops == 0


def test_the_second_word_also_stops_it_mid_work_and_a_gpu_server(aziz, host, clock):
    running_server(aziz, host, clock)
    host.image_app_state = WORKING
    with pytest.raises(NeedsConfirm, match="api"):
        aziz.set_mode("assistant")
    assert host.stopped_jobs == []
    aziz.set_mode("assistant", confirm=True)
    assert host.image_app_stops == 1 and host.stopped_jobs == [GPU_SERVER] and aziz.job("api").status == "cancelled"


def test_a_cpu_server_stays_when_choosing_kamil(aziz, host, clock):
    aziz.submit("api", "api", False, False, server=True)
    aziz.tick()
    host.job_states[CPU_SERVER] = "active"
    aziz.set_mode("assistant")
    assert host.stopped_jobs == [] and aziz.job("api").status == "running"


def test_a_machine_with_no_server_keeps_plain_compute(aziz, host):
    host.image_app_is_known = False
    aziz.set_mode("compute")
    assert host.image_app_starts == 0 and aziz.mode == "compute"


def test_if_it_will_not_open_compute_still_stands_and_says_so(aziz, host):
    def fails():
        raise OSError("no")

    host.start_image_app = fails
    aziz.set_mode("compute")
    assert aziz.mode == "compute" and any("açılamadı" in entry["text"] for entry in aziz.journal.recent(5))


def test_compute_reopens_it_after_a_reboot_but_not_after_a_restart(host, clock, tmp_path):
    aziz = Aziz(host, tmp_path / "gpu-mode", clock)
    aziz.set_mode("compute")
    host.image_app_state = OFF
    again = Aziz(host, tmp_path / "gpu-mode", clock)  # the service restarted, and someone closed it by hand
    again.tick()
    assert host.image_app_starts == 1
    host.boot_id = lambda: "another"
    rebooted = Aziz(host, tmp_path / "gpu-mode", clock)
    rebooted.tick()
    assert host.image_app_starts == 2


def test_after_a_reboot_in_ai_asistan_it_stays_on_disk(host, clock, tmp_path):
    Aziz(host, tmp_path / "gpu-mode", clock)
    host.boot_id = lambda: "another"
    Aziz(host, tmp_path / "gpu-mode", clock).tick()
    assert host.image_app_starts == 0


def test_the_wait_for_more_gpu_work_ends_with_a_stop(aziz, host, clock):
    aziz.submit("render", "işle", True, True)
    aziz.tick()
    host.job_states[GPU_JOB] = "active"
    settled(aziz, clock, 1)
    host.job_states[GPU_JOB] = "inactive"
    settled(aziz, clock, 1)
    assert aziz.keep_off_until > clock.now
    aziz.set_server(True)
    aziz.set_server(False)
    aziz.tick()
    assert host.active is True


# Servers sent as offload work


def test_a_server_has_its_own_unit_and_no_time_limit(aziz, host, clock):
    job = aziz.submit("api", "api", True, True, False, 5, server=True)
    assert job.unit == GPU_SERVER and job.max_seconds == 0 and job.restart is True and job.next_gpu_likely is False
    aziz.tick()
    host.job_states[GPU_SERVER] = "active"
    clock.now += WEEK
    aziz.tick()
    assert host.stopped_jobs == [] and aziz.job("api").status == "running"
    assert aziz.submit("cpu", "api", False, False, server=True).unit == "offload-server@cpu.service"


def running_server(aziz, host, clock):
    aziz.submit("api", "api", True, False, server=True)
    aziz.tick()
    host.job_states[GPU_SERVER] = "active"
    settled(aziz, clock, 1)
    assert aziz.job("api").status == "running"


def test_only_a_cancel_stops_a_server(aziz, host, clock):
    running_server(aziz, host, clock)
    aziz.preempt()
    host.image_app_state = WORKING
    aziz.tick()
    host.image_app_state = OFF
    host.ram = JOB_GUARD_RAM_GIB - 1
    aziz.tick()
    assert host.stopped_jobs == [] and aziz.job("api").status == "running"
    assert aziz.cancel("api") is True
    assert host.stopped_jobs == [GPU_SERVER] and aziz.job("api").status == "cancelled"


def test_a_gpu_server_keeps_the_model_on_disk_and_it_returns_right_after(aziz, host, clock):
    aziz.tick()
    assert host.active is True
    running_server(aziz, host, clock)
    clock.now += WEEK
    aziz.tick()
    assert host.active is False and "hesaplama" in aziz.acquire()
    aziz.cancel("api")
    host.job_states[GPU_SERVER] = "inactive"
    aziz.tick()
    assert aziz.keep_off_until <= clock.now and host.active is True


def test_a_cpu_server_leaves_the_model_alone(aziz, host, clock):
    aziz.tick()
    aziz.submit("api", "api", False, False, server=True)
    aziz.tick()
    host.job_states[CPU_SERVER] = "active"
    settled(aziz, clock)
    assert host.active is True and host.started_jobs == [CPU_SERVER]


def test_priority_is_refused_beside_a_gpu_server(aziz, host, clock):
    running_server(aziz, host, clock)
    with pytest.raises(ValueError, match="kapat"):
        aziz.set_priority(True)
    assert host.stopped_jobs == []


def test_a_gpu_job_waits_for_a_gpu_server_and_says_so(aziz, host, clock):
    running_server(aziz, host, clock)
    aziz.submit("render", "işle", True, False)
    aziz.tick()
    assert GPU_JOB not in host.started_jobs and "sunucusu açık" in aziz.job("render").note


def test_a_server_comes_back_after_a_reboot(host, clock, tmp_path):
    aziz = Aziz(host, tmp_path / "gpu-mode", clock)
    running_server(aziz, host, clock)
    host.boot_id = lambda: "another"
    assert Aziz(host, tmp_path / "gpu-mode", clock).job("api").status == "queued"


# The workflow, end to end


def test_job_with_no_more_work_coming_gives_the_gpu_straight_back(aziz, host, clock):
    aziz.tick()
    assert host.active is True
    aziz.submit("render", "işle", True, False)
    aziz.tick()
    assert host.active is False and host.started_jobs == [GPU_JOB]
    host.job_states[GPU_JOB] = "active"
    settled(aziz, clock, 1)
    host.job_states[GPU_JOB] = "inactive"
    settled(aziz, clock, 1)
    assert host.active is True


def test_job_with_more_work_coming_reserves_twenty_minutes(aziz, host, clock):
    aziz.tick()
    aziz.submit("render", "işle", True, True)
    aziz.tick()
    host.job_states[GPU_JOB] = "active"
    settled(aziz, clock, 1)
    host.job_states[GPU_JOB] = "inactive"
    settled(aziz, clock, 1)
    assert host.active is False
    clock.now += KEEP_OFF_SECONDS - 60
    aziz.tick()
    assert host.active is False
    clock.now += 61
    aziz.tick()
    assert host.active is True


# Over HTTP


@pytest.fixture
def http(aziz):
    server = make_server(aziz, port=0)
    import threading

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


def test_http_the_switch_takes_a_second_word(http, host):
    assert httpx.post(f"{http}/mode", json={"mode": "compute"}, timeout=5).json() == {"mode": "compute"}
    assert host.image_app_starts == 1
    refused = httpx.post(f"{http}/mode", json={"mode": "assistant"}, timeout=5)
    assert refused.status_code == 409 and refused.json()["needsConfirm"] is True and "onayla" in refused.json()["reason"]
    assert httpx.post(f"{http}/mode", json={"mode": "assistant", "confirm": True}, timeout=5).json() == {"mode": "assistant"}
    assert host.image_app_stops == 1
    assert httpx.post(f"{http}/mode", json={"mode": "x"}, timeout=5).status_code == 400


def test_http_jobs_route_takes_a_server(http):
    answer = httpx.post(f"{http}/jobs", json={"id": "api", "label": "api", "gpu": True, "server": True}).json()
    assert answer["server"] is True and answer["max_seconds"] == 0
