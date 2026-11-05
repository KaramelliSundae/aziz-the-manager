#!/usr/bin/env python3
"""The same offload tools over MCP, for Claude Code and any other app that speaks it. Standard library only."""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import offload  # noqa: E402

PROJECT = {"type": "string", "description": "The project's folder. Defaults to the current project."}
TOOLS = [
    {"name": "offload_run", "description": "Run heavy work on the home server instead of this Mac: builds, big test suites, data processing. "
        "Copies the project (never secrets) and returns a job name at once; Aziz, the server's manager, decides when it runs. "
        "Refused unless the project is enabled with /offload on.",
     "inputSchema": {"type": "object", "required": ["command"], "properties": {
        "command": {"type": "string", "description": "Shell command, run from the project's folder."},
        "gpu": {"type": "boolean", "description": "True only if it really needs the GPU. This takes Kamil's local model offline while it runs."},
        "next_gpu": {"type": "string", "enum": ["likely", "unlikely"], "description": "How likely more GPU work is in the next 15-20 minutes. Default unlikely, so Kamil returns at once."},
        "restart": {"type": "boolean", "description": "False if rerunning from the start is not safe. Aziz restarts interrupted jobs otherwise."},
        "max_minutes": {"type": "integer", "description": "Time limit. Defaults: 4 hours GPU, 12 hours CPU."},
        "label": {"type": "string", "description": "Short name shown in the owner's app."},
        "project_dir": PROJECT}}},
    {"name": "offload_job", "description": "A job's status. `note` says why it is waiting or stopped.",
     "inputSchema": {"type": "object", "required": ["job"], "properties": {"job": {"type": "string"}}}},
    {"name": "offload_wait", "description": "Wait for a job to end, up to 10 minutes, then return its status.",
     "inputSchema": {"type": "object", "required": ["job"], "properties": {"job": {"type": "string"}, "seconds": {"type": "integer"}}}},
    {"name": "offload_logs", "description": "The end of a job's output.",
     "inputSchema": {"type": "object", "required": ["job"], "properties": {"job": {"type": "string"}, "lines": {"type": "integer"}}}},
    {"name": "offload_cancel", "description": "Stop a job.",
     "inputSchema": {"type": "object", "required": ["job"], "properties": {"job": {"type": "string"}}}},
    {"name": "offload_forecast", "description": "Update the guess about more GPU work. Send unlikely when GPU work is finished, so Kamil returns at once.",
     "inputSchema": {"type": "object", "required": ["next_gpu"], "properties": {"next_gpu": {"type": "string", "enum": ["likely", "unlikely"]}}}},
    {"name": "offload_pull", "description": "Bring a file or folder from the server's copy back into the project.",
     "inputSchema": {"type": "object", "required": ["path"], "properties": {"path": {"type": "string"}, "project_dir": PROJECT}}},
    {"name": "offload_server", "description": "What the server is doing: who has the GPU, memory, jobs.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "offload_files", "description": "What would be copied for this project, and which secrets stay on this Mac. Copies nothing.",
     "inputSchema": {"type": "object", "properties": {"project_dir": PROJECT}}},
]


def tool(name: str, args: dict) -> object:
    if name == "offload_run":
        root = offload.git_root(args.get("project_dir"))
        return offload.run_job(root, args["command"], bool(args.get("gpu")), args.get("next_gpu", "unlikely"),
                               args.get("restart", True), int(args.get("max_minutes") or 0), args.get("label", ""))
    if name == "offload_job":
        return offload.call(f"job {args['job']}")
    if name == "offload_wait":
        return offload.wait_job(args["job"], min(float(args.get("seconds") or 600), 600))
    if name == "offload_logs":
        return offload.call(f"logs {args['job']} {int(args.get('lines') or 200)}")["log"]
    if name == "offload_cancel":
        return offload.call(f"cancel {args['job']}")
    if name == "offload_forecast":
        return offload.call(f"forecast {args['next_gpu']}")
    if name == "offload_pull":
        return offload.pull(offload.git_root(args.get("project_dir")), args["path"])
    if name == "offload_server":
        return offload.call("state")
    if name == "offload_files":
        root = offload.git_root(args.get("project_dir"))
        files, secrets = offload.project_files(root)
        return {"enabled": offload.enabled(root), "files": len(files), "bytes": sum(f["s"] for f in files), "keptOnThisMac": secrets}
    raise offload.OffloadError(f"unknown tool {name}")


def answer(message: dict) -> dict | None:
    method, identity = message.get("method"), message.get("id")
    if identity is None:
        return None  # a notification
    if method == "initialize":
        result = {
            "protocolVersion": message.get("params", {}).get("protocolVersion", "2025-06-18"),
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "offload", "version": "0.1.0"},
        }
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": TOOLS}
    elif method == "tools/call":
        params = message.get("params", {})
        try:
            value = tool(params.get("name", ""), params.get("arguments") or {})
            text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
            result = {"content": [{"type": "text", "text": text}]}
        except (offload.OffloadError, KeyError, ValueError, OSError) as error:
            result = {"content": [{"type": "text", "text": f"offload: {error}"}], "isError": True}
    else:
        return {"jsonrpc": "2.0", "id": identity, "error": {"code": -32601, "message": f"no method {method}"}}
    return {"jsonrpc": "2.0", "id": identity, "result": result}


def main() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            reply = answer(json.loads(line))
        except ValueError:
            reply = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "not JSON"}}
        if reply is not None:
            sys.stdout.write(json.dumps(reply, ensure_ascii=False) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
