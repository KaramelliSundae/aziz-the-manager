"""The gate is the security boundary: what the Mac's key can do, and nothing more."""
from __future__ import annotations

import io
import json
import sys
import tarfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "deploy" / "offload"))
import offload_gate as gate  # noqa: E402


@pytest.fixture(autouse=True)
def root(tmp_path, monkeypatch):
    monkeypatch.setattr(gate, "ROOT", tmp_path)
    monkeypatch.setattr(gate, "aziz", lambda method, path, body=None: (404, {}))
    return tmp_path


def run(*command, stdin=b""):
    return gate.run(list(command), io.BytesIO(stdin), io.BytesIO())


def tar_of(*members):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w|gz") as tar:
        for member, data in members:
            tar.addfile(member, io.BytesIO(data) if data is not None else None)
    return buffer.getvalue()


def member(name, data=b"x", **fields):
    info = tarfile.TarInfo(name)
    info.size = len(data)
    for key, value in fields.items():
        setattr(info, key, value)
    return info, data


@pytest.mark.parametrize("command", [
    ["bash"], ["sh", "-c", "id"], ["cat", "/etc/passwd"], ["rm", "-rf", "/"], [], ["plan"], ["mode", "image"], ["priority", "on"],
    ["preempt"], ["acquire"], ["forecast", "maybe"], ["job", "a", "b"],
])
def test_anything_else_is_refused(command):
    with pytest.raises(gate.Refused):
        run(*command)


@pytest.mark.parametrize("name", ["../etc", "a/b", "..", "A", "", "a b", "a\n", "-rf", ".hidden", "x" * 64])
def test_bad_project_names(name):
    with pytest.raises(gate.Refused):
        run("plan", name, stdin=b"[]")


@pytest.mark.parametrize("name", ["../x", "a/b", "A", "a\n", "-x", "x" * 64, "a.service"])
def test_bad_job_names(name):
    with pytest.raises(gate.Refused):
        run("job", name)
    with pytest.raises(gate.Refused):
        run("logs", name)
    with pytest.raises(gate.Refused):
        run("cancel", name)


@pytest.mark.parametrize("path", ["../../etc/passwd", "/etc/passwd", "a/../../b", "", ".git/config"])
def test_a_list_cannot_point_outside(path):
    with pytest.raises(gate.Refused):
        run("plan", "proj", stdin=json.dumps([{"p": path, "s": 1, "m": 1}]).encode())


@pytest.mark.parametrize("bad", [
    member("../evil"), member("/etc/evil"), member("a/../../evil"),
    member("link", b"", type=tarfile.SYMTYPE, linkname="/etc/passwd"),
    member("link", b"", type=tarfile.SYMTYPE, linkname="../../outside"),
    member("hard", b"", type=tarfile.LNKTYPE, linkname="/etc/passwd"),
    member("device", b"", type=tarfile.CHRTYPE),
    member("fifo", b"", type=tarfile.FIFOTYPE),
])
def test_a_hostile_archive_is_refused(bad, root):
    with pytest.raises((gate.Refused, tarfile.TarError)):
        run("put", "proj", stdin=tar_of(bad))
    assert not (root / "evil").exists() and not (root.parent / "evil").exists()


def test_a_setuid_bit_does_not_survive(root):
    run("put", "proj", stdin=tar_of(member("tool", b"#!", mode=0o4755)))
    assert (root / "jobs" / "proj" / "tool").stat().st_mode & 0o7000 == 0


def test_sync_sends_only_what_changed_and_keeps_caches(root):
    assert run("plan", "proj", stdin=json.dumps([{"p": "a.txt", "s": 1, "m": 100}]).encode())["need"] == ["a.txt"]
    run("put", "proj", stdin=tar_of(member("a.txt", mtime=100)))
    assert run("plan", "proj", stdin=json.dumps([{"p": "a.txt", "s": 1, "m": 100}]).encode())["need"] == []
    cache = root / "jobs" / "proj" / "node_modules" / "x"
    cache.parent.mkdir()
    cache.write_text("kept")
    answer = run("plan", "proj", stdin=b"[]")
    assert answer["removed"] == 1 and not (root / "jobs" / "proj" / "a.txt").exists() and cache.read_text() == "kept"


def test_a_damaged_manifest_cannot_delete_outside(root):
    outside = root / "precious"
    outside.write_text("keep")
    base = root / "jobs" / "proj"
    base.mkdir(parents=True)
    (base / gate.MANIFEST).write_text(json.dumps(["../../precious", "/etc/hostname"]))
    run("plan", "proj", stdin=b"[]")
    assert outside.read_text() == "keep"


def test_a_symlink_a_job_left_cannot_lead_outside(root):
    outside = root / "secret"
    outside.mkdir()
    (outside / "f").write_text("s")
    base = root / "jobs" / "proj"
    base.mkdir(parents=True)
    (base / "out").symlink_to(outside)
    with pytest.raises(gate.Refused):
        run("plan", "proj", stdin=json.dumps([{"p": "out/f", "s": 1, "m": 1}]).encode())
    with pytest.raises(gate.Refused):
        run("pull", "proj", "out/f")
    with pytest.raises(gate.Refused):
        run("pull", "proj", "out")


def test_pull_leaves_symlinks_out(root):
    base = root / "jobs" / "proj"
    (base / "dir").mkdir(parents=True)
    (base / "dir" / "file").write_text("ok")
    (base / "dir" / "link").symlink_to("/etc/passwd")
    out = io.BytesIO()
    gate.run(["pull", "proj", "dir"], io.BytesIO(), out)
    out.seek(0)
    with tarfile.open(fileobj=out, mode="r|gz") as tar:
        assert sorted(entry.name for entry in tar) == ["dir", "dir/file"]


def test_submit_asks_aziz_and_cleans_up_if_refused(root, monkeypatch):
    run("plan", "proj", stdin=b"[]")
    calls = []

    def accepting(method, path, body=None):
        calls.append((method, path, body))
        return (200, {"id": "j1", "status": "queued"}) if method == "POST" else (404, {})

    monkeypatch.setattr(gate, "aziz", accepting)
    answer = run("submit", "j1", "proj", "gpu", "likely", "norestart", "30", "my", "job", stdin=b"make test")
    assert answer["status"] == "queued"
    posted = calls[-1][2]
    assert posted["gpu"] is True and posted["nextGpu"] == "likely" and posted["restart"] is False
    assert posted["maxMinutes"] == 30 and posted["label"] == "my job"
    assert (root / "queue" / "j1" / "run.sh").read_bytes() == b"make test"
    monkeypatch.setattr(gate, "aziz", lambda method, path, body=None: (409, {"reason": "kuyruk dolu"}) if method == "POST" else (404, {}))
    with pytest.raises(gate.Refused, match="kuyruk"):
        run("submit", "j2", "proj", "cpu", "unlikely", "restart", "0", stdin=b"x")
    assert not (root / "queue" / "j2").exists()


@pytest.mark.parametrize("args", [
    ("j", "nope", "cpu", "unlikely", "restart", "0"), ("j", "proj", "tpu", "unlikely", "restart", "0"),
    ("j", "proj", "cpu", "perhaps", "restart", "0"), ("j", "proj", "cpu", "unlikely", "maybe", "0"), ("j", "proj"),
])
def test_bad_submits(args):
    run("plan", "proj", stdin=b"[]")
    with pytest.raises(gate.Refused):
        run("submit", *args, stdin=b"x")


def test_an_empty_command_is_refused():
    run("plan", "proj", stdin=b"[]")
    with pytest.raises(gate.Refused):
        run("submit", "j", "proj", "cpu", "unlikely", "restart", "0", stdin=b"   ")


def test_a_running_job_name_cannot_be_overwritten(root, monkeypatch):
    run("plan", "proj", stdin=b"[]")
    monkeypatch.setattr(gate, "aziz", lambda method, path, body=None: (200, {"status": "running"}))
    with pytest.raises(gate.Refused, match="active"):
        run("submit", "j", "proj", "cpu", "unlikely", "restart", "0", stdin=b"x")


def test_logs_have_a_limit(root):
    queue = root / "queue" / "j"
    queue.mkdir(parents=True)
    (queue / "log").write_text("line\n" * 200000)
    assert len(run("logs", "j", "99999")["log"].splitlines()) == gate.MAX_LOG_LINES


def test_low_disk_refuses_a_sync(monkeypatch):
    monkeypatch.setattr(gate.shutil, "disk_usage", lambda path: type("D", (), {"free": 1})())
    with pytest.raises(gate.Refused, match="disk"):
        run("plan", "proj", stdin=b"[]")


def test_state_gives_only_what_is_needed(monkeypatch):
    monkeypatch.setattr(gate, "aziz", lambda method, path, body=None: (200, {"mode": "auto", "imageAppReachable": True, "gateMissing": False, "log": []}))
    assert set(run("state")) >= {"mode"} and "log" not in run("state") and "imageAppReachable" not in run("state")
