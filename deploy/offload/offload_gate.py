#!/usr/bin/python3
"""The only thing the Mac's offload key can run: a short list of operations, each checked here.

Installed as /usr/local/libexec/offload-gate and forced by authorized_keys, so this file is the security boundary.
Names and paths from the other side are validated, a project can only touch its own folder, and jobs go through Aziz.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import sys
import tarfile
import time
import urllib.error
import urllib.request
from pathlib import Path, PurePosixPath

ROOT = Path(os.environ.get("OFFLOAD_ROOT", "/var/lib/offload"))
AZIZ = os.environ.get("AZIZ_URL", "http://127.0.0.1:8421")
JOB_ID = re.compile(r"[a-z0-9][a-z0-9-]{0,62}")
PROJECT = re.compile(r"[a-z0-9][a-z0-9_-]{0,62}")
MIN_FREE_BYTES = 10 * 2**30  # the machine's own work comes first
MAX_LOG_LINES = 2000
MAX_LOG_BYTES = 256 * 1024
OLD_JOB_SECONDS = 14 * 86400
MANIFEST = ".offload-manifest"
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class Refused(Exception):
    pass


def job_id(value: str) -> str:
    if not JOB_ID.fullmatch(value):
        raise Refused("bad job name")
    return value


def project_dir(value: str, create: bool = False) -> Path:
    if not PROJECT.fullmatch(value):
        raise Refused("bad project name")
    path = ROOT / "jobs" / value
    if create:
        path.mkdir(parents=True, exist_ok=True)
    elif not path.is_dir():
        raise Refused("unknown project")
    return path


def inside(base: Path, relative: str) -> Path:
    """A path in a project: no absolute paths, no way out, also through a symlink a job left behind."""
    if not relative or "\0" in relative:
        raise Refused("bad path")
    pure = PurePosixPath(relative)
    if pure.is_absolute() or ".." in pure.parts or pure.parts[:1] == (".git",):
        raise Refused("bad path")
    path = base / pure
    if not path.parent.resolve().is_relative_to(base.resolve()):
        raise Refused("bad path")
    return path


def aziz(method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(AZIZ + path, data=data, method=method)
    try:
        with _opener.open(request, timeout=20) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as error:
        try:
            return error.code, json.loads(error.read())
        except ValueError:
            return error.code, {}
    except (OSError, ValueError) as error:
        raise Refused(f"cannot reach Aziz: {error}") from error


def plan(project: str, stdin) -> dict:
    """Takes what the Mac has; removes what it no longer has, answers what it must send. Caches are never touched."""
    base = project_dir(project, create=True)
    if shutil.disk_usage(base).free < MIN_FREE_BYTES:
        raise Refused("the server is low on disk")
    files = json.load(stdin)
    if not isinstance(files, list):
        raise Refused("bad list")
    wanted, need = set(), []
    for entry in files:
        path = inside(base, entry["p"])
        wanted.add(entry["p"])
        try:
            stat = path.lstat()
            same = stat.st_size == entry["s"] and int(stat.st_mtime) == int(entry["m"])
        except OSError:
            same = False
        if not same:
            need.append(entry["p"])
    manifest = base / MANIFEST
    removed = 0
    try:
        old = json.loads(manifest.read_text())
    except (OSError, ValueError):
        old = []
    for relative in old:
        if relative not in wanted:
            try:
                inside(base, relative).unlink()
                removed += 1
            except (OSError, Refused):
                pass
    manifest.write_text(json.dumps(sorted(wanted)))
    return {"need": need, "removed": removed}


def put(project: str, stdin) -> dict:
    base = project_dir(project, create=True)
    count = 0
    with tarfile.open(fileobj=stdin, mode="r|gz") as tar:
        for member in tar:
            inside(base, member.name)
            try:
                tar.extract(member, base, filter="data")
            except tarfile.FilterError as error:
                raise Refused(f"refused file: {member.name}") from error
            count += 1
    return {"received": count}


def submit(args: list[str], stdin) -> dict:
    """submit ID PROJECT cpu|gpu likely|unlikely restart|norestart MINUTES LABEL..., with the command on stdin."""
    if len(args) < 6:
        raise Refused("bad submit")
    identity, project, kind, next_gpu, restart, minutes = args[:6]
    job_id(identity)
    project_dir(project)
    if kind not in ("cpu", "gpu") or next_gpu not in ("likely", "unlikely") or restart not in ("restart", "norestart"):
        raise Refused("bad submit")
    command = stdin.read(MAX_LOG_BYTES)
    if not command.strip():
        raise Refused("no command")
    _prune_old_jobs()
    queue = ROOT / "queue" / identity
    active, _ = aziz("GET", f"/jobs/{identity}")
    if active == 200 and aziz("GET", f"/jobs/{identity}")[1].get("status") in ("queued", "running"):
        raise Refused("a job with this name is still active")
    queue.mkdir(parents=True, exist_ok=True)
    (queue / "run.sh").write_bytes(command)
    (queue / "project").write_text(project)
    (queue / "log").write_text("")
    body = {
        "id": identity, "label": " ".join(args[6:]), "gpu": kind == "gpu", "nextGpu": next_gpu,
        "restart": restart == "restart",
    }
    if minutes != "0":
        body["maxMinutes"] = int(minutes) if minutes.isdigit() else 0
    status, answer = aziz("POST", "/jobs", body)
    if status != 200:
        shutil.rmtree(queue, ignore_errors=True)
        raise Refused(answer.get("reason") or "Aziz refused the job")
    return answer


def _prune_old_jobs() -> None:
    queue = ROOT / "queue"
    if not queue.is_dir():
        return
    for path in queue.iterdir():
        try:
            if time.time() - path.stat().st_mtime > OLD_JOB_SECONDS:
                if aziz("GET", f"/jobs/{path.name}")[1].get("status") not in ("queued", "running"):
                    shutil.rmtree(path, ignore_errors=True)
        except (OSError, Refused):
            pass


def logs(identity: str, lines: str) -> dict:
    path = ROOT / "queue" / job_id(identity) / "log"
    wanted = min(int(lines) if lines.isdigit() else 200, MAX_LOG_LINES)
    try:
        with path.open("rb") as file:
            file.seek(0, os.SEEK_END)
            file.seek(max(0, file.tell() - MAX_LOG_BYTES))
            text = file.read().decode("utf-8", "replace")
    except OSError as error:
        raise Refused("no log for this job") from error
    return {"log": "\n".join(text.splitlines()[-wanted:])}


def pull(project: str, relative: str, stdout) -> None:
    base = project_dir(project)
    path = inside(base, relative)
    if not path.exists() or not path.resolve().is_relative_to(base.resolve()):
        raise Refused("no such path")
    with tarfile.open(fileobj=stdout, mode="w|gz") as tar:
        tar.add(path, arcname=relative, filter=lambda member: None if member.issym() or member.islnk() else member)


def state() -> dict:
    status, answer = aziz("GET", "/state")
    if status != 200:
        raise Refused("Aziz did not answer")
    keep = ("mode", "llmActive", "blocked", "gpuOwner", "prioritySeconds", "keepOffSeconds", "jobs", "system")
    return {key: answer.get(key) for key in keep}


def run(command: list[str], stdin, stdout) -> dict | None:
    if not command:
        raise Refused("no operation")
    op, args = command[0], command[1:]
    if op == "plan" and len(args) == 1:
        return plan(args[0], stdin)
    if op == "put" and len(args) == 1:
        return put(args[0], stdin)
    if op == "submit":
        return submit(args, stdin)
    if op == "job" and len(args) == 1:
        status, answer = aziz("GET", f"/jobs/{job_id(args[0])}")
        if status != 200:
            raise Refused("no such job")
        return answer
    if op == "logs" and len(args) in (1, 2):
        return logs(args[0], args[1] if len(args) == 2 else "200")
    if op == "cancel" and len(args) == 1:
        status, answer = aziz("POST", f"/jobs/{job_id(args[0])}/cancel", {})
        if status != 200:
            raise Refused(answer.get("reason") or "not an active job")
        return answer
    if op == "forecast" and len(args) == 1 and args[0] in ("likely", "unlikely"):
        return aziz("POST", "/forecast", {"nextGpu": args[0]})[1]
    if op == "state" and not args:
        return state()
    if op == "pull" and len(args) == 2:
        pull(args[0], args[1], stdout)
        return None
    if op == "forget" and len(args) == 1:
        shutil.rmtree(project_dir(args[0]), ignore_errors=True)
        return {"forgotten": True}
    raise Refused("unknown operation")


def main() -> int:
    try:
        command = shlex.split(os.environ.get("SSH_ORIGINAL_COMMAND", ""))
        result = run(command, sys.stdin.buffer, sys.stdout.buffer)
        if result is not None:
            sys.stdout.write(json.dumps(result, ensure_ascii=False) + "\n")
        return 0
    except (Refused, ValueError, KeyError, TypeError, tarfile.TarError) as error:
        sys.stderr.write(f"offload: {error}\n")
        return 1


if __name__ == "__main__":
    sys.exit(main())
