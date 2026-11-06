from __future__ import annotations

import json
import threading

import httpx

from aziz.journal import KEPT_IN_MEMORY, REPEAT_SECONDS, Journal
from aziz.manager import KEEP_OFF_SECONDS, Aziz
from aziz.server import make_server

from .test_manager import Clock, FakeHost

GPU_UNIT = "offload-gpu-job@render.service"


class Now:
    def __init__(self):
        self.value = 1000.0

    def __call__(self):
        return self.value


def journal(tmp_path, **options):
    now = Now()
    return Journal(tmp_path / "journal.jsonl", now, **options), now


def test_entries_survive_a_restart(tmp_path):
    first, _ = journal(tmp_path)
    first.add("job", "İş başladı")
    second, _ = journal(tmp_path)
    assert [entry["text"] for entry in second.recent(5)] == ["İş başladı"]


def test_size_is_limited_for_ever(tmp_path):
    log, now = journal(tmp_path, max_bytes=2000)
    for number in range(5000):
        now.value += 1
        log.add("job", f"İş {number} bitti")
    assert (tmp_path / "journal.jsonl").stat().st_size < 2200
    assert (tmp_path / "journal.1").stat().st_size < 2200
    assert len(log.entries) == KEPT_IN_MEMORY
    assert log.recent(1)[0]["text"] == "İş 4999 bitti"


def test_a_repeat_counts_up_instead_of_burying_the_rest(tmp_path):
    log, now = journal(tmp_path)
    log.add("problem", "Hata")
    for _ in range(100):
        now.value += 2
        log.add("problem", "Hata")
    assert len(log.entries) == 1 and log.entries[0]["count"] == 101
    now.value += REPEAT_SECONDS + 1
    log.add("problem", "Hata")
    assert len(log.entries) == 2


def test_a_broken_disk_never_raises(tmp_path):
    (tmp_path / "file").write_text("x")
    log = Journal(tmp_path / "file" / "journal.jsonl")
    log.add("job", "İş başladı")
    assert log.recent(1)[0]["text"] == "İş başladı"


def test_damaged_lines_are_skipped(tmp_path):
    good = json.dumps({"at": 1, "kind": "job", "text": "iyi"})
    (tmp_path / "journal.jsonl").write_text('{"cut sh\n[1]\n{"no": "text"}\n' + good + "\n\xff\n", encoding="latin-1")
    log, _ = journal(tmp_path)
    assert [entry["text"] for entry in log.recent(10)] == ["iyi"]


def test_many_threads_at_once(tmp_path):
    log, _ = journal(tmp_path)
    def write(name):
        for number in range(200):
            log.add("job", f"{name} {number}")
    threads = [threading.Thread(target=write, args=(n,)) for n in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    lines = (tmp_path / "journal.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1600 and all(json.loads(line)["text"] for line in lines)


def test_the_story_of_a_gpu_job_is_told(tmp_path):
    host, clock = FakeHost(), Clock()
    host.active, host.llm_vram = True, 6266
    aziz = Aziz(host, tmp_path / "gpu-mode", clock)
    aziz.submit("render", "Görsel işleme", True, True)
    aziz.tick()
    clock.now += 90
    host.job_states[GPU_UNIT] = "inactive"
    aziz.tick()
    clock.now += KEEP_OFF_SECONDS + 1
    aziz.tick()
    clock.now += 2
    aziz.tick()
    told = [entry["text"] for entry in aziz.journal.recent(20)]
    assert told == [
        "Aziz göreve başladı",
        "Yeni iş geldi: Görsel işleme (GPU)",
        "Kamil diske indi: GPU işi için (Görsel işleme)",
        "İş başladı: Görsel işleme (GPU)",
        "İş bitti: Görsel işleme (2 dakika)",
        "Yeni GPU işi bekleniyor, Kamil en çok 20 dakika diskte kalacak",
        "Kamil yükleniyor: GPU boşaldı",
        "Kamil hazır, 2 saniyede yüklendi",
    ]


def test_preempt_failures_and_priority_are_written_down(tmp_path):
    host, clock = FakeHost(), Clock()
    aziz = Aziz(host, tmp_path / "gpu-mode", clock)
    aziz.preempt()
    aziz.set_priority(True)
    aziz.set_priority(False)
    aziz.set_mode("compute")
    aziz.set_mode("compute")
    aziz.set_mode("assistant")
    told = " | ".join(entry["text"] for entry in aziz.journal.recent(20))
    assert "Görsel uygulaması GPU'yu istedi, 0 saniyede boşaldı" in told
    assert "öne alındı" in told and "önceliği bitirildi" in told
    assert told.count("GPU işlere ayrıldı") == 1 and "AI-Asistan" in told


def test_state_carries_only_the_last_few_and_the_route_gives_more(tmp_path):
    aziz = Aziz(FakeHost(), tmp_path / "gpu-mode", Clock())
    for number in range(50):
        aziz.journal.add("job", f"satır {number}")
    assert len(aziz.state()["log"]) == 8
    server = make_server(aziz, port=0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        assert len(httpx.get(f"{base}/log?limit=30", timeout=5).json()["entries"]) == 30
        assert len(httpx.get(f"{base}/log?limit=99999", timeout=5).json()["entries"]) == 51
        assert httpx.get(f"{base}/log?limit=abc", timeout=5).status_code == 409
    finally:
        server.shutdown()
