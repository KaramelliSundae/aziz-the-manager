from __future__ import annotations

import threading
import time

import httpx
import pytest

from aziz import host as arbiter_module
from aziz.host import SystemHost, ImageApp
from aziz.manager import (
    ANSWER_WAIT_SECONDS,
    CLAIM_SECONDS,
    DEFAULT_GPU_JOB_SECONDS,
    FOREIGN_GPU_MIB,
    HOLD_SECONDS,
    JOB_GUARD_RAM_GIB,
    JOB_START_SECONDS,
    KEEP_OFF_SECONDS,
    LOW_RAM_GIB,
    MAX_RESTARTS,
    MAX_VRAM_BEFORE_START_MIB,
    MIN_RAM_GIB,
    PRIORITY_SECONDS,
    QUESTION_QUIET_SECONDS,
    RAM_BACKOFF_SECONDS,
    START_RETRY_SECONDS,
    Aziz as Arbiter,
)
from aziz.server import make_server

# What the image app really keeps on the GPU after a job, measured on the real machine.
IMAGE_APP_CACHE_MIB = 846


class FakeHost:
    def __init__(self):
        self.image_app_state = ImageApp(reachable=True, hooked=True)
        self.foreign = 0
        self.ram = 17.0
        self.active = False
        self.ready_after_start = True
        self.vram = 128
        self.llm_vram = 0
        self.starts = 0
        self.stops = 0
        self.answering = False
        self.units_exist = True
        self.job_states = {}
        self.started_jobs = []
        self.stopped_jobs = []

    def image_app(self):
        return self.image_app_state

    def foreign_gpu_mib(self):
        return self.foreign

    def vram_used_mib(self):
        return self.vram

    def llm_vram_mib(self):
        return self.llm_vram

    def available_ram_gib(self):
        return self.ram

    def llm_active(self):
        return self.active

    def llm_ready(self):
        return self.active and self.ready_after_start

    def start_llm(self):
        self.starts += 1
        self.active = True

    def stop_llm(self):
        self.stops += 1
        self.active = False
        self.llm_vram = 0

    def llm_busy(self):
        return self.answering

    def start_job(self, unit):
        self.job_states[unit] = "active"
        self.started_jobs.append(unit)

    def stop_job(self, unit):
        self.job_states[unit] = "inactive"
        self.stopped_jobs.append(unit)

    def job_state(self, unit):
        return self.job_states.get(unit, "inactive")

    def system(self):
        return {"ramAvailableGib": self.ram}

    def job_known(self, unit):
        return self.units_exist

    def boot_id(self):
        return "boot-1"


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


@pytest.fixture
def host():
    return FakeHost()


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def arbiter(host, tmp_path, clock):
    return Arbiter(host, tmp_path / "gpu-mode", clock)


def test_free_gpu_starts_the_model(arbiter, host):
    assert arbiter.acquire() is None
    assert host.starts == 1


def test_running_model_is_not_started_twice(arbiter, host):
    arbiter.acquire()
    arbiter.acquire()
    assert host.starts == 1


@pytest.mark.parametrize(
    ("change", "word"),
    [
        (lambda h: setattr(h, "image_app_state", ImageApp(True, True, busy=True)), "çalışıyor"),
        (lambda h: setattr(h, "image_app_state", ImageApp(True, hooked=False)), "kancası"),
        (lambda h: setattr(h, "foreign", FOREIGN_GPU_MIB + 1), "başka"),
    ],
)
def test_model_never_starts_while_the_gpu_is_not_free(arbiter, host, change, word):
    change(host)
    assert word in arbiter.acquire()
    assert host.starts == 0


def test_low_memory_blocks_a_new_start(arbiter, host):
    host.ram = MIN_RAM_GIB - 1
    assert "RAM" in arbiter.acquire()
    assert host.starts == 0


def test_running_model_keeps_working_with_its_own_memory_in_use(arbiter, host):
    arbiter.acquire()
    host.ram = MIN_RAM_GIB - 1  # the model itself is what used it
    assert arbiter.acquire() is None


def test_stopped_image_app_leaves_the_gpu_free(arbiter, host):
    host.image_app_state = ImageApp(reachable=False)
    assert arbiter.acquire() is None


def test_model_starts_beside_the_cache_image_app_keeps_after_a_job(arbiter, host):
    host.vram = IMAGE_APP_CACHE_MIB
    assert arbiter.acquire() is None


def test_model_starts_in_the_memory_left_by_a_used_image_app(arbiter, host):
    host.vram, host.ram = IMAGE_APP_CACHE_MIB, 7.8  # measured on the real machine after a job
    assert arbiter.acquire() is None


def test_watch_stops_the_model_when_memory_runs_out(arbiter, host):
    arbiter.acquire()
    host.ram = LOW_RAM_GIB - 0.5
    arbiter.tick()
    assert host.active is False


def test_model_stays_off_when_gpu_memory_is_already_full(arbiter, host):
    host.vram = MAX_VRAM_BEFORE_START_MIB + 1
    assert "belleği" in arbiter.acquire()
    assert host.starts == 0


IDLE_IMAGE_APP = ImageApp(reachable=True, hooked=True)  # its model may still be in RAM


def test_model_comes_back_by_itself_once_image_app_is_idle(arbiter, host):
    host.image_app_state = IDLE_IMAGE_APP
    arbiter.tick()
    assert host.starts == 1


def test_model_comes_back_when_image_app_is_stopped(arbiter, host):
    host.image_app_state = ImageApp(reachable=False)
    arbiter.tick()
    assert host.starts == 1


def test_model_starts_itself_beside_a_image_app_that_keeps_its_model_in_ram(arbiter, host):
    arbiter.tick()  # the default state: the image app reachable and idle
    assert host.starts == 1


def test_model_waits_while_image_app_is_busy(arbiter, host):
    host.image_app_state = ImageApp(True, True, busy=True)
    arbiter.tick()
    assert host.starts == 0


@pytest.mark.parametrize(
    "state",
    [
        ImageApp(True, True, busy=True),  # a job is loading the model back
        ImageApp(True, hooked=False),
    ],
)
def test_model_stays_off_while_image_app_needs_the_gpu(arbiter, host, state):
    host.image_app_state = state
    arbiter.tick()
    assert host.starts == 0


def test_image_mode_keeps_the_model_off(arbiter, host):
    host.image_app_state = IDLE_IMAGE_APP
    arbiter.set_mode("image")
    arbiter.tick()
    assert host.starts == 0


def test_model_stays_off_during_the_hold_after_a_preempt(arbiter, host, clock):
    host.image_app_state = IDLE_IMAGE_APP
    arbiter.preempt()
    arbiter.tick()
    assert host.starts == 0
    clock.now += HOLD_SECONDS + 1
    arbiter.tick()
    assert host.starts == 1


def test_model_does_not_start_itself_without_room(arbiter, host):
    host.image_app_state = IDLE_IMAGE_APP
    host.ram = MIN_RAM_GIB - 1
    arbiter.tick()
    assert host.starts == 0
    host.ram = 20.0
    host.vram = MAX_VRAM_BEFORE_START_MIB + 1
    arbiter.tick()
    assert host.starts == 0


def test_a_crashing_model_is_not_restarted_every_round(arbiter, host, clock):
    host.image_app_state = IDLE_IMAGE_APP
    arbiter.tick()
    host.active = False  # it died straight away
    arbiter.tick()
    assert host.starts == 1
    clock.now += START_RETRY_SECONDS + 1
    arbiter.tick()
    assert host.starts == 2


def test_model_stays_loaded_however_long_it_sits_idle(arbiter, host, clock):
    arbiter.acquire()
    clock.now += 7 * 24 * 3600
    arbiter.tick()
    assert host.active is True and host.stops == 0


def test_low_ram_stop_is_not_undone_by_itself_at_once(arbiter, host, clock):
    arbiter.tick()
    host.ram = LOW_RAM_GIB - 0.5
    arbiter.tick()
    assert host.active is False
    host.ram = 17.0
    clock.now += START_RETRY_SECONDS + 1
    arbiter.tick()
    assert host.active is False  # would flap otherwise
    clock.now += RAM_BACKOFF_SECONDS
    arbiter.tick()
    assert host.active is True
    host.ram = LOW_RAM_GIB - 0.5
    arbiter.tick()
    host.ram = 17.0
    assert arbiter.acquire() is None  # a question does not wait for the backoff


def test_full_cycle_model_leaves_for_a_job_and_returns_after_it(arbiter, host, clock):
    host.image_app_state = IDLE_IMAGE_APP
    arbiter.tick()
    assert host.active is True

    host.llm_vram = 6400
    arbiter.preempt()  # the job's hand-over
    assert host.active is False
    host.image_app_state = ImageApp(True, True, busy=True)  # running, the image app loaded in RAM
    clock.now += 200
    arbiter.tick()
    assert host.active is False

    host.image_app_state = IDLE_IMAGE_APP  # finished; the image app may keep its model in RAM
    arbiter.tick()
    assert host.active is True and host.starts == 2


def test_watch_stops_the_default_model_for_a_job_that_skipped_the_hand_over(arbiter, host):
    host.image_app_state = IDLE_IMAGE_APP
    arbiter.tick()
    host.image_app_state = ImageApp(True, True, busy=True)
    arbiter.tick()
    assert host.active is False


def test_preempt_stops_the_model_and_waits_for_its_memory(arbiter, host):
    arbiter.acquire()
    host.llm_vram = 6400
    arbiter.preempt()
    assert host.stops == 1
    assert host.llm_vram == 0


def test_preempt_does_not_wait_on_memory_that_image_app_itself_holds(arbiter, host):
    host.vram = IMAGE_APP_CACHE_MIB
    started = time.monotonic()
    arbiter.preempt()
    assert time.monotonic() - started < 1


def test_preempt_gives_up_so_image_app_is_never_stuck(host, tmp_path):
    clock = Clock()

    class StuckHost(FakeHost):
        def llm_vram_mib(self):
            clock.now += 5
            return 6400

    arbiter = Arbiter(StuckHost(), tmp_path / "gpu-mode", clock)
    arbiter.preempt()  # returns instead of looping forever


def test_preempt_with_no_model_returns_at_once(arbiter, host):
    arbiter.preempt()
    assert host.stops == 0


def test_model_stays_off_right_after_a_preempt(arbiter, host, clock):
    arbiter.preempt()
    assert arbiter.acquire() is not None
    clock.now += HOLD_SECONDS + 1
    assert arbiter.acquire() is None


def test_preempt_during_model_start_cancels_the_wait(arbiter, host, clock):
    host.ready_after_start = False
    results = []
    thread = threading.Thread(target=lambda: results.append(arbiter.acquire()))
    thread.start()
    while host.starts == 0:
        pass
    arbiter.preempt()
    thread.join(5)
    assert not thread.is_alive()
    assert results and results[0] is not None
    assert host.active is False


@pytest.mark.parametrize(
    "change",
    [
        lambda h: setattr(h, "image_app_state", ImageApp(True, True, busy=True)),
        lambda h: setattr(h, "foreign", 3000),
        lambda h: setattr(h, "image_app_state", ImageApp(True, hooked=False)),
    ],
)
def test_watch_stops_the_model_when_the_gpu_is_needed(arbiter, host, change):
    arbiter.acquire()
    change(host)
    arbiter.tick()
    assert host.active is False


def test_watch_leaves_a_busy_model_alone(arbiter, host):
    arbiter.acquire()
    arbiter.tick()
    assert host.active is True


def test_model_left_running_by_a_restart_stays_on(arbiter, host, clock):
    host.active = True
    arbiter.tick()
    clock.now += 3600
    arbiter.tick()
    assert host.active is True and host.stops == 0  # a deploy must not cut off an answer that is loading


def test_image_mode_stops_the_model_and_blocks_it(arbiter, host):
    arbiter.acquire()
    arbiter.set_mode("image")
    assert host.active is False
    assert "görsel" in arbiter.acquire()


def test_mode_survives_a_restart(arbiter, host, tmp_path, clock):
    arbiter.set_mode("image")
    assert Arbiter(host, tmp_path / "gpu-mode", clock).mode == "image"


def test_unknown_mode_is_refused(arbiter):
    with pytest.raises(ValueError):
        arbiter.set_mode("turbo")


def test_state_reports_a_missing_hook(arbiter, host):
    host.image_app_state = ImageApp(True, hooked=False)
    assert arbiter.state()["gateMissing"] is True


def test_system_host_tells_gpu_processes_apart(monkeypatch):
    units = {1: "0::/system.slice/image-app.service\n", 2: "0::/system.slice/ai-ders-llm.service\n", 3: "0::/user.slice/user-1000.slice\n"}
    monkeypatch.setattr(arbiter_module, "_nvidia_smi", lambda query: "1, 846\n2, 6400\n3, 3000\n[N/A], 5\n")
    monkeypatch.setattr(arbiter_module, "_unit_of", lambda pid: units[pid])
    host = SystemHost()
    assert host.foreign_gpu_mib() == 3000
    assert host.llm_vram_mib() == 6400



def test_system_host_ignores_image_app_cache(monkeypatch):
    monkeypatch.setattr(arbiter_module, "_nvidia_smi", lambda query: f"1, {IMAGE_APP_CACHE_MIB}\n")
    monkeypatch.setattr(arbiter_module, "_unit_of", lambda pid: "0::/system.slice/image-app.service\n")
    assert SystemHost().foreign_gpu_mib() == 0


@pytest.fixture
def http(arbiter):
    server = make_server(arbiter, port=0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


def test_server_listens_on_localhost_only(arbiter):
    server = make_server(arbiter, port=0)
    assert server.server_address[0] == "127.0.0.1"
    server.server_close()


def test_http_preempt_answers_ok(http, arbiter, host):
    arbiter.acquire()
    assert httpx.post(f"{http}/preempt").status_code == 200
    assert host.active is False


def test_http_acquire_reports_why_it_cannot_run(http, host):
    host.image_app_state = ImageApp(True, True, busy=True)
    response = httpx.post(f"{http}/acquire")
    assert response.status_code == 409
    assert "çalışıyor" in response.json()["reason"]


def test_http_acquire_returns_the_model_address(http):
    assert httpx.post(f"{http}/acquire", timeout=5).json()["url"].startswith("http://127.0.0.1")


def test_http_mode_rejects_bad_input(http):
    assert httpx.post(f"{http}/mode", json={"mode": "x"}).status_code == 400
    assert httpx.post(f"{http}/mode", content=b"not json").status_code == 400
    assert httpx.post(f"{http}/mode", json={"mode": "image"}).json() == {"mode": "image"}


def test_http_unknown_path_is_404(http):
    assert httpx.get(f"{http}/nope").status_code == 404
    assert httpx.post(f"{http}/nope").status_code == 404


GPU_UNIT = "offload-gpu-job@render.service"
CPU_UNIT = "offload-job@build.service"


def on_gpu(host):
    host.active = True
    host.llm_vram = 6266


def finish(host, unit, how="inactive"):
    host.job_states[unit] = how


def test_cpu_job_leaves_the_model_alone(arbiter, host):
    on_gpu(host)
    arbiter.submit("build", "derleme", False, False)
    arbiter.tick()
    assert host.started_jobs == [CPU_UNIT] and host.stops == 0 and host.active


def test_gpu_job_sends_the_model_to_disk_then_runs_and_the_model_returns(arbiter, host, clock):
    on_gpu(host)
    arbiter.submit("render", "işle", True, False)
    arbiter.tick()
    assert host.stops == 1 and host.started_jobs == [GPU_UNIT]
    assert arbiter.state()["gpuOwner"] == "job"
    assert "hesaplama" in arbiter.state()["blocked"]
    assert host.starts == 0  # stays off beside the job

    clock.now += 60
    finish(host, GPU_UNIT)
    arbiter.tick()
    assert arbiter.job("render").status == "done"
    assert host.starts == 1 and host.active


def test_a_job_waits_while_the_model_is_still_freeing_its_memory(arbiter, host):
    on_gpu(host)
    stop = host.stop_llm
    host.stop_llm = lambda: (stop(), setattr(host, "llm_vram", 6266))
    arbiter.submit("render", "işle", True, False)
    arbiter.tick()
    assert host.started_jobs == [] and arbiter.claim == "render"
    host.llm_vram = 0
    arbiter.tick()
    assert host.started_jobs == [GPU_UNIT]


def test_likely_gpu_work_keeps_the_gpu_free_then_gives_it_back(arbiter, host, clock):
    on_gpu(host)
    arbiter.submit("render", "işle", True, True)
    arbiter.tick()
    clock.now += 60
    finish(host, GPU_UNIT)
    arbiter.tick()
    assert host.starts == 0 and "Sıradaki GPU" in arbiter.state()["blocked"]
    clock.now += KEEP_OFF_SECONDS + 1
    arbiter.tick()
    assert host.starts == 1


def test_next_job_in_the_wait_does_not_load_the_model_in_between(arbiter, host, clock):
    on_gpu(host)
    arbiter.submit("render", "işle", True, True)
    arbiter.tick()
    clock.now += 60
    finish(host, GPU_UNIT)
    arbiter.tick()
    arbiter.submit("second", "ikinci", True, False)
    arbiter.tick()
    assert host.starts == 0 and "offload-gpu-job@second.service" in host.started_jobs


def test_a_question_ends_the_wait_at_once(arbiter, host, clock):
    on_gpu(host)
    arbiter.submit("render", "işle", True, True)
    arbiter.tick()
    clock.now += 60
    finish(host, GPU_UNIT)
    arbiter.tick()
    assert arbiter.keep_off_until > clock.now
    assert arbiter.acquire() is None and host.active
    assert arbiter.keep_off_until == 0  # over for good, even if the model has to stop again a moment later


def test_the_sender_can_call_off_the_wait(arbiter, host, clock):
    on_gpu(host)
    arbiter.submit("render", "işle", True, True)
    arbiter.tick()
    clock.now += 60
    finish(host, GPU_UNIT)
    arbiter.tick()
    arbiter.forecast(False)
    arbiter.tick()
    assert host.active


@pytest.mark.parametrize("how", ["failed", "inactive"])
def test_a_job_that_dies_brings_the_model_back(arbiter, host, clock, how):
    on_gpu(host)
    arbiter.submit("render", "işle", True, False)
    arbiter.tick()
    clock.now += 60
    finish(host, GPU_UNIT, how)
    arbiter.tick()
    assert arbiter.job("render").status == ("failed" if how == "failed" else "done") and host.active


def test_cancel_brings_the_model_back_without_waiting(arbiter, host, clock):
    on_gpu(host)
    arbiter.submit("render", "işle", True, True)
    arbiter.tick()
    assert arbiter.cancel("render") is True
    assert GPU_UNIT in host.stopped_jobs and arbiter.job("render").status == "cancelled"
    arbiter.tick()
    assert host.active


def test_an_answer_in_progress_finishes_first_but_not_forever(arbiter, host, clock):
    on_gpu(host)
    host.answering = True
    arbiter.submit("render", "işle", True, False)
    arbiter.tick()
    assert host.stops == 0 and arbiter.claim is None
    clock.now += ANSWER_WAIT_SECONDS + 1
    arbiter.tick()
    assert host.stops == 1 and host.started_jobs == [GPU_UNIT]


def test_image_app_beats_a_gpu_job_on_preempt(arbiter, host):
    arbiter.submit("render", "işle", True, False)
    arbiter.tick()
    arbiter.preempt()
    job = arbiter.job("render")
    assert GPU_UNIT in host.stopped_jobs and job.status == "queued" and "görsel uygulaması" in job.note
    host.started_jobs.clear()
    arbiter.tick()
    assert host.started_jobs == []  # the hold after the preempt


def test_busy_image_app_stops_a_gpu_job_but_not_a_cpu_job(arbiter, host):
    arbiter.submit("render", "işle", True, False)
    arbiter.submit("build", "derleme", False, False)
    arbiter.tick()
    host.image_app_state = ImageApp(reachable=True, hooked=True, busy=True)
    arbiter.tick()
    assert GPU_UNIT in host.stopped_jobs and CPU_UNIT not in host.stopped_jobs


def test_gpu_job_waits_for_image_app_and_image_mode(arbiter, host):
    host.image_app_state = ImageApp(reachable=True, hooked=True, busy=True)
    arbiter.submit("render", "işle", True, False)
    arbiter.tick()
    assert host.started_jobs == []
    host.image_app_state = ImageApp(reachable=True, hooked=True)
    arbiter.set_mode("image")
    arbiter.tick()
    assert host.started_jobs == [] and arbiter.mode == "image"


def test_image_mode_stops_a_running_job_and_finishing_does_not_change_the_mode(arbiter, host):
    arbiter.submit("render", "işle", True, False)
    arbiter.tick()
    arbiter.set_mode("image")
    assert GPU_UNIT in host.stopped_jobs and arbiter.mode == "image"


def test_only_one_gpu_job_at_a_time(arbiter, host):
    arbiter.submit("render", "işle", True, False)
    arbiter.submit("second", "ikinci", True, False)
    arbiter.tick()
    arbiter.tick()
    assert host.started_jobs == [GPU_UNIT]
    finish(host, GPU_UNIT)
    arbiter.clock.now += 60
    arbiter.tick()
    assert "offload-gpu-job@second.service" in host.started_jobs and host.starts == 0


def test_priority_puts_the_student_first_and_jobs_resume_after(arbiter, host, clock):
    arbiter.submit("render", "işle", True, False)
    arbiter.tick()
    arbiter.set_priority(True)
    assert GPU_UNIT in host.stopped_jobs and arbiter.job("render").status == "queued"
    arbiter.tick()
    assert host.active and arbiter.state()["prioritySeconds"] > 0
    host.started_jobs.clear()
    arbiter.tick()
    assert host.started_jobs == []
    clock.now += PRIORITY_SECONDS + 1
    arbiter.tick()
    assert host.started_jobs == [GPU_UNIT]


def test_priority_can_be_ended_early_and_is_refused_in_image_mode(arbiter, host):
    arbiter.set_priority(True)
    arbiter.set_priority(False)
    assert arbiter.state()["prioritySeconds"] == 0
    arbiter.set_mode("image")
    with pytest.raises(ValueError, match="Otomatik"):
        arbiter.set_priority(True)


def test_image_app_still_beats_priority(arbiter, host):
    arbiter.set_priority(True)
    host.image_app_state = ImageApp(reachable=True, hooked=True, busy=True)
    assert "görsel uygulaması" in arbiter.acquire()


def test_a_job_gives_way_before_the_model_hits_its_low_ram_backoff(arbiter, host):
    on_gpu(host)
    arbiter.submit("build", "derleme", False, False)
    arbiter.tick()
    host.ram = JOB_GUARD_RAM_GIB - 0.1
    arbiter.tick()
    assert CPU_UNIT in host.stopped_jobs and host.active


def test_a_job_interrupted_again_and_again_stops_coming_back(arbiter, host, clock):
    arbiter.submit("render", "işle", True, False)
    for _ in range(MAX_RESTARTS + 1):
        arbiter.tick()
        arbiter.preempt()
        clock.now += 60
    assert arbiter.job("render").status == "interrupted"


def test_bad_job_names_are_refused(arbiter):
    for name in ("../../etc", "A", "", "a b", "x;rm", "-x", "a" * 64):
        with pytest.raises(ValueError):
            arbiter.submit(name, "", False, False)


def test_a_restart_keeps_the_queue_and_never_the_wait(host, tmp_path, clock):
    first = Arbiter(host, tmp_path / "gpu-mode", clock)
    first.submit("render", "işle", True, True)
    first.tick()
    second = Arbiter(host, tmp_path / "gpu-mode", clock)
    assert second.job("render").status == "running" and second.gpu_reserved()
    finish(host, GPU_UNIT)
    clock.now += 60
    second.tick()
    assert second.job("render").status == "done"


def test_job_routes_over_http(host, tmp_path, clock):
    server = make_server(Arbiter(host, tmp_path / "gpu-mode", clock), port=0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        made = httpx.post(f"{base}/jobs", json={"id": "render", "label": "işle", "gpu": True, "nextGpu": "likely"})
        assert made.status_code == 200 and made.json()["next_gpu_likely"] is True
        assert httpx.post(f"{base}/jobs", json={"id": "../x"}).status_code == 409
        assert httpx.get(f"{base}/jobs/render").json()["gpu"] is True
        assert httpx.get(f"{base}/state").json()["jobs"][0]["id"] == "render"
        assert httpx.post(f"{base}/forecast", json={"nextGpu": "unlikely"}).status_code == 200
        assert httpx.post(f"{base}/priority", json={"on": True}).json()["prioritySeconds"] > 0
        assert httpx.post(f"{base}/jobs/render/cancel").status_code == 200
        assert httpx.post(f"{base}/jobs/none/cancel").status_code == 404
    finally:
        server.shutdown()
