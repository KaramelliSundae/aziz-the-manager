"""Each test here is a way Aziz could strand the model, hurt the image app, or lie about a job."""

from __future__ import annotations

import json
import subprocess
import threading

import httpx
import pytest

from aziz.host import ImageApp
from aziz.manager import (
    ANSWER_WAIT_SECONDS,
    CLAIM_SECONDS,
    DEFAULT_GPU_JOB_SECONDS,
    JOB_START_SECONDS,
    KEEP_OFF_SECONDS,
    MAX_JOBS,
    MIN_RAM_GIB,
    QUESTION_QUIET_SECONDS,
    Aziz,
)
from aziz.server import MAX_BODY_BYTES, make_server

from .test_manager import Clock, FakeHost

GPU_UNIT = "offload-gpu-job@render.service"
CPU_UNIT = "offload-job@build.service"
BROKEN = subprocess.CalledProcessError(1, "systemctl")


def raises(error):
    def fail(*args):
        raise error
    return fail


@pytest.fixture
def host():
    return FakeHost()


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def aziz(host, tmp_path, clock):
    return Aziz(host, tmp_path / "gpu-mode", clock)


def on_gpu(host):
    host.active = True
    host.llm_vram = 6266


def run_job(aziz, host, clock, *flags):
    aziz.submit("render", "işle", True, *flags)
    aziz.tick()
    assert host.job_state(GPU_UNIT) == "active"
    clock.now += 60


# A broken job must never keep the model off


def test_a_unit_that_cannot_start_fails_the_job_and_the_model_returns(aziz, host):
    on_gpu(host)
    host.start_job = raises(BROKEN)
    aziz.submit("render", "işle", True, False)
    aziz.tick()
    assert aziz.job("render").status == "failed" and aziz.job("render").note == "başlatılamadı"
    assert aziz.claim is None
    aziz.tick()
    assert host.active and host.starts == 1


def test_a_gpu_job_without_a_unit_never_sends_the_model_to_disk(aziz, host):
    on_gpu(host)
    host.units_exist = False
    aziz.submit("render", "işle", True, False)
    aziz.tick()
    assert aziz.job("render").status == "failed" and host.stops == 0 and host.active


def test_a_failing_job_round_still_runs_the_model_rules(aziz, host, monkeypatch):
    monkeypatch.setattr(aziz, "_manage_jobs", raises(RuntimeError("bug")))
    aziz.tick()
    assert host.active


def test_a_job_past_its_limit_is_stopped_and_the_model_returns(aziz, host, clock):
    on_gpu(host)
    run_job(aziz, host, clock, False)
    clock.now += DEFAULT_GPU_JOB_SECONDS + 1
    aziz.tick()
    assert GPU_UNIT in host.stopped_jobs
    assert aziz.job("render").status == "failed" and aziz.job("render").note == "süresi doldu"
    assert host.active


def test_a_sender_can_ask_for_a_longer_limit_within_a_day(aziz, host, clock):
    aziz.submit("render", "işle", True, False, max_minutes=600)
    aziz.tick()
    clock.now += DEFAULT_GPU_JOB_SECONDS + 1
    aziz.tick()
    assert aziz.job("render").status == "running"
    with pytest.raises(ValueError):
        aziz.submit("other", "", True, False, max_minutes=100000)
    with pytest.raises(ValueError):
        aziz.submit("other", "", True, False, max_minutes=0)


def test_model_memory_that_never_goes_fails_the_job_not_the_model(aziz, host, clock):
    on_gpu(host)
    stop = host.stop_llm
    host.stop_llm = lambda: (stop(), setattr(host, "llm_vram", 6266))
    aziz.submit("render", "işle", True, False)
    aziz.tick()
    assert aziz.claim == "render" and host.started_jobs == []
    clock.now += CLAIM_SECONDS + 1
    aziz.tick()
    assert aziz.job("render").status == "failed" and aziz.claim is None


def test_a_claim_ends_when_its_job_is_cancelled(aziz, host):
    on_gpu(host)
    stop = host.stop_llm
    host.stop_llm = lambda: (stop(), setattr(host, "llm_vram", 6266))
    aziz.submit("render", "işle", True, False)
    aziz.tick()
    assert aziz.cancel("render") and aziz.claim is None
    host.llm_vram = 0
    aziz.tick()
    assert host.active


# The image app always wins, whatever else is going wrong


def test_preempt_stops_the_model_even_if_the_job_will_not_stop(aziz, host, clock, monkeypatch):
    monkeypatch.setattr("aziz.manager.time.sleep", lambda s: setattr(clock, "now", clock.now + s))
    on_gpu(host)
    aziz.submit("render", "işle", True, False)
    aziz.tick()
    host.stop_job = raises(BROKEN)
    host.foreign = 3000
    assert aziz.preempt() is False  # it says so, and it returned
    assert aziz.job("render").status == "running"


def test_preempt_survives_a_broken_gpu_driver(aziz, host):
    on_gpu(host)
    host.llm_vram_mib = raises(BROKEN)
    assert aziz.preempt() is False
    assert not host.active


def test_a_job_cannot_start_after_a_preempt_slipped_in(aziz, host, clock):
    aziz.submit("render", "işle", True, False)
    aziz.preempt()
    job = aziz.job("render")
    assert aziz._start(job) is False and host.started_jobs == []


def test_a_running_job_stops_during_the_hold_even_before_image_app_shows_busy(aziz, host, monkeypatch):
    aziz.submit("render", "işle", True, False)
    aziz.tick()
    with aziz._start_lock:
        aziz.held_until = aziz.clock() + 30
    aziz.tick()
    assert GPU_UNIT in host.stopped_jobs


def test_a_job_that_is_not_safe_to_rerun_is_not_rerun(aziz, host):
    aziz.submit("render", "işle", True, False, restart=False)
    aziz.tick()
    aziz.preempt()
    assert aziz.job("render").status == "interrupted"
    host.started_jobs.clear()
    aziz.clock.now += 100
    aziz.tick()
    assert host.started_jobs == []


def test_a_stop_that_failed_is_tried_again_every_round(aziz, host):
    aziz.submit("render", "işle", True, False)
    aziz.tick()
    working_stop = host.stop_job
    host.stop_job = raises(BROKEN)
    aziz.set_mode("image")
    assert aziz.job("render").status == "running"
    host.stop_job = working_stop
    aziz.tick()
    assert GPU_UNIT in host.stopped_jobs and aziz.job("render").status == "queued"


def test_priority_retries_a_stop_that_failed_too(aziz, host):
    aziz.submit("render", "işle", True, False)
    aziz.tick()
    working_stop = host.stop_job
    host.stop_job = raises(BROKEN)
    aziz.set_priority(True)
    host.stop_job = working_stop
    aziz.tick()
    assert GPU_UNIT in host.stopped_jobs


def test_a_silent_image_app_is_not_taken_for_a_stopped_one(aziz, host):
    on_gpu(host)
    host.image_app_state = ImageApp(reachable=False, running=True)
    aziz.submit("render", "işle", True, False)
    aziz.tick()
    assert host.started_jobs == [] and host.stops == 0 and "yanıt" in aziz.job("render").note
    host.image_app_state = ImageApp(reachable=False)  # really stopped: the GPU is free
    aziz.tick()
    assert host.started_jobs == [GPU_UNIT]


def test_a_full_gpu_keeps_the_job_waiting_without_stopping_the_model(aziz, host):
    on_gpu(host)
    host.vram = 6266 + 3000
    aziz.submit("render", "işle", True, False)
    aziz.tick()
    assert host.stops == 0 and host.started_jobs == [] and "dolu" in aziz.job("render").note
    host.vram = 6266 + 850  # the image app's normal cache
    aziz.tick()
    assert host.stops == 1 and host.started_jobs == [GPU_UNIT]


# The model comes first when the student is using it


def test_turn_in_progress_keeps_a_job_waiting_between_its_model_calls(aziz, host, clock):
    on_gpu(host)
    assert aziz.acquire() is None
    aziz.submit("render", "işle", True, False)
    aziz.tick()
    assert host.stops == 0 and "cevab" in aziz.job("render").note
    clock.now += QUESTION_QUIET_SECONDS + 1
    aziz.tick()
    assert host.stops == 1 and host.started_jobs == [GPU_UNIT]


def test_constant_questions_cannot_starve_a_job_for_ever(aziz, host, clock):
    on_gpu(host)
    aziz.submit("render", "işle", True, False)
    aziz.acquire()
    aziz.tick()
    for _ in range(int(ANSWER_WAIT_SECONDS / 10) + 2):
        clock.now += 10
        if not host.started_jobs:
            aziz.acquire()
        aziz.tick()
    assert host.started_jobs == [GPU_UNIT]


def test_a_question_during_a_job_means_no_wait_after_it(aziz, host, clock):
    on_gpu(host)
    run_job(aziz, host, clock, True)
    assert "hesaplama" in aziz.acquire()
    host.job_states[GPU_UNIT] = "inactive"
    aziz.tick()
    assert host.active and aziz.keep_off_until == 0


def test_a_failed_job_does_not_keep_the_model_waiting(aziz, host, clock):
    on_gpu(host)
    run_job(aziz, host, clock, True)
    host.job_states[GPU_UNIT] = "failed"
    aziz.tick()
    assert host.active


def test_the_wait_cannot_be_extended_by_the_sender(aziz, host, clock):
    run_job(aziz, host, clock, True)
    host.job_states[GPU_UNIT] = "inactive"
    aziz.tick()
    before = aziz.keep_off_until
    clock.now += 60
    aziz.forecast(True)
    assert aziz.keep_off_until == before
    clock.now += KEEP_OFF_SECONDS
    aziz.tick()
    assert host.active


def test_a_foreign_gpu_program_does_not_get_the_model_stopped_for_a_job(aziz, host):
    on_gpu(host)
    host.foreign = 0
    aziz.submit("render", "işle", True, False)
    host.foreign = 2000
    aziz._manage_jobs()
    assert host.started_jobs == [] and aziz.claim is None
    assert "başka" in aziz.job("render").note


def test_image_mode_is_the_students_latest_word_over_priority(aziz, host):
    aziz.set_priority(True)
    aziz.set_mode("image")
    assert aziz.state()["prioritySeconds"] == 0


def test_priority_pauses_cpu_jobs_and_image_mode_does_not(aziz, host):
    aziz.set_priority(True)
    aziz.submit("build", "derleme", False, False)
    aziz.tick()
    assert host.started_jobs == [] and "öne" in aziz.job("build").note
    aziz.set_priority(False)
    aziz.set_mode("image")
    aziz.tick()
    assert host.started_jobs == [CPU_UNIT]


def test_priority_moves_a_cpu_job_aside_when_the_model_needs_its_memory(aziz, host):
    aziz.submit("build", "derleme", False, False)
    aziz.tick()
    host.active = False  # the model is off and this job's memory is what keeps it from starting
    aziz.set_priority(True)
    host.ram = MIN_RAM_GIB - 1
    aziz.tick()
    assert CPU_UNIT in host.stopped_jobs


# Telling the truth about jobs


@pytest.mark.parametrize("state", ["", "unknown", "maintenance"])
def test_a_systemctl_hiccup_does_not_read_as_a_finished_job(aziz, host, clock, state):
    run_job(aziz, host, clock, False)
    host.job_states[GPU_UNIT] = state
    aziz.tick()
    assert aziz.job("render").status == "running" and not host.active


def test_reusing_a_name_is_not_fooled_by_the_last_failure(aziz, host, clock):
    aziz.submit("render", "işle", True, False)
    aziz.tick()
    clock.now += 60
    host.job_states[GPU_UNIT] = "failed"
    aziz.tick()
    assert aziz.job("render").status == "failed"
    aziz.submit("render", "yine", True, False)
    start = host.start_job
    host.start_job = lambda unit: (start(unit), host.job_states.__setitem__(unit, "failed"))
    aziz.tick()
    assert aziz.job("render").status == "running"  # the fresh start still reads as the old failure for a moment
    clock.now += JOB_START_SECONDS + 1
    aziz.tick()
    assert aziz.job("render").status == "failed"


def test_a_reboot_puts_a_running_job_back_instead_of_calling_it_done(host, tmp_path, clock):
    first = Aziz(host, tmp_path / "gpu-mode", clock)
    first.submit("render", "işle", True, False)
    first.tick()
    host.boot_id = lambda: "boot-2"
    host.job_states.clear()
    second = Aziz(host, tmp_path / "gpu-mode", clock)
    job = second.job("render")
    assert job.status == "queued" and "yeniden" in job.note and not second.gpu_reserved()


def test_a_reboot_does_not_rerun_a_job_that_is_not_safe_to_rerun(host, tmp_path, clock):
    first = Aziz(host, tmp_path / "gpu-mode", clock)
    first.submit("render", "işle", True, False, restart=False)
    first.tick()
    host.boot_id = lambda: "boot-2"
    assert Aziz(host, tmp_path / "gpu-mode", clock).job("render").status == "interrupted"


@pytest.mark.parametrize("content", ["not json", "[]", '{"jobs": [{"nonsense": 1}]}', '{"jobs": 5}', "null"])
def test_a_damaged_jobs_file_starts_clean(host, tmp_path, clock, content):
    (tmp_path / "jobs.json").write_text(content)
    aziz = Aziz(host, tmp_path / "gpu-mode", clock)
    assert aziz.jobs == []
    aziz.tick()
    assert host.active


def test_a_full_disk_costs_only_the_saved_copy(aziz, host, monkeypatch):
    aziz.jobs_file = aziz.jobs_file.parent / "gpu-mode" / "not-a-folder" / "jobs.json"
    aziz.state_file.write_text("auto")
    aziz.submit("render", "işle", True, False)
    aziz.tick()
    assert aziz.job("render").status == "running"
    assert aziz.preempt() is True


def test_a_failed_cancel_says_so_and_the_job_keeps_its_status(aziz, host):
    aziz.submit("render", "işle", True, False)
    aziz.tick()
    host.stop_job = raises(BROKEN)
    with pytest.raises(subprocess.CalledProcessError):
        aziz.cancel("render")
    assert aziz.job("render").status == "running"


def test_cancel_of_a_finished_job_is_refused(aziz, host, clock):
    aziz.submit("render", "işle", True, False)
    aziz.tick()
    clock.now += 60
    host.job_states[GPU_UNIT] = "inactive"
    aziz.tick()
    assert aziz.cancel("render") is False and aziz.job("render").status == "done"


def test_state_says_why_the_stopped_model_cannot_start(aziz, host):
    host.ram = 1.0
    assert "RAM" in aziz.state()["blocked"]


# Nothing a caller sends reaches a unit name


@pytest.mark.parametrize("name", ["abc\n", "abc\x00", "a/b", "a.service", "a@b", "A", "", " a", 5, None, ["a"]])
def test_only_clean_names_become_units(aziz, name):
    with pytest.raises(ValueError):
        aziz.submit(name, "", True, False)


def test_labels_are_cleaned_and_cut(aziz):
    assert aziz.submit("ok", "a\n\x1b[31mb" + "c" * 200, False, False).label.startswith("a[31mb")
    assert len(aziz.job("ok").label) == 80


def test_the_queue_has_a_limit(aziz):
    for number in range(MAX_JOBS):
        aziz.submit(f"j{number}", "", False, False)
    with pytest.raises(ValueError):
        aziz.submit("onemore", "", False, False)


# HTTP: every caller gets an answer


@pytest.fixture
def http(aziz):
    server = make_server(aziz, port=0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


def test_preempt_always_answers_even_with_the_machine_on_fire(aziz, host, http):
    for name in ("llm_vram_mib", "foreign_gpu_mib", "stop_llm", "llm_active", "image_app"):
        setattr(host, name, raises(BROKEN))
    response = httpx.post(f"{http}/preempt", timeout=20)
    assert response.status_code == 200 and response.json()["free"] is False


def test_acquire_on_a_broken_host_gives_the_api_a_reason(host, http):
    host.foreign_gpu_mib = raises(BROKEN)
    response = httpx.post(f"{http}/acquire", timeout=20)
    assert response.status_code == 500 and response.json()["reason"]


@pytest.mark.parametrize("body", [b"[]", b"5", b'"x"', b"null", b"{bad", b"\xff\xfe"])
@pytest.mark.parametrize("path", ["/jobs", "/forecast", "/priority"])
def test_bad_bodies_get_a_clear_refusal(http, path, body):
    response = httpx.post(f"{http}{path}", content=body, timeout=5)
    assert response.status_code in (409, 200)
    assert response.json() is not None


def test_a_huge_body_is_refused_without_being_read(http):
    response = httpx.post(f"{http}/jobs", content=b"x" * (MAX_BODY_BYTES + 1), timeout=5)
    assert response.status_code == 409


@pytest.mark.parametrize("minutes", ["abc", [], {}])
def test_bad_limits_are_refused(http, minutes):
    response = httpx.post(f"{http}/jobs", json={"id": "ok", "maxMinutes": minutes}, timeout=5)
    assert response.status_code in (409, 500) and response.json()["reason"]


def test_a_cancel_that_fails_answers_with_a_reason(aziz, host, http):
    aziz.submit("render", "işle", True, False)
    aziz.tick()
    host.stop_job = raises(BROKEN)
    response = httpx.post(f"{http}/jobs/render/cancel", timeout=5)
    assert response.status_code == 500 and response.json()["reason"]


def test_status_from_the_api_is_valid_json_with_job_details(aziz, http):
    aziz.submit("render", "işle", True, False, max_minutes=5)
    body = httpx.get(f"{http}/state", timeout=5).json()
    assert json.dumps(body) and body["jobs"][0]["label"] == "işle"
    assert httpx.get(f"{http}/jobs/render", timeout=5).json()["max_seconds"] == 300


def test_the_day_counts_time_loads_and_jobs(tmp_path):
    from tests.test_manager import FakeHost

    now = [1000.0]
    host = FakeHost()
    aziz = Aziz(host, tmp_path / "gpu-mode", lambda: now[0])
    host.active = True
    aziz.tick()
    assert aziz.state()["kamilToday"]["upSeconds"] == 0
    now[0] += 10
    aziz.tick()
    assert aziz.state()["kamilToday"]["upSeconds"] == 10
    now[0] += 5000  # the machine slept: that was not work
    aziz.tick()
    assert aziz.state()["kamilToday"]["upSeconds"] == 40
    host.active = False
    aziz.tick()
    now[0] += 100  # off the GPU counts for nothing
    host.active = True
    aziz.tick()
    assert aziz.state()["kamilToday"]["upSeconds"] == 40
