#!/usr/bin/env python3
"""Sends heavy work from this Mac to the server. Used by the offload skill and the MCP server, or by hand.

Nothing here decides when a job runs: Aziz does, on the server. This syncs a project, says what it needs, and reads back.
"""
from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
import re
import subprocess
import sys
import tarfile
import time
from pathlib import Path

CONFIG = Path.home() / ".claude" / "offload" / "config.json"
MARKER = Path(".claude") / "offload-enabled"
NOTE_START = "<!-- offload: on -->"
NOTE_END = "<!-- offload: end -->"
NOTE = f"""{NOTE_START}
Heavy work in this project goes to the home server: use the offload tools (see the offload skill), not this Mac.
{NOTE_END}
"""
# Never leaves this Mac, even if git tracks it.
SECRET_NAMES = (
    ".env", ".env.*", "*.pem", "*.key", "*.p12", "*.pfx", "*.jks", "*.keystore", "*.cred", "*.gpg", "id_rsa*", "id_ed25519*",
    "id_ecdsa*", "client_secret*", "credentials*", "service-account*.json", "*password*", "*secret*", ".netrc", ".npmrc",
    ".pypirc", ".git-credentials", "*token*.txt", "*.tfvars",
)
SECRET_ALLOWED = (".env.example", ".env.sample", ".env.template")
SKIPPED_DIRS = {".claude", ".local", ".ssh", ".aws", ".gnupg", ".git"}
MAX_PROJECT_BYTES = 2 * 2**30


class OffloadError(Exception):
    pass


def config() -> dict:
    try:
        return json.loads(CONFIG.read_text())
    except (OSError, ValueError) as error:
        raise OffloadError(f"not set up: {CONFIG} is missing. Run the client's install.sh.") from error


def ssh(command: str, data: bytes | None = None, capture: bool = True) -> bytes:
    settings = config()
    arguments = [
        "ssh", "-i", os.path.expanduser(settings["key"]), "-o", "IdentitiesOnly=yes", "-o", "BatchMode=yes",
        "-o", "ConnectTimeout=15", settings["host"], command,
    ]
    try:
        done = subprocess.run(arguments, input=data, capture_output=capture, timeout=3600)
    except subprocess.TimeoutExpired as error:
        raise OffloadError("the server took too long") from error
    if done.returncode != 0:
        message = (done.stderr or b"").decode("utf-8", "replace").strip()
        raise OffloadError(message or "could not reach the server. Is Tailscale on?")
    return done.stdout or b""


def call(command: str, data: bytes | None = None) -> dict:
    return json.loads(ssh(command, data))


def git_root(directory: str | None = None) -> Path:
    where = directory or os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
    done = subprocess.run(["git", "-C", where, "rev-parse", "--show-toplevel"], capture_output=True, text=True)
    if done.returncode != 0:
        raise OffloadError(f"{where} is not in a git repository. Offload needs one, to know what to copy (git init).")
    return Path(done.stdout.strip())


def enabled(root: Path) -> bool:
    return (root / MARKER).exists()


def require_enabled(root: Path) -> None:
    if not enabled(root):
        raise OffloadError(f"offload is off in {root.name}. Turn it on with: /offload on")


def project_name(root: Path) -> str:
    slug = re.sub(r"[^a-z0-9_-]+", "-", root.name.lower()).strip("-_")[:40] or "project"
    return f"{slug}-{hashlib.sha1(str(root).encode()).hexdigest()[:6]}"


def _exclude(root: Path, line: str) -> None:
    path = root / ".git" / "info" / "exclude"
    path.parent.mkdir(parents=True, exist_ok=True)
    text = path.read_text() if path.exists() else ""
    if line not in text.splitlines():
        path.write_text(text.rstrip("\n") + ("\n" if text else "") + line + "\n")


def turn_on(root: Path) -> str:
    (root / MARKER).parent.mkdir(parents=True, exist_ok=True)
    (root / MARKER).write_text("")
    _exclude(root, str(MARKER))
    _exclude(root, "CLAUDE.local.md")
    note = root / "CLAUDE.local.md"
    text = note.read_text() if note.exists() else ""
    if NOTE_START not in text:
        note.write_text(text.rstrip("\n") + ("\n\n" if text else "") + NOTE)
    return f"offload is on in {root.name}. New sessions here will use it."


def turn_off(root: Path) -> str:
    (root / MARKER).unlink(missing_ok=True)
    note = root / "CLAUDE.local.md"
    if note.exists():
        text = re.sub(re.escape(NOTE_START) + r".*?" + re.escape(NOTE_END) + r"\n?", "", note.read_text(), flags=re.S).strip()
        note.write_text(text + "\n") if text else note.unlink()
    return f"offload is off in {root.name}."


def is_secret(relative: str) -> bool:
    parts = relative.split("/")
    if any(part in SKIPPED_DIRS for part in parts[:-1]):
        return True
    name = parts[-1].lower()
    if name in SECRET_ALLOWED:
        return False
    return any(fnmatch.fnmatch(name, pattern) for pattern in SECRET_NAMES)


def project_files(root: Path) -> tuple[list[dict], list[str]]:
    listed = subprocess.run(
        ["git", "-C", str(root), "ls-files", "-co", "--exclude-standard", "-z"], capture_output=True, check=True,
    ).stdout.decode("utf-8", "surrogateescape").split("\0")
    files, secrets = [], []
    for relative in sorted(set(filter(None, listed))):
        if is_secret(relative):
            secrets.append(relative)
            continue
        path = root / relative
        try:
            stat = path.lstat()
        except OSError:
            continue  # deleted but still tracked
        # A link stays only if it points inside the project.
        if path.is_symlink():
            target = os.path.normpath(os.path.join(os.path.dirname(relative), os.readlink(path)))
            if os.path.isabs(os.readlink(path)) or target.startswith(".."):
                continue
        elif not path.is_file():
            continue
        files.append({"p": relative, "s": stat.st_size, "m": int(stat.st_mtime)})
    return files, secrets


def sync(root: Path) -> dict:
    files, secrets = project_files(root)
    total = sum(entry["s"] for entry in files)
    if total > MAX_PROJECT_BYTES:
        raise OffloadError(f"{total / 2**30:.1f} GB to copy. Is something missing from .gitignore?")
    name = project_name(root)
    answer = call(f"plan {name}", json.dumps(files).encode())
    need = answer["need"]
    if need:
        settings = config()
        process = subprocess.Popen(
            ["ssh", "-i", os.path.expanduser(settings["key"]), "-o", "IdentitiesOnly=yes", "-o", "BatchMode=yes",
             settings["host"], f"put {name}"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        with tarfile.open(fileobj=process.stdin, mode="w|gz") as tar:
            for relative in need:
                tar.add(root / relative, arcname=relative, recursive=False)
        output, errors = process.communicate()
        if process.returncode != 0:
            raise OffloadError(errors.decode("utf-8", "replace").strip() or "could not send the files")
    return {"project": name, "files": len(files), "sent": len(need), "removed": answer["removed"], "secretsKept": len(secrets)}


def run_job(root: Path, command: str, gpu: bool = False, next_gpu: str = "unlikely", restart: bool = True,
            max_minutes: int = 0, label: str = "") -> dict:
    require_enabled(root)
    if next_gpu not in ("likely", "unlikely"):
        raise OffloadError("next_gpu must be likely or unlikely")
    synced = sync(root)
    # Names sort by time, stay short, and are safe in a unit name.
    job = f"{synced['project'][:40]}-{int(time.time() * 10) % 36**7:x}".strip("-")
    words = re.sub(r"\s+", " ", label or command).strip()[:60]
    quoted = " ".join(json.dumps(word) for word in words.split(" "))
    kind = "gpu" if gpu else "cpu"
    submitted = call(
        f"submit {job} {synced['project']} {kind} {next_gpu} {'restart' if restart else 'norestart'} {int(max_minutes)} {quoted}",
        command.encode(),
    )
    return {"job": job, "status": submitted.get("status"), "gpu": gpu, "sync": synced}


def wait_job(job: str, timeout: float = 600.0) -> dict:
    deadline = time.time() + timeout
    while True:
        status = call(f"job {job}")
        if status["status"] not in ("queued", "running") or time.time() > deadline:
            return status
        time.sleep(5)


def pull(root: Path, relative: str) -> str:
    require_enabled(root)
    if relative.startswith("/") or ".." in relative.split("/"):
        raise OffloadError("the path must be inside the project")
    settings = config()
    remote = subprocess.Popen(
        ["ssh", "-i", os.path.expanduser(settings["key"]), "-o", "IdentitiesOnly=yes", "-o", "BatchMode=yes",
         settings["host"], f"pull {project_name(root)} {json.dumps(relative)}"], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    count = 0
    try:
        with tarfile.open(fileobj=remote.stdout, mode="r|gz") as tar:
            for member in tar:
                if sys.version_info >= (3, 12):
                    tar.extract(member, root, filter="data")
                else:
                    tar.extract(member, root)
                count += 1
    except tarfile.ReadError as error:
        remote.wait()
        raise OffloadError((remote.stderr.read() or b"").decode("utf-8", "replace").strip() or "nothing came back") from error
    remote.wait()
    return f"pulled {count} entries into {root.name}/{relative}"


def main() -> int:
    parser = argparse.ArgumentParser(prog="offload", description="Send heavy work to the server")
    parser.add_argument("-C", dest="directory", help="the project's folder")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("on")
    commands.add_parser("off")
    commands.add_parser("status")
    commands.add_parser("files", help="what would be copied, and which secrets stay here")
    run = commands.add_parser("run")
    run.add_argument("--gpu", action="store_true")
    run.add_argument("--next-gpu", choices=("likely", "unlikely"), default="unlikely")
    run.add_argument("--no-restart", action="store_true")
    run.add_argument("--max-minutes", type=int, default=0)
    run.add_argument("--label", default="")
    run.add_argument("--wait", action="store_true")
    run.add_argument("job_command", nargs=argparse.REMAINDER)
    for name in ("job", "cancel"):
        commands.add_parser(name).add_argument("job")
    logs = commands.add_parser("logs")
    logs.add_argument("job")
    logs.add_argument("-n", type=int, default=200)
    commands.add_parser("forecast").add_argument("next_gpu", choices=("likely", "unlikely"))
    commands.add_parser("pull").add_argument("path")
    commands.add_parser("server")
    arguments = parser.parse_args()
    try:
        root = None if arguments.command in ("job", "cancel", "logs", "forecast", "server") else git_root(arguments.directory)
        if arguments.command == "on":
            print(turn_on(root))
        elif arguments.command == "off":
            print(turn_off(root))
        elif arguments.command == "status":
            print(json.dumps({"project": root.name, "enabled": enabled(root)}))
        elif arguments.command == "files":
            files, secrets = project_files(root)
            print(f"{len(files)} files, {sum(f['s'] for f in files) / 2**20:.1f} MB would be copied.")
            print(f"{len(secrets)} kept on this Mac: " + (", ".join(secrets[:30]) or "none"))
        elif arguments.command == "run":
            command = " ".join(arguments.job_command).strip()
            if not command:
                raise OffloadError("what should run? offload run -- make test")
            command = re.sub(r"^--\s*", "", command)
            job = run_job(root, command, arguments.gpu, arguments.next_gpu, not arguments.no_restart, arguments.max_minutes, arguments.label)
            print(json.dumps(job))
            if arguments.wait:
                print(json.dumps(wait_job(job["job"])))
        elif arguments.command == "job":
            print(json.dumps(call(f"job {arguments.job}"), ensure_ascii=False))
        elif arguments.command == "cancel":
            print(json.dumps(call(f"cancel {arguments.job}"), ensure_ascii=False))
        elif arguments.command == "logs":
            print(call(f"logs {arguments.job} {arguments.n}")["log"])
        elif arguments.command == "forecast":
            print(json.dumps(call(f"forecast {arguments.next_gpu}")))
        elif arguments.command == "pull":
            print(pull(root, arguments.path))
        elif arguments.command == "server":
            print(json.dumps(call("state"), ensure_ascii=False))
        return 0
    except OffloadError as error:
        print(f"offload: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
