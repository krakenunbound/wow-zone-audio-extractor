"""Localhost agent API for the running WoW Zone Audio Extractor.

Bind is 127.0.0.1 only. The GUI thread owns every command. This server
queues the request and waits for the window to answer.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

AGENT_PORT = 47221

_TIMEOUTS = {
    "/zones/load": 300,
    "/audio/open": 180,
    "/extract": 600,
    "/preview": 120,
}


class AgentBridge:
    def __init__(self, ui_queue, port: int = AGENT_PORT):
        self.ui_queue = ui_queue
        self.port = port
        self._httpd: ThreadingHTTPServer | None = None

    def start(self) -> None:
        bridge = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args) -> None:  # noqa: A003
                return

            def _reply(self, status: int, payload: dict) -> None:
                raw = json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def _body(self) -> dict:
                length = int(self.headers.get("Content-Length") or 0)
                if length <= 0:
                    return {}
                raw = self.rfile.read(length)
                if not raw:
                    return {}
                data = json.loads(raw.decode("utf-8"))
                if not isinstance(data, dict):
                    raise ValueError("JSON body must be an object.")
                return data

            def _dispatch(self, body: dict) -> None:
                path = urlparse(self.path).path
                box: dict = {}
                event = threading.Event()
                bridge.ui_queue.put(("agent", path, body, box, event))
                timeout = _TIMEOUTS.get(path, 30)
                if not event.wait(timeout):
                    self._reply(504, {"ok": False, "error": f"Timed out after {timeout}s."})
                    return
                result = box.get("result") or {"ok": False, "error": "The app returned nothing."}
                status = 200 if result.get("ok", True) else 400
                self._reply(status, result)

            def do_GET(self) -> None:  # noqa: N802
                try:
                    self._dispatch({})
                except Exception as exc:  # noqa: BLE001
                    self._reply(400, {"ok": False, "error": str(exc)})

            def do_POST(self) -> None:  # noqa: N802
                try:
                    self._dispatch(self._body())
                except Exception as exc:  # noqa: BLE001
                    self._reply(400, {"ok": False, "error": str(exc)})

        self._httpd = ThreadingHTTPServer(("127.0.0.1", self.port), Handler)
        thread = threading.Thread(target=self._httpd.serve_forever, name="wow-ambient-agent", daemon=True)
        thread.start()
