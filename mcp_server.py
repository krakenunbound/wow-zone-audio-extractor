"""MCP server for the WoW Zone Audio Extractor.

Stdio only. Tools call the app's localhost bridge at 127.0.0.1:47221.
If the window is closed, the first tool starts it.

Register in ~/.grok/config.toml:

[mcp_servers.wow-ambient]
command = "python"
args = ["F:/WoW-Ambient/mcp_server.py"]
enabled = true
startup_timeout_sec = 30
tool_timeout_sec = 600
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

HOST_ADDR = "127.0.0.1"
PORT = 47221
HOST = f"http://{HOST_ADDR}:{PORT}"
HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.join(HERE, "wow_zone_audio_extractor_gui.py")
SERVER_INFO = {"name": "wow-ambient", "version": "0.5.0"}
INSTRUCTIONS = (
    "WoW Zone Audio Extractor is a desktop app. These tools drive the open window. "
    "A zone ambience is separate clocks: Bed (one seamless loop, birds live inside it), "
    "Atmosphere (short loops re-picked on a timer), Detail (one-shots with gaps, fades, and position), "
    "and Music (playlist plus a long silence). "
    "Call wow_status first. Load zones, open one area, then read wow_recipe before extracting."
)

TOOLS = [
    {
        "name": "wow_status",
        "description": "See whether the extractor window is up, which build and zone are loaded, and the current layers.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "wow_load_zones",
        "description": "Load the zone list from the WoW DB2 build. Uses the detected WoW folder unless wow_path or build is set.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "wow_path": {"type": "string"},
                "build": {"type": "string"},
                "force": {"type": "boolean"},
            },
        },
    },
    {
        "name": "wow_search_zones",
        "description": "Find zones by name or area id. Load zones first.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "limit": {"type": "integer"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "wow_open_zone",
        "description": "Select one zone in the window and retrieve its music, ambience and weather clips. Every clip starts unchecked; check the ones you want (wow_check) before extracting checked clips.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "area_id": {"type": "integer"},
                "name": {"type": "string"},
                "include_music": {"type": "boolean"},
                "include_ambient": {"type": "boolean"},
                "include_subzone_music": {"type": "boolean"},
                "include_weather": {"type": "boolean"},
            },
        },
    },
    {
        "name": "wow_recipe",
        "description": "Summarize the loaded clips as Bed, Atmosphere, Detail, and Music layers, including how each clock plays.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "wow_clips",
        "description": "List loaded clips. Filter by role (Bed, Atmosphere, Detail, Music, Intro, Weather), time, or a text query.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "role": {"type": "string"},
                "time": {"type": "string"},
                "limit": {"type": "integer"},
            },
        },
    },
    {
        "name": "wow_check",
        "description": "Check or uncheck loaded clips for extraction by file_data_ids.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "file_data_ids": {"type": "array", "items": {"type": "integer"}},
                "checked": {"type": "boolean"},
            },
            "required": ["file_data_ids"],
        },
    },
    {
        "name": "wow_extract",
        "description": "Extract clips to the output folder. which is checked, visible, or tagged. Optional file_data_ids extracts just those. format is mp3, ogg, flac, wav, or m4a.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "which": {"type": "string"},
                "format": {"type": "string"},
                "output": {"type": "string"},
                "file_data_ids": {"type": "array", "items": {"type": "integer"}},
            },
        },
    },
    {
        "name": "wow_preview",
        "description": "Play one loaded clip through the app window.",
        "inputSchema": {
            "type": "object",
            "properties": {"file_data_id": {"type": "integer"}},
            "required": ["file_data_id"],
        },
    },
    {
        "name": "wow_stop",
        "description": "Stop the clip playing in the app window.",
        "inputSchema": {"type": "object", "properties": {}},
    },
]

_PATHS = {
    "wow_status": ("GET", "/status"),
    "wow_load_zones": ("POST", "/zones/load"),
    "wow_search_zones": ("POST", "/zones/search"),
    "wow_open_zone": ("POST", "/audio/open"),
    "wow_recipe": ("GET", "/recipe"),
    "wow_clips": ("POST", "/clips"),
    "wow_check": ("POST", "/clips/check"),
    "wow_extract": ("POST", "/extract"),
    "wow_preview": ("POST", "/preview"),
    "wow_stop": ("POST", "/preview/stop"),
}


def http_json(method: str, path: str, body: dict | None = None, timeout: int = 30) -> dict:
    data = None if body is None else json.dumps(body).encode("utf-8")
    request = urllib.request.Request(
        HOST + path,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8") or "{}")


def port_open() -> bool:
    """True when something is listening on the bridge port, even if it is too busy to answer."""
    try:
        with socket.create_connection((HOST_ADDR, PORT), timeout=1.5):
            return True
    except OSError:
        return False


def app_status(timeout: int = 10) -> dict:
    """Ask the open window for its status. A slow answer means busy, not closed."""
    try:
        return http_json("GET", "/status", timeout=timeout)
    except (TimeoutError, socket.timeout):
        return {"ok": True, "busy": True, "note": "The window is open but did not answer in time. It may be busy or showing a dialog."}
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, (TimeoutError, socket.timeout)):
            return {"ok": True, "busy": True, "note": "The window is open but did not answer in time. It may be busy or showing a dialog."}
        raise


def ensure_app() -> dict:
    # Launch only when nothing holds the port. A busy window still holds it, and a
    # second copy would fail to bind and leave two windows open.
    if port_open():
        return app_status()
    exe = sys.executable
    pythonw = os.path.join(os.path.dirname(exe), "pythonw.exe")
    if os.name == "nt" and os.path.exists(pythonw):
        exe = pythonw
    flags = 0
    if os.name == "nt":
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    subprocess.Popen([exe, APP], cwd=HERE, creationflags=flags)
    deadline = time.time() + 25
    while time.time() < deadline:
        if port_open():
            return app_status()
        time.sleep(0.4)
    return {"ok": False, "error": "The extractor window did not open. Start wow_zone_audio_extractor_gui.py."}


def call_tool(name: str, arguments: dict) -> dict:
    if name not in _PATHS:
        raise ValueError(f"unknown tool {name}")
    status = ensure_app()
    if status.get("ok") is False or name == "wow_status":
        return status
    method, path = _PATHS[name]
    timeout = 620 if name == "wow_extract" else 320 if name in {"wow_load_zones", "wow_open_zone"} else 30
    try:
        return http_json(method, path, arguments, timeout=timeout)
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {"ok": False, "error": raw or str(exc)}


def send(message: dict) -> None:
    sys.stdout.write(json.dumps(message) + "\n")
    sys.stdout.flush()


def handle(message: dict):
    method = message.get("method")
    if method == "initialize":
        version = message.get("params", {}).get("protocolVersion", "2025-06-18")
        return {
            "protocolVersion": version,
            "capabilities": {"tools": {}},
            "serverInfo": SERVER_INFO,
            "instructions": INSTRUCTIONS,
        }
    if method == "tools/list":
        return {"tools": TOOLS}
    if method == "tools/call":
        params = message.get("params") or {}
        try:
            result = call_tool(params.get("name"), dict(params.get("arguments") or {}))
            return {
                "content": [{"type": "text", "text": json.dumps(result, indent=1)}],
                "isError": isinstance(result, dict) and result.get("ok") is False,
            }
        except Exception as exc:  # noqa: BLE001
            return {"content": [{"type": "text", "text": f"{type(exc).__name__}: {exc}"}], "isError": True}
    if method == "ping":
        return {}
    if message.get("id") is not None:
        raise LookupError(str(method))
    return None


def main() -> None:
    for stream in (sys.stdin, sys.stdout):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except ValueError:
            continue
        message_id = message.get("id")
        try:
            result = handle(message)
            if message_id is not None:
                send({"jsonrpc": "2.0", "id": message_id, "result": result})
        except LookupError as exc:
            send({"jsonrpc": "2.0", "id": message_id, "error": {"code": -32601, "message": f"method not found: {exc}"}})
        except Exception as exc:  # noqa: BLE001
            if message_id is not None:
                send({"jsonrpc": "2.0", "id": message_id, "error": {"code": -32603, "message": str(exc)}})


if __name__ == "__main__":
    main()
