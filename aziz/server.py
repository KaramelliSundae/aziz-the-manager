"""Aziz's HTTP face. Localhost only, no secrets. Whatever goes wrong, a caller gets an answer, because the image app and
the API are waiting on the other end."""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .host import SystemHost
from .manager import Aziz, NeedsConfirm

log = logging.getLogger(__name__)

PORT = 8421
LLM_URL = "http://127.0.0.1:8422"
WATCH_SECONDS = 2.0
MAX_BODY_BYTES = 64 * 1024


class Handler(BaseHTTPRequestHandler):
    aziz: Aziz
    timeout = 30  # a client that connects and says nothing must not hold a thread for ever

    def _reply(self, status: int, body: dict) -> None:
        data = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _guarded(self, route) -> None:
        try:
            route()
        except ValueError as error:
            self._reply(409, {"reason": str(error)})
        except (BrokenPipeError, ConnectionResetError):
            pass  # the caller gave up
        except Exception:  # noqa: BLE001 - the caller gets a reason, the service stays up
            log.exception("request failed: %s", self.path)
            self._reply(500, {"reason": "yönetici bu isteği işleyemedi"})

    def do_GET(self) -> None:  # noqa: N802 - http.server naming
        self._guarded(self._get)

    def do_POST(self) -> None:  # noqa: N802 - http.server naming
        self._guarded(self._post)

    def _get(self) -> None:
        if self.path == "/state":
            self._reply(200, self.aziz.state())
        elif self.path == "/log" or self.path.startswith("/log?"):
            limit = parse_qs(urlparse(self.path).query).get("limit", ["100"])[0]
            self._reply(200, {"entries": self.aziz.journal.recent(min(max(int(limit), 0), 200))})
        elif self.path.startswith("/jobs/"):
            job = self.aziz.job(self.path.removeprefix("/jobs/"))
            self._reply(200, asdict(job)) if job else self._reply(404, {"error": "not_found"})
        else:
            self._reply(404, {"error": "not_found"})

    def _body(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise ValueError("gövde geçersiz") from None
        if not 0 <= length <= MAX_BODY_BYTES:
            raise ValueError("gövde çok büyük")
        body = json.loads(self.rfile.read(length) or b"{}")
        if not isinstance(body, dict):
            raise ValueError("gövde geçersiz")
        return body

    def _post(self) -> None:
        if self.path == "/preempt":
            # Read first, and answer whatever happens: the image app is waiting.
            self.rfile.read(min(int(self.headers.get("Content-Length") or 0), MAX_BODY_BYTES))
            self._reply(200, {"free": self.aziz.preempt()})
        elif self.path == "/acquire":
            reason = self.aziz.acquire()
            self._reply(409, {"reason": reason}) if reason else self._reply(200, {"url": LLM_URL})
        elif self.path == "/mode":
            try:
                body = self._body()
                self.aziz.set_mode(body.get("mode", ""), body.get("confirm") is True)
            except NeedsConfirm as refused:
                self._reply(409, {"reason": str(refused), "needsConfirm": True})
                return
            except ValueError:
                self._reply(400, {"error": "mode must be assistant or compute"})
                return
            self._reply(200, {"mode": self.aziz.mode})
        elif self.path == "/server":
            body = self._body()  # the only way to open the image app: someone has to say so
            if not isinstance(body.get("on"), bool):
                raise ValueError("on, true veya false olmalı")  # a mistyped word must not close it either
            self.aziz.set_server(body["on"], body.get("force") is True)
            self._reply(200, self.aziz.state()["server"])
        elif self.path == "/jobs":
            body = self._body()
            minutes = body.get("maxMinutes")
            job = self.aziz.submit(
                body.get("id"), str(body.get("label", "")), body.get("gpu") is True, body.get("nextGpu") == "likely",
                body.get("restart") is not False, None if minutes is None else int(minutes), body.get("server") is True,
            )
            self._reply(200, asdict(job))
        elif self.path == "/forecast":
            self.aziz.forecast(self._body().get("nextGpu") == "likely")
            self._reply(200, {"ok": True})
        elif self.path == "/priority":
            self.aziz.set_priority(self._body().get("on") is True)
            self._reply(200, self.aziz.state())
        elif self.path.startswith("/jobs/") and self.path.endswith("/cancel"):
            found = self.aziz.cancel(self.path.removeprefix("/jobs/").removesuffix("/cancel"))
            self._reply(200, {"cancelled": True}) if found else self._reply(404, {"error": "not_found"})
        else:
            self._reply(404, {"error": "not_found"})

    def log_message(self, format: str, *args: object) -> None:
        log.debug(format, *args)


class Server(ThreadingHTTPServer):
    # The default of 5 resets callers during a burst, and one of them may be the image app asking for the GPU.
    request_queue_size = 128


def make_server(aziz: Aziz, port: int = PORT) -> ThreadingHTTPServer:
    handler = type("BoundHandler", (Handler,), {"aziz": aziz})
    return Server(("127.0.0.1", port), handler)


def serve(state_dir: Path) -> None:
    aziz = Aziz(SystemHost(), state_dir / "gpu-mode")
    server = make_server(aziz)
    threading.Thread(target=server.serve_forever, name="http", daemon=True).start()
    log.info("listening on 127.0.0.1:%d, mode %s", PORT, aziz.mode)
    while True:
        try:
            aziz.tick()
        except Exception:  # noqa: BLE001 - this loop is what brings the model back, so it must not die
            log.exception("watch round failed")
        time.sleep(WATCH_SECONDS)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    serve(Path(os.environ.get("STATE_DIRECTORY", "/var/lib/aziz")))
