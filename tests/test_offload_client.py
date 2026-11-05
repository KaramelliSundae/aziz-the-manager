"""The Mac side, with the server replaced: what gets copied, and the per-project switch."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "client" / "scripts"))
import offload  # noqa: E402
import offload_mcp  # noqa: E402


def git(root, *args):
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "My Project!"
    root.mkdir()
    git(root, "init", "-q")
    (root / ".gitignore").write_text("build/\n")
    (root / "main.py").write_text("print(1)")
    (root / "build").mkdir()
    (root / "build" / "out").write_text("x")
    return root


@pytest.mark.parametrize("name", [
    ".env", ".env.local", "server.key", "cert.PEM", "app.p12", "release.jks", "id_rsa", "id_ed25519.pub", "client_secret_123.json",
    "credentials.json", "my-password.txt", "app_secrets.yaml", "service-account-x.json", ".npmrc", "api_token.txt", "deep/dir/.env",
    ".local/note.txt", ".ssh/config", ".claude/settings.json",
])
def test_secrets_stay_here(repo, name):
    path = repo / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("secret")
    git(repo, "add", "-f", name)  # tracked or not
    files, secrets = offload.project_files(repo)
    assert name in secrets and name not in [entry["p"] for entry in files]


@pytest.mark.parametrize("name", [".env.example", "keyboard.py", "src/key_value.py", "tokenizer.py", "README.md"])
def test_ordinary_files_go(repo, name):
    path = repo / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("x")
    assert name in [entry["p"] for entry in offload.project_files(repo)[0]]


def test_ignored_files_stay_and_links_out_of_the_project_stay(repo):
    (repo / "outside").symlink_to("/etc/passwd")
    (repo / "up").symlink_to("../elsewhere")
    (repo / "fine").symlink_to("main.py")
    names = [entry["p"] for entry in offload.project_files(repo)[0]]
    assert "main.py" in names and "fine" in names
    assert "build/out" not in names and "outside" not in names and "up" not in names


def test_on_and_off_leave_git_clean_and_keep_my_notes(repo):
    git(repo, "add", "-A")
    git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "x")
    (repo / "CLAUDE.local.md").write_text("my own note\n")
    assert not offload.enabled(repo)
    offload.turn_on(repo)
    assert offload.enabled(repo)
    status = subprocess.run(["git", "-C", str(repo), "status", "--short"], capture_output=True, text=True).stdout
    assert status == ""
    assert "my own note" in (repo / "CLAUDE.local.md").read_text() and "offload" in (repo / "CLAUDE.local.md").read_text()
    offload.turn_on(repo)
    assert (repo / "CLAUDE.local.md").read_text().count("offload: on") == 1
    offload.turn_off(repo)
    assert not offload.enabled(repo) and (repo / "CLAUDE.local.md").read_text().strip() == "my own note"


def test_off_removes_a_note_file_that_was_only_ours(repo):
    offload.turn_on(repo)
    offload.turn_off(repo)
    assert not (repo / "CLAUDE.local.md").exists()


def test_nothing_runs_in_a_project_that_is_off(repo, monkeypatch):
    monkeypatch.setattr(offload, "ssh", lambda *a, **k: pytest.fail("reached the server"))
    with pytest.raises(offload.OffloadError, match="/offload on"):
        offload.run_job(repo, "make")
    with pytest.raises(offload.OffloadError, match="/offload on"):
        offload.pull(repo, "x")


def test_outside_git_is_a_clear_refusal(tmp_path):
    with pytest.raises(offload.OffloadError, match="git"):
        offload.git_root(str(tmp_path))


def test_names_are_safe_and_differ_by_folder(tmp_path):
    first, second = tmp_path / "a" / "My Project!", tmp_path / "b" / "My Project!"
    assert offload.project_name(first) != offload.project_name(second)
    assert offload.project_name(first).startswith("my-project-")


@pytest.mark.parametrize("path", ["/etc/passwd", "../x", "a/../../b"])
def test_pull_stays_in_the_project(repo, path):
    offload.turn_on(repo)
    with pytest.raises(offload.OffloadError, match="inside"):
        offload.pull(repo, path)


def test_too_much_to_copy_is_stopped(repo, monkeypatch):
    monkeypatch.setattr(offload, "MAX_PROJECT_BYTES", 1)
    monkeypatch.setattr(offload, "call", lambda *a, **k: pytest.fail("reached the server"))
    with pytest.raises(offload.OffloadError, match="gitignore"):
        offload.sync(repo)


def test_job_names_fit_the_servers_rule(repo, monkeypatch):
    import re
    offload.turn_on(repo)
    sent = []
    monkeypatch.setattr(offload, "sync", lambda root: {"project": "x" * 47 + "-abc123"})
    monkeypatch.setattr(offload, "call", lambda command, data=None: sent.append((command, data)) or {"status": "queued"})
    job = offload.run_job(repo, "make -j8 'all things'\n  && echo done", gpu=True, next_gpu="likely", restart=False, max_minutes=30)
    assert re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", job["job"])
    command, data = sent[0]
    assert command.split()[3:7] == ["gpu", "likely", "norestart", "30"] and data.startswith(b"make -j8")


# MCP


def mcp(method, **params):
    return offload_mcp.answer({"jsonrpc": "2.0", "id": 1, "method": method, "params": params})


def test_mcp_handshake_and_tool_list():
    assert mcp("initialize", protocolVersion="2025-06-18")["result"]["serverInfo"]["name"] == "offload"
    names = [t["name"] for t in mcp("tools/list")["result"]["tools"]]
    assert "offload_run" in names and all(t["inputSchema"]["type"] == "object" for t in mcp("tools/list")["result"]["tools"])
    assert offload_mcp.answer({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
    assert mcp("nonsense")["error"]["code"] == -32601


def test_mcp_refuses_in_a_project_that_is_off(repo, monkeypatch):
    monkeypatch.setattr(offload, "ssh", lambda *a, **k: pytest.fail("reached the server"))
    result = mcp("tools/call", name="offload_run", arguments={"command": "make", "project_dir": str(repo)})["result"]
    assert result["isError"] is True and "/offload on" in result["content"][0]["text"]


def test_mcp_reports_errors_without_dying(monkeypatch):
    def down(*args, **kwargs):
        raise offload.OffloadError("could not reach the server")
    monkeypatch.setattr(offload, "call", down)
    result = mcp("tools/call", name="offload_server", arguments={})["result"]
    assert result["isError"] is True
    assert mcp("tools/call", name="missing", arguments={})["result"]["isError"] is True
    assert mcp("tools/call", name="offload_job", arguments={})["result"]["isError"] is True


def test_mcp_shows_what_would_be_copied(repo):
    (repo / ".env").write_text("x")
    body = json.loads(mcp("tools/call", name="offload_files", arguments={"project_dir": str(repo)})["result"]["content"][0]["text"])
    assert body["keptOnThisMac"] == [".env"] and body["enabled"] is False
