"""The decisions. Every rule that says who gets the GPU lives here, and nothing here talks to the machine directly.

The model stays loaded, idle or not (1.5 W idle, measured; a cold start costs 20-30 s on the next question). It
leaves only while something with a better claim needs the GPU, and it comes back by itself. If anything goes
wrong in job handling, the model logic still runs, so a broken job can never keep the model off.

What is deliberately not saved: the wait for more GPU work, priority, and the claim. A crash or a reboot must
bring the model back, never strand it. Jobs are saved, and a job's systemd unit is the truth about it.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path

from .host import FREE_SECONDS, IMAGE_APP_NAME, Host
from .journal import Journal
from .stats import DayStats

log = logging.getLogger(__name__)

MODES = ("assistant", "compute")
# What the modes used to be called: a file saved back then, or an app not yet updated, still means the same.
OLD_MODE_NAMES = {"auto": "assistant", "image": "compute"}
# After the model was stopped for low RAM it does not start itself again for this long, so it cannot flap.
# A question from the phone still starts it at once.
RAM_BACKOFF_SECONDS = 15 * 60
MAX_COUNTED_TICK_SECONDS = 30.0  # a longer gap is the machine or the service having been away, not time at work
START_RETRY_SECONDS = 30.0  # a model that fails to start must not be restarted every watch round
HOLD_SECONDS = 30.0  # after a preempt, until the image app's own state shows the work
START_SECONDS = 45.0
FOREIGN_GPU_MIB = 100
MAX_VRAM_BEFORE_START_MIB = 1400  # the image app keeps about 850 MiB cached after a job
# Weights are memory-mapped, so this is reclaimable cache, not a hard need. The image app holds about 21 GiB after its
# first job, which leaves about 8. Under 7 the default model's RAM share no longer stays cached and it slows down.
MIN_RAM_GIB = 7.0
LOW_RAM_GIB = 2.0

JOB_ID = re.compile(r"[a-z0-9][a-z0-9-]{0,62}")
MAX_JOBS = 16
MAX_CPU_JOBS = 2
MAX_RESTARTS = 3  # an interrupted job goes back in the queue this many times, then waits for its sender
RECENT_JOBS = 8
LABEL_CHARS = 80
JOB_START_SECONDS = 5.0  # a just-started unit may still read as inactive or as its last failure
JOB_MIN_RAM_GIB = 4.0
# Above LOW_RAM_GIB on purpose: a job gives way first, so it never sends the model into its low-RAM backoff.
JOB_GUARD_RAM_GIB = 3.0
ANSWER_WAIT_SECONDS = 120.0  # a GPU job lets an answer in progress finish, for this long at most
QUESTION_QUIET_SECONDS = 20.0  # between a turn's model calls the model looks idle, but the student is still waiting
CLAIM_SECONDS = 60.0  # how long the model's memory may take to go; after that the job gives up, not the model
KEEP_OFF_SECONDS = 20 * 60
PRIORITY_SECONDS = 30 * 60
# A backstop, so a forgotten job cannot keep the model off for days. A sender may ask for up to a day.
DEFAULT_GPU_JOB_SECONDS = 4 * 3600
DEFAULT_CPU_JOB_SECONDS = 12 * 3600
LONGEST_JOB_SECONDS = 24 * 3600
LOG_IN_STATE = 8
RUNNING_STATES = ("active", "activating", "deactivating", "reloading")
ENDED_STATES = ("inactive", "failed")  # anything else, such as a systemctl hiccup, proves nothing
HOST_ERRORS = (OSError, subprocess.SubprocessError, ValueError, IndexError, KeyError)


@dataclass
class Job:
    id: str
    label: str
    gpu: bool
    next_gpu_likely: bool = False
    restart: bool = True  # False: rerunning it is not safe, so an interrupted job waits for its sender
    max_seconds: int = 0
    status: str = "queued"  # queued | running | done | failed | cancelled | interrupted
    note: str = ""
    restarts: int = 0
    submitted_at: float = 0.0
    started_at: float = 0.0
    finished_at: float = 0.0

    @property
    def unit(self) -> str:
        return f"offload-gpu-job@{self.id}.service" if self.gpu else f"offload-job@{self.id}.service"


class Aziz:
    def __init__(self, host: Host, state_file: Path, clock: Callable[[], float] = time.monotonic):
        self.host = host
        self.state_file = state_file
        self.clock = clock
        # Days are wall-clock days, whatever clock the rules run on.
        self.stats = DayStats(state_file.parent / "stats.json")
        self.up_since_tick: float | None = None
        self.ram_backoff_until = 0.0
        self.held_until = 0.0
        self.next_start = 0.0
        self.keep_off_until = 0.0
        self.priority_until = 0.0
        self.claim: str | None = None  # the GPU job the model is being stopped for
        self.stop_owed = False  # the image app asked and a GPU job's stop failed: it is owed even if the app goes silent
        self.claim_since = 0.0
        self.answer_wait_since: float | None = None
        self.last_question = -QUESTION_QUIET_SECONDS
        self.question_during_job = False
        self.waking_since: float | None = None
        self.journal = Journal(state_file.parent / "journal.jsonl")
        self._lock = threading.Lock()
        self._start_lock = threading.Lock()  # a preempt must never slip between "may it start?" and starting it
        self._jobs_lock = threading.Lock()  # data only, never held across a host call
        saved = state_file.read_text().strip() if state_file.exists() else ""
        saved = OLD_MODE_NAMES.get(saved, saved)
        self.mode = saved if saved in MODES else "assistant"
        self.jobs_file = state_file.parent / "jobs.json"
        self.started: dict[str, float] = {}
        self.jobs: list[Job] = self._load_jobs()
        self.journal.add("aziz", "Aziz göreve başladı")

    # The model

    def blocked_reason(self) -> str | None:
        """Why the model may not run now. Asked in the same order for starting and for staying on."""
        if self.mode == "compute":
            return "GPU Compute modunda"
        if self.clock() < self.held_until:
            return "görsel uygulaması GPU'yu istedi"
        if self.gpu_reserved():
            return "GPU hesaplama işine ayrıldı"
        image_app = self.host.image_app()
        # A stopped the image app leaves the GPU free, and its hook preempts when it starts again.
        if image_app.reachable and not image_app.hooked:
            return "görsel uygulamasında GPU kancası yok"
        if image_app.busy:
            return "görsel uygulaması çalışıyor"
        if self.host.foreign_gpu_mib() > FOREIGN_GPU_MIB:
            return "GPU'da başka bir süreç var"
        return None

    def room_problem(self) -> str | None:
        if self.host.available_ram_gib() < MIN_RAM_GIB:
            return "yeterli boş RAM yok"
        if self.host.vram_used_mib() > MAX_VRAM_BEFORE_START_MIB:
            return "GPU belleği dolu"
        return None

    def set_mode(self, mode: str) -> None:
        """Compute keeps the model off until the mode changes. Work keeps its place: only the model moves."""
        mode = OLD_MODE_NAMES.get(mode, mode)
        if mode not in MODES:
            raise ValueError(mode)
        with self._lock:
            changed = mode != self.mode
            self.mode = mode
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            self.state_file.write_text(mode)
        if changed:
            self.journal.add("mode", "GPU işlere ayrıldı: Compute" if mode == "compute" else "GPU Kamil'e döndü: AI-Asistan")
        if mode == "compute":
            self.priority_until = 0.0  # the student's latest word
            self.stop("Compute moduna alındı")

    def acquire(self) -> str | None:
        """Makes the model ready. Returns None when it is, otherwise the reason it cannot run."""
        self.keep_off_until = 0.0  # a question beats a guess about the next job
        self.last_question = self.clock()
        if self.gpu_reserved():
            self.question_during_job = True  # so the model comes straight back when the job ends
        with self._lock:
            reason = self.blocked_reason()
            if reason is None and not self.host.llm_active():
                reason = self.room_problem()
            if reason:
                return reason
            if not self.host.llm_active():
                self.host.start_llm()
                self.waking_since = self.clock()
                self.journal.add("model", "Kamil yükleniyor: soru geldi")
        # Waits without the lock, so a preempt never queues behind a model that is still loading.
        deadline = self.clock() + START_SECONDS
        while self.clock() < deadline:
            if self.clock() < self.held_until:
                return "görsel uygulaması GPU'yu istedi"
            if self.gpu_reserved():
                return "GPU hesaplama işine ayrıldı"
            if self.host.llm_ready():
                return None
            time.sleep(0.25)
        return "model zamanında açılmadı"

    def stop(self, why: str = "") -> None:
        if self.host.llm_active():
            self.host.stop_llm()
            self.waking_since = None
            self.journal.add("model", f"Kamil diske indi: {why}" if why else "Kamil diske indi")

    def preempt(self) -> bool:
        """Returns once the GPU is free, or when the wait runs out: the image app goes ahead either way, so nothing that
        goes wrong here may keep it waiting or leave the model running. True when the GPU is known to be free."""
        with self._start_lock:
            self.held_until = self.clock() + HOLD_SECONDS
        started = self.clock()
        deadline = started + FREE_SECONDS
        interrupted = False
        self.stop_owed = True
        try:
            interrupted = self._interrupt_gpu_jobs("görsel uygulaması GPU'yu istedi")
            self.stop_owed = any(job.gpu for job in self._with_status("running"))
        except Exception:  # noqa: BLE001 - the model must still leave
            log.exception("preempt: could not stop the GPU job")
        try:
            self.stop("görsel uygulaması GPU'yu istedi")
            while self.clock() < deadline:
                if self.host.llm_vram_mib() <= 0 and not (interrupted and self.host.foreign_gpu_mib() > FOREIGN_GPU_MIB):
                    self.journal.add("image_app", f"Görsel uygulaması GPU'yu istedi, {self.clock() - started:.0f} saniyede boşaldı")
                    return True
                time.sleep(0.1)
        except Exception:  # noqa: BLE001 - see above
            log.exception("preempt: could not free the GPU")
        self.journal.add("problem", "Görsel uygulaması GPU'yu istedi ama GPU zamanında boşalmadı")
        return False

    def tick(self) -> None:
        """One watch round. Job handling comes first and cannot stop the rest: the model's own rules always run."""
        try:
            self._manage_jobs()
        except Exception:  # noqa: BLE001 - whatever a job did, the model logic below still runs
            log.exception("job round failed")
            self.journal.add("problem", "İşleri yönetirken bir hata oldu, Kamil etkilenmedi")
        if not self.host.llm_active():
            self.waking_since = None
            self.up_since_tick = None
            self._bring_back()
            return
        now = self.clock()
        if self.up_since_tick is not None:
            self.stats.add_up(min(now - self.up_since_tick, MAX_COUNTED_TICK_SECONDS))
        self.up_since_tick = now
        if self.waking_since is not None and self.host.llm_ready():
            self.journal.add("model", f"Kamil hazır, {self.clock() - self.waking_since:.0f} saniyede yüklendi")
            self.waking_since = None
            self.stats.count("loads")
        reason = self.blocked_reason()
        if reason is None and self.host.available_ram_gib() < LOW_RAM_GIB:
            reason = "bellek azaldı"
            self.ram_backoff_until = self.clock() + RAM_BACKOFF_SECONDS
        if reason:
            self.stop(reason)

    def _bring_back(self) -> None:
        with self._lock:
            waiting = max(self.next_start, self.ram_backoff_until, self.keep_off_until)
            if self.clock() < waiting or self.blocked_reason() or self.room_problem():
                return
            self.next_start = self.clock() + START_RETRY_SECONDS
            self.host.start_llm()
            self.waking_since = self.clock()
            self.journal.add("model", "Kamil yükleniyor: GPU boşaldı")

    # Jobs

    def _boot_id(self) -> str:
        try:
            return self.host.boot_id()
        except HOST_ERRORS:
            return ""

    def _load_jobs(self) -> list[Job]:
        try:
            saved = json.loads(self.jobs_file.read_text())
            jobs = [Job(**entry) for entry in saved["jobs"]]
            boot = saved.get("boot", "")
        except (OSError, ValueError, TypeError, KeyError) as error:
            if self.jobs_file.exists():
                log.warning("could not read the saved jobs: %s", error)
            return []
        rebooted = bool(boot) and boot != self._boot_id()
        for job in jobs:
            if job.status != "running":
                continue
            if rebooted:  # its unit is gone, and "inactive" would read as a clean finish
                job.status, job.note, job.restarts = "queued", "sunucu yeniden başladı", job.restarts + 1
                self.journal.add("job", f"Sunucu yeniden başladı, iş baştan alınacak: {job.label or job.id}")
                if not job.restart or job.restarts > MAX_RESTARTS:
                    job.status, job.finished_at = "interrupted", time.time()
            else:
                self.started[job.id] = self.clock() - JOB_START_SECONDS
        return jobs

    def _save_jobs(self) -> None:
        """Atomic, and a full disk only costs the saved copy: it must never stop a preempt."""
        try:
            self.jobs_file.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.jobs_file.with_suffix(".new")
            temporary.write_text(json.dumps({"boot": self._boot_id(), "jobs": [asdict(job) for job in self.jobs]}, ensure_ascii=False))
            temporary.replace(self.jobs_file)
        except OSError as error:
            log.warning("could not save the jobs: %s", error)

    def _with_status(self, *statuses: str) -> list[Job]:
        with self._jobs_lock:
            return [job for job in self.jobs if job.status in statuses]

    def gpu_reserved(self) -> bool:
        return self.claim is not None or any(job.gpu for job in self._with_status("running"))

    def priority_active(self) -> bool:
        return self.clock() < self.priority_until

    def submit(
        self, job_id: str, label: str, gpu: bool, next_gpu_likely: bool, restart: bool = True, max_minutes: int | None = None,
    ) -> Job:
        # fullmatch: with match and $, a trailing newline would reach the unit name.
        if not isinstance(job_id, str) or not JOB_ID.fullmatch(job_id):
            raise ValueError("geçersiz iş adı")
        default = DEFAULT_GPU_JOB_SECONDS if gpu else DEFAULT_CPU_JOB_SECONDS
        seconds = default if max_minutes is None else int(max_minutes) * 60
        if not 0 < seconds <= LONGEST_JOB_SECONDS:
            raise ValueError("geçersiz iş süresi")
        clean = "".join(char for char in str(label) if char.isprintable())[:LABEL_CHARS]
        with self._jobs_lock:
            if any(job.id == job_id and job.status in ("queued", "running") for job in self.jobs):
                raise ValueError("bu adda bir iş zaten var")
            if sum(job.status in ("queued", "running") for job in self.jobs) >= MAX_JOBS:
                raise ValueError("kuyruk dolu")
            job = Job(job_id, clean, bool(gpu), bool(next_gpu_likely), bool(restart), seconds, submitted_at=time.time())
            self.jobs = [other for other in self.jobs if other.id != job_id] + [job]
            self._save_jobs()
        self.journal.add("job", f"Yeni iş geldi: {job.label or job.id} ({'GPU' if job.gpu else 'işlemci'})")
        return job

    def job(self, job_id: str) -> Job | None:
        with self._jobs_lock:
            return next((job for job in self.jobs if job.id == job_id), None)

    def cancel(self, job_id: str) -> bool:
        job = self.job(job_id)
        if job is None:
            return False
        with self._start_lock:  # not while it is being started
            if job.status not in ("queued", "running"):
                return False
            if job.status == "running":
                self.host.stop_job(job.unit)  # a failure raises: better a failed cancel than a false "cancelled"
            self._finish(job, "cancelled", "iptal edildi")
        self.journal.add("job", f"İş iptal edildi: {job.label or job.id}")
        self.started.pop(job.id, None)
        if self.claim == job_id:
            self.claim = None
        return True

    def forecast(self, likely: bool) -> None:
        """What the sender expects: is more GPU work likely in the next 15-20 minutes? It can end the wait but
        never extend it, so a chatty sender cannot keep the model off indefinitely."""
        with self._jobs_lock:
            for job in self.jobs:
                if job.gpu and job.status in ("queued", "running"):
                    job.next_gpu_likely = likely
            self._save_jobs()
        if not likely:
            self.keep_off_until = 0.0

    def set_priority(self, on: bool) -> None:
        """The student needs the model now: GPU jobs step aside for a while. The image app still comes first."""
        if not on:
            if self.priority_active():
                self.journal.add("priority", "Kamil'in önceliği bitirildi")
            self.priority_until = 0.0
            return
        if self.mode == "compute":
            raise ValueError("GPU Compute modunda, önce AI-Asistan'a al")
        self.priority_until = self.clock() + PRIORITY_SECONDS
        self.keep_off_until = self.ram_backoff_until = self.next_start = 0.0
        self.question_during_job = False
        self.journal.add("priority", f"Kamil {PRIORITY_SECONDS // 60} dakika öne alındı")
        self._interrupt_gpu_jobs("Kamil öne alındı")

    def _finish(self, job: Job, status: str, note: str = "") -> None:
        with self._jobs_lock:
            job.status, job.note, job.finished_at = status, note, time.time()
            ended = [other for other in self.jobs if other.status not in ("queued", "running")]
            for old in ended[:-RECENT_JOBS]:
                self.jobs.remove(old)
            self._save_jobs()
        if status == "done":
            self.stats.count("jobsDone")
        elif status == "failed":
            self.stats.count("jobsFailed")

    def _interrupt(self, job: Job, note: str) -> bool:
        """Back to the front of its queue once its unit stops. A job that cannot be rerun, or keeps being
        interrupted, waits for its sender instead. If the unit will not stop, the job is still running."""
        try:
            self.host.stop_job(job.unit)
        except HOST_ERRORS as error:
            log.warning("could not stop job %s: %s", job.id, error)
            return False
        self.started.pop(job.id, None)
        self.journal.add("job", f"İş durduruldu: {job.label or job.id}. Sebep: {note}")
        if not job.restart or job.restarts >= MAX_RESTARTS:
            self._finish(job, "interrupted", note)
            return True
        with self._jobs_lock:
            if job.status == "running":  # not one cancelled or reaped in the meantime
                job.status, job.note = "queued", note
                job.restarts += 1
                self.jobs.remove(job)
                self.jobs.insert(0, job)
                self._save_jobs()
        return True

    def _interrupt_gpu_jobs(self, note: str) -> bool:
        self.claim = None
        self.answer_wait_since = None
        running = [job for job in self._with_status("running") if job.gpu]
        for job in running:
            self._interrupt(job, note)
        return bool(running)

    def _reap(self) -> None:
        """A job's unit is the truth about it, so this also covers crashes and a restart of this service."""
        for job in self._with_status("running"):
            started = self.started.get(job.id)
            if started is None:
                started = self.started[job.id] = self.clock() - JOB_START_SECONDS
            age = self.clock() - started
            if age < JOB_START_SECONDS:
                continue
            state = self.host.job_state(job.unit)
            if state not in ENDED_STATES:
                if age > (job.max_seconds or DEFAULT_GPU_JOB_SECONDS):
                    self.host.stop_job(job.unit)
                    self._ended(job, "failed", "süresi doldu")
                continue
            self._ended(job, "failed" if state == "failed" else "done")

    def _ended(self, job: Job, status: str, note: str = "") -> None:
        ran = self.clock() - self.started.get(job.id, self.clock())
        self._finish(job, status, note)
        self.started.pop(job.id, None)
        name = job.label or job.id
        took = f"{ran / 60:.0f} dakika" if ran >= 60 else f"{ran:.0f} saniye"
        if status == "done":
            self.journal.add("job", f"İş bitti: {name} ({took})")
        else:
            self.journal.add("problem", f"İş hata verdi: {name}" + (f". {note}" if note else f" ({took})"))
        if not self._with_status("running"):
            self.ram_backoff_until = self.next_start = 0.0  # the low RAM was most likely the job's
        if job.gpu:
            if job.next_gpu_likely and status == "done" and not self.priority_active() and not self.question_during_job:
                self.keep_off_until = self.clock() + KEEP_OFF_SECONDS
                self.journal.add("model", f"Yeni GPU işi bekleniyor, Kamil en çok {KEEP_OFF_SECONDS // 60} dakika diskte kalacak")
            self.question_during_job = False

    def _guard_memory(self) -> None:
        running = self._with_status("running")
        if not running:
            return
        available = self.host.available_ram_gib()
        # Under priority a job also leaves when it is what keeps the stopped model from having room to start.
        needed_by_model = self.priority_active() and not self.host.llm_active() and available < MIN_RAM_GIB
        if available < JOB_GUARD_RAM_GIB or needed_by_model:
            newest_cpu_first = sorted(running, key=lambda job: (job.gpu, -job.started_at))
            self._interrupt(newest_cpu_first[0], "bellek azaldı")

    def _start(self, job: Job) -> bool:
        with self._start_lock:
            if job.gpu and self.clock() < self.held_until:
                return False
            if job.status != "queued":
                return False
            try:
                self.host.start_job(job.unit)
            except HOST_ERRORS as error:
                # A missing unit must not be retried forever with the model waiting.
                log.warning("could not start job %s: %s", job.id, error)
                self._finish(job, "failed", "başlatılamadı")
                self.journal.add("problem", f"İş başlatılamadı: {job.label or job.id}")
                return False
            self.started[job.id] = self.clock()
            with self._jobs_lock:
                job.status, job.note, job.started_at = "running", "", time.time()
                self._save_jobs()
        self.journal.add("job", f"İş başladı: {job.label or job.id} ({'GPU' if job.gpu else 'işlemci'})")
        return True

    def _note(self, job: Job, note: str) -> None:
        """Why a queued job is waiting, for whoever looks."""
        if job.note != note:
            with self._jobs_lock:
                job.note = note
                self._save_jobs()

    def _gpu_job_waits(self) -> str | None:
        if self.priority_active():
            return "Kamil öne alındı"
        if self.clock() < self.held_until:
            return "görsel uygulaması GPU'yu istedi"
        image_app = self.host.image_app()
        if image_app.reachable and not image_app.hooked:
            return "görsel uygulamasında GPU kancası yok"
        if image_app.busy:
            return "görsel uygulaması çalışıyor"
        if image_app.running and not image_app.reachable:
            return "görsel uygulaması yanıt vermiyor"
        if self.host.foreign_gpu_mib() > FOREIGN_GPU_MIB:
            return "GPU'da başka bir süreç var"
        if self.host.available_ram_gib() < JOB_MIN_RAM_GIB:
            return "bellek az"
        # Asked before the model is stopped, so a full GPU cannot make the model leave and return again and again.
        if self.host.vram_used_mib() - self.host.llm_vram_mib() > MAX_VRAM_BEFORE_START_MIB:
            return "GPU belleği dolu"
        return None

    def _next_gpu_job(self) -> None:
        job = next((job for job in self._with_status("queued") if job.gpu), None)
        if job is None or any(other.gpu for other in self._with_status("running")):
            self.claim = None
            return
        reason = self._gpu_job_waits()
        if reason:
            self.claim = None  # the image app or the student came first; the job stays queued
            self.answer_wait_since = None
            self._note(job, reason)
            return
        if self.host.job_state(job.unit) in RUNNING_STATES:
            return  # the last run is still stopping
        if self.claim != job.id:
            # Before the model leaves: a job that cannot run must not cost the student a cold start.
            if not self.host.job_known(job.unit):
                self._finish(job, "failed", "başlatılamadı")
                self.journal.add("problem", f"İş başlatılamadı, servisi yok: {job.label or job.id}")
                return
            if self.host.llm_active():
                # The model keeps answering during this wait: no claim yet.
                if self.answer_wait_since is None:
                    self.answer_wait_since = self.clock()
                in_use = self.host.llm_busy() or self.clock() - self.last_question < QUESTION_QUIET_SECONDS
                if in_use and self.clock() - self.answer_wait_since < ANSWER_WAIT_SECONDS:
                    self._note(job, "Kamil'in cevabı bitiyor")
                    return
            self.answer_wait_since = None
            self.claim, self.claim_since = job.id, self.clock()
            self.keep_off_until = 0.0
        if self.host.llm_active():
            self.stop(f"GPU işi için ({job.label or job.id})")
        if self.host.llm_vram_mib() > 0:
            if self.clock() - self.claim_since > CLAIM_SECONDS:
                self.claim = None
                self._finish(job, "failed", "GPU boşalmadı")
                self.journal.add("problem", f"Kamil'in belleği boşalmadı, iş iptal: {job.label or job.id}")
            return  # still freeing; the claim keeps the model off until the next round
        self._start(job)
        self.claim = None

    def _manage_jobs(self) -> None:
        self._reap()
        if not self._with_status("queued", "running"):
            self.claim = None
            return
        # Every round, not once: a job whose stop failed is stopped again until it goes.
        if self.host.image_app().busy or self.clock() < self.held_until or self.stop_owed:
            self._interrupt_gpu_jobs("görsel uygulaması GPU'yu istedi")
            self.stop_owed = any(job.gpu for job in self._with_status("running"))
        elif self.priority_active():
            self._interrupt_gpu_jobs("Kamil öne alındı")
        self._guard_memory()
        for job in [job for job in self._with_status("queued") if not job.gpu]:
            if self.priority_active():
                self._note(job, "Kamil öne alındı")
                continue
            if sum(not other.gpu for other in self._with_status("running")) >= MAX_CPU_JOBS:
                break
            if self.host.available_ram_gib() < JOB_MIN_RAM_GIB:
                self._note(job, "bellek az")
                break
            if self.host.job_state(job.unit) not in RUNNING_STATES:
                self._start(job)
        self._next_gpu_job()

    def state(self) -> dict:
        image_app = self.host.image_app()
        active = self.host.llm_active()
        reason = self.blocked_reason()
        if not reason and not active:
            if self.clock() < self.keep_off_until:
                reason = "Sıradaki GPU işi bekleniyor, soru sorarsan Kamil hemen açılır"
            else:
                reason = self.room_problem()
        reserved = self.gpu_reserved()
        try:
            system = self.host.system()
        except HOST_ERRORS:
            system = {}
        with self._jobs_lock:
            jobs = [
                {"id": job.id, "label": job.label, "gpu": job.gpu, "status": job.status, "note": job.note,
                 "startedAt": job.started_at or None, "finishedAt": job.finished_at or None}
                for job in self.jobs
            ]
        return {
            "mode": self.mode,
            "llmActive": active,
            "blocked": reason,
            "imageAppReachable": image_app.reachable,
            "gateMissing": image_app.reachable and not image_app.hooked,
            "gpuOwner": "image" if image_app.busy else "job" if reserved else "kamil" if active else "free",
            "prioritySeconds": max(0, round(self.priority_until - self.clock())),
            "keepOffSeconds": max(0, round(self.keep_off_until - self.clock())) if not active else 0,
            "jobs": jobs,
            "system": system,
            "log": self.journal.recent(LOG_IN_STATE),
            "kamilToday": self.stats.snapshot(),
            "imageAppName": IMAGE_APP_NAME or None,
        }
