#!/usr/bin/env python3
"""
WoW Zone Audio Extractor (Desktop GUI, no web UI).
"""

from __future__ import annotations

import csv
import ctypes
import dataclasses
import html
import json
import math
import os
import pickle
import queue
import re
import shutil
import subprocess
import threading
import time
import traceback
import urllib.request
import winsound
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from agent_bridge import AgentBridge, AGENT_PORT


APP_TITLE = "WoW Zone Audio Extractor"
APP_VERSION = "0.4.3"
WAGO_DB_INDEX_URL = "https://wago.tools/db2"
WAGO_DB_TABLE_URL = "https://wago.tools/db2/{table}/csv?build={build}&locale=enUS"
WAGO_CASC_URL = "https://wago.tools/api/casc/{file_data_id}"
USER_AGENT = f"{APP_TITLE}/{APP_VERSION}"
EXPORT_INDEX_FILENAME = "wow_zone_audio_export.json"
ZONE_MANIFEST_FILENAME = "zone_manifest.json"
WOW_LISTFILE_URL = "https://github.com/wowdev/wow-listfile/releases/latest/download/community-listfile.csv"

# Maps WoW expansion content-patch prefixes found in filenames to friendly labels.
EXPANSION_PREFIX_MAP: dict[str, str] = {
    "50": "Vanilla",
    "60": "Vanilla",
    "70": "TBC",
    "80": "WotLK",
    "85": "Cata",
    "90": "MoP",
    "100": "WoD",
    "110": "Legion",
    "120": "BFA",
    "130": "SL",
    "140": "DF",
    "150": "TWW",
}


def _to_int(value: str | int | None) -> int:
    if value is None:
        return 0
    if isinstance(value, int):
        return value
    text = str(value).strip()
    if not text:
        return 0
    try:
        return int(float(text))
    except ValueError:
        return 0


def sanitize_path_name(name: str, fallback: str) -> str:
    cleaned = re.sub(r"[<>:\"/\\|?*\x00-\x1F]", "_", name).strip()
    cleaned = cleaned.rstrip(". ")
    return cleaned if cleaned else fallback


def slugify_name(name: str, fallback: str) -> str:
    cleaned = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
    return cleaned if cleaned else fallback


def pretty_count(value: int) -> str:
    return f"{value:,}"


def utc_timestamp() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def humanize_identifier(text: str) -> str:
    value = (text or "").strip()
    if not value:
        return ""
    value = re.sub(r"([a-z])([A-Z])", r"\1 \2", value)
    value = re.sub(r"[_\-]+", " ", value)
    value = re.sub(r"\s+", " ", value).strip()
    return value.title()


def friendly_zone_name(area_name: str, parent_area_name: str, zone_name: str, area_id: int) -> str:
    label = (area_name or "").strip()
    zone_label = humanize_identifier(zone_name)
    if re.fullmatch(r"\d+", zone_label or ""):
        zone_label = ""
    if not label:
        label = zone_label or parent_area_name or f"Area {area_id}"

    if label.startswith("$n's"):
        suffix = label[4:].strip()
        base = parent_area_name or "Player"
        return f"{base} {suffix}".strip()

    cleaned = re.sub(r"^\[(PH|TEMP|TEST|UNUSED|DNT)\]\s*", "", label, flags=re.IGNORECASE).strip()
    cleaned = cleaned.replace("[TRANSITION ZONE]", "Transition Zone").strip()
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" -")
    if cleaned and cleaned != label:
        label = cleaned

    if re.fullmatch(r"\d+", label):
        label = zone_label or parent_area_name or f"Area {area_id}"

    if not label or label in {"[DNT]", "[PH]", "[TEMP]"}:
        label = zone_label or parent_area_name or f"Area {area_id}"

    return label


def pretty_sound_title_from_path(file_path: str | None, file_data_id: int) -> str | None:
    if not file_path:
        return None
    name = Path(file_path).stem
    # Split on underscores, hyphens, spaces
    tokens = [token for token in re.split(r"[_\-\s]+", name) if token]
    # Strip common WoW audio prefixes
    while tokens and tokens[0].lower() in {"amb", "fx", "mus", "music", "snd", "sfx", "sound", "vo", "mon"}:
        tokens.pop(0)
    # Map expansion numbers to friendly names
    if tokens and tokens[0] in EXPANSION_PREFIX_MAP:
        tokens[0] = EXPANSION_PREFIX_MAP[tokens[0]]
    # Strip leading small numeric IDs (zone internal IDs etc.)
    while tokens and tokens[0].isdigit() and len(tokens[0]) <= 3:
        tokens.pop(0)
    # Strip the file_data_id if it appears as last token
    if tokens and tokens[-1] == str(file_data_id):
        tokens.pop()
    # Expand camelCase within tokens  e.g. "AtalAbasi" -> "Atal Abasi"
    expanded: list[str] = []
    for token in tokens:
        parts = re.sub(r"([a-z])([A-Z])", r"\1 \2", token)
        expanded.extend(parts.split())
    tokens = expanded
    if not tokens:
        return None
    title = " ".join(tokens)
    title = re.sub(r"\s+", " ", title).strip()
    return title.title()


def fetch_bytes(url: str, timeout: int = 120, retries: int = 3) -> bytes:
    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as response:
                return response.read()
        except Exception as exc:  # noqa: BLE001 - network retries
            last_error = exc
            if attempt < retries:
                time.sleep(0.6 * attempt)
    assert last_error is not None
    raise RuntimeError(f"Request failed for {url}: {last_error}") from last_error


def parse_wago_db_index() -> dict:
    page = fetch_bytes(WAGO_DB_INDEX_URL, timeout=90).decode("utf-8", errors="replace")
    match = re.search(r'data-page="([^"]+)"', page)
    if not match:
        raise RuntimeError("Could not parse Wago DB2 index page.")
    payload = html.unescape(match.group(1))
    return json.loads(payload)


def detect_retail_dir(path: Path) -> Path | None:
    path = path.expanduser().resolve()
    if (path / "Wow.exe").exists() and (path / "Interface").exists():
        return path
    if (path / "_retail_" / "Wow.exe").exists():
        return (path / "_retail_").resolve()
    return None


def detect_wow_root_from_retail(retail_dir: Path) -> Path:
    if retail_dir.name.lower() == "_retail_":
        return retail_dir.parent
    return retail_dir


def parse_local_retail_build(wow_root_dir: Path) -> str | None:
    build_info = wow_root_dir / ".build.info"
    if not build_info.exists():
        return None

    try:
        lines = build_info.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None

    if len(lines) < 2:
        return None

    header = [chunk.split("!", 1)[0] for chunk in lines[0].split("|")]
    rows = []
    for raw in lines[1:]:
        cols = raw.split("|")
        if len(cols) < len(header):
            continue
        rows.append(dict(zip(header, cols)))

    for row in rows:
        if row.get("Product") == "wow" and row.get("Active") == "1":
            version = row.get("Version", "").strip()
            if version:
                return version

    for row in rows:
        if row.get("Product") == "wow":
            version = row.get("Version", "").strip()
            if version:
                return version

    return None


PRODUCT_LABELS = {
    "wow": "Retail",
    "wowt": "Retail PTR",
    "wow_beta": "Retail Beta",
    "wowxptr": "Retail XPTR",
    "wow_classic": "Classic",
    "wow_classic_era": "Classic Era",
    "wow_classic_ptr": "Classic PTR",
    "wow_classic_beta": "Classic Beta",
    "wow_anniversary": "Anniversary",
    "wowf": "Forever",
}


@dataclasses.dataclass(frozen=True)
class WowInstall:
    product: str
    label: str
    version: str
    folder: Path

    @property
    def choice(self) -> str:
        return f"{self.label}  {self.version}"


def exe_file_version(path: Path) -> str:
    version = ctypes.windll.version
    version.GetFileVersionInfoSizeW.argtypes = [ctypes.c_wchar_p, ctypes.c_void_p]
    version.GetFileVersionInfoSizeW.restype = ctypes.c_uint
    version.GetFileVersionInfoW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint, ctypes.c_uint, ctypes.c_void_p]
    version.GetFileVersionInfoW.restype = ctypes.c_int
    version.VerQueryValueW.argtypes = [
        ctypes.c_void_p,
        ctypes.c_wchar_p,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_uint),
    ]
    version.VerQueryValueW.restype = ctypes.c_int

    size = version.GetFileVersionInfoSizeW(str(path), None)
    if not size:
        return ""
    buffer = ctypes.create_string_buffer(size)
    if not version.GetFileVersionInfoW(str(path), 0, size, buffer):
        return ""
    translation = ctypes.c_void_p()
    length = ctypes.c_uint()
    if not version.VerQueryValueW(buffer, "\\VarFileInfo\\Translation", ctypes.byref(translation), ctypes.byref(length)):
        return ""
    words = ctypes.cast(translation, ctypes.POINTER(ctypes.c_uint16))
    key = f"\\StringFileInfo\\{words[0]:04x}{words[1]:04x}\\FileVersion"
    text_ptr = ctypes.c_void_p()
    if not version.VerQueryValueW(buffer, key, ctypes.byref(text_ptr), ctypes.byref(length)):
        return ""
    text = ctypes.cast(text_ptr, ctypes.c_wchar_p).value or ""
    text = text.replace("Version", " ").strip()
    return text.split()[0] if text else ""


def wow_root_from(path: Path) -> Path | None:
    path = path.expanduser()
    if not path.exists():
        return None
    if (path / ".build.info").exists():
        return path.resolve()
    if (path / ".flavor.info").exists() and (path.parent / ".build.info").exists():
        return path.parent.resolve()
    if (path / "_retail_" / "Wow.exe").exists():
        return path.resolve()
    return None


def locate_wow_root(start: Path | None = None) -> Path | None:
    candidates: list[Path] = []
    if start is not None:
        candidates.append(start)
    candidates.extend(
        [
            Path(r"C:\Program Files (x86)\World of Warcraft"),
            Path(r"C:\Program Files\World of Warcraft"),
        ]
    )
    for candidate in candidates:
        found = wow_root_from(candidate)
        if found:
            return found
    return None


def _build_info_versions(root: Path) -> dict[str, str]:
    versions: dict[str, str] = {}
    if not (root / ".build.info").exists():
        return versions
    for row in _build_info_rows(root):
        product = (row.get("Product") or "").strip()
        version = (row.get("Version") or "").strip()
        if product and version:
            versions[product] = version
    return versions


def _build_info_rows(root: Path) -> list[dict[str, str]]:
    try:
        lines = (root / ".build.info").read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    if len(lines) < 2:
        return []
    header = [chunk.split("!", 1)[0] for chunk in lines[0].split("|")]
    rows: list[dict[str, str]] = []
    for raw in lines[1:]:
        cols = raw.split("|")
        if len(cols) < len(header):
            continue
        rows.append(dict(zip(header, cols)))
    return rows


def _flavor_product(folder: Path) -> str:
    try:
        lines = [line.strip() for line in (folder / ".flavor.info").read_text(encoding="utf-8", errors="replace").splitlines()]
    except OSError:
        return ""
    lines = [line for line in lines if line and "!" not in line]
    return lines[-1] if lines else ""


def discover_wow_installs(root: Path) -> list[WowInstall]:
    versions = _build_info_versions(root)
    installs: list[WowInstall] = []
    try:
        folders = sorted(path for path in root.iterdir() if path.is_dir() and path.name.startswith("_"))
    except OSError:
        return []
    for folder in folders:
        if not (folder / "Wow.exe").exists() or not (folder / ".flavor.info").exists():
            continue
        product = _flavor_product(folder)
        if not product:
            continue
        version = versions.get(product) or exe_file_version(folder / "Wow.exe")
        if not version:
            continue
        installs.append(
            WowInstall(
                product=product,
                label=PRODUCT_LABELS.get(product, product),
                version=version,
                folder=folder.resolve(),
            )
        )
    installs.sort(key=lambda item: (item.product != "wow", item.label.lower()))
    return installs


def find_ffmpeg() -> Path | None:
    direct = shutil.which("ffmpeg")
    if direct:
        return Path(direct)

    candidates = [
        Path(
            r"C:\Users\The Kraken\AppData\Local\Microsoft\WinGet\Packages\Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe\ffmpeg-8.0.1-full_build\bin\ffmpeg.exe"
        ),
        Path(r"C:\ffmpeg\bin\ffmpeg.exe"),
        Path(r"C:\Program Files\ffmpeg\bin\ffmpeg.exe"),
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


def ffmpeg_args_for_format(fmt: str) -> tuple[str, list[str]]:
    fmt = fmt.lower().strip()
    if fmt == "wav":
        return "wav", ["-c:a", "pcm_s16le"]
    if fmt == "m4a":
        return "m4a", ["-c:a", "aac", "-b:a", "192k"]
    return "mp3", ["-c:a", "libmp3lame", "-b:a", "192k"]


@dataclasses.dataclass(frozen=True)
class ZoneInfo:
    area_id: int
    area_name: str
    zone_name: str
    parent_area_id: int
    parent_area_name: str
    ambience_id: int
    zone_music_id: int
    uw_zone_music_id: int
    intro_sound_id: int
    uw_intro_sound_id: int

    @property
    def display_name(self) -> str:
        if self.parent_area_name and self.parent_area_name != self.friendly_name:
            return f"{self.friendly_name} ({self.parent_area_name})  [ID {self.area_id}]"
        return f"{self.friendly_name}  [ID {self.area_id}]"

    @property
    def friendly_name(self) -> str:
        return friendly_zone_name(self.area_name, self.parent_area_name, self.zone_name, self.area_id)

    @property
    def family_name(self) -> str:
        if self.parent_area_name and self.parent_area_name != self.area_name:
            return self.parent_area_name
        if self.zone_name and self.zone_name != self.area_name:
            return self.zone_name
        return "Standalone Zones"


ZONE_CATALOG_NAME = "zones.json"
AUDIO_INDEX_NAME = "audio_index.pkl"


def write_zone_catalog(path: Path, build: str, zones: list[ZoneInfo]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "v": 1,
        "build": build,
        "zones": [dataclasses.asdict(zone) for zone in zones],
    }
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    temporary.replace(path)


def read_zone_catalog(path: Path, build: str) -> list[ZoneInfo] | None:
    if not path.exists() or path.stat().st_size < 32:
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or payload.get("v") != 1 or payload.get("build") != build:
        return None
    rows = payload.get("zones")
    if not isinstance(rows, list) or not rows:
        return None
    fields = [field.name for field in dataclasses.fields(ZoneInfo)]
    zones: list[ZoneInfo] = []
    for row in rows:
        if not isinstance(row, dict):
            return None
        try:
            zones.append(ZoneInfo(**{name: row[name] for name in fields}))
        except (KeyError, TypeError):
            return None
    return zones


@dataclasses.dataclass(frozen=True)
class AudioEntry:
    zone_id: int
    zone_name: str
    category: str  # "music" or "ambient sounds"
    sound_kit_id: int
    file_data_id: int
    source_type: str
    source_id: int
    hint: str

    @property
    def category_slug(self) -> str:
        return "music" if self.category == "music" else "ambient"

    @property
    def group_label(self) -> str:
        hint = (self.hint or "").strip()
        hint_lower = hint.lower()
        if self.category == "music":
            if self.source_type == "ZoneIntroMusicTable":
                return f"Intro Theme: {hint}" if hint else "Intro Theme"
            return hint or "Zone Music"

        if self.source_type == "SoundAmbienceFlavor":
            if "night" in hint_lower and "any " not in hint_lower:
                return "Night Details"
            if "day" in hint_lower and "any " not in hint_lower:
                return "Day Details"
            return "Ambient Details"

        if hint_lower.startswith("any "):
            if "ambient loop" in hint_lower:
                return "Ambient Loop"
            if "atmosphere" in hint_lower:
                return "Atmosphere"
            return "Ambient Details"

        # Distinguish ambient loops (nature/forest) from atmosphere (wind/weather)
        if "ambient loop" in hint_lower:
            if "night" in hint_lower:
                return "Night Ambient Loop"
            if "day" in hint_lower:
                return "Day Ambient Loop"
            return "Ambient Loop"

        if "ambient start" in hint_lower or "ambient stop" in hint_lower:
            if "night" in hint_lower:
                return "Night Transition"
            if "day" in hint_lower:
                return "Day Transition"
            return "Ambient Transition"

        if "night" in hint_lower:
            return "Night Atmosphere"
        if "day" in hint_lower:
            return "Day Atmosphere"

        layer_match = re.search(r"(\d+)$", hint)
        if layer_match:
            return f"Atmosphere Layer {layer_match.group(1)}"
        return "Ambient Atmosphere"

    @property
    def group_slug(self) -> str:
        base = f"{self.category_slug}_{self.group_label}_sk_{self.sound_kit_id}"
        return slugify_name(base, f"group_{self.sound_kit_id}")

    @property
    def suggested_layer_type(self) -> str:
        if self.category == "music":
            return "playlist"
        if self.source_type == "SoundAmbienceFlavor":
            return "intermittent"
        return "loop"

    @property
    def source_label(self) -> str:
        if self.source_type == "ZoneMusic":
            return f"ZoneMusic #{self.source_id}"
        if self.source_type == "ZoneIntroMusicTable":
            return f"Intro #{self.source_id}"
        if self.source_type == "SoundAmbienceFlavor":
            return f"Flavor #{self.source_id}"
        if self.source_type == "SoundAmbience":
            return f"Ambience #{self.source_id}"
        return f"{self.source_type} #{self.source_id}"

    @property
    def time_of_day(self) -> str:
        hint_lower = (self.hint or "").lower()
        if "night" in hint_lower:
            return "Night"
        if "day" in hint_lower:
            return "Day"
        return "Any"

    @property
    def type_label(self) -> str:
        if self.category == "music":
            if self.source_type == "ZoneIntroMusicTable":
                return "Intro Theme"
            return "Zone Music"
        if self.source_type == "SoundAmbienceFlavor":
            return "Ambient Details"
        return "Ambient Bed"

    @property
    def mood_label(self) -> str:
        hint_lower = (self.hint or "").lower()
        if self.source_type == "ZoneIntroMusicTable":
            return "Cinematic"
        if self.category == "music":
            if "battle" in hint_lower or "combat" in hint_lower:
                return "Tension"
            if self.time_of_day == "Night":
                return "Night Travel"
            return "Exploration"
        if self.source_type == "SoundAmbienceFlavor":
            return "Details"
        if "storm" in hint_lower or "wind" in hint_lower:
            return "Weather"
        return "Atmospheric"

    @property
    def area_label(self) -> str:
        return self.zone_name

    @property
    def default_title(self) -> str:
        return f"{self.group_label} [{self.file_data_id}]"

    @property
    def default_description(self) -> str:
        return (
            f"{self.type_label} for {self.zone_name}. "
            f"Time of day: {self.time_of_day}. "
            f"Mood: {self.mood_label}. "
            f"Source: {self.source_label}."
        )


def merge_time_hint(current: str, incoming: str) -> str:
    """Collapse day+night references to the same kit into one 'any' hint."""
    if not current or current == incoming:
        return incoming or current

    def split_hint(value: str) -> tuple[str, str]:
        lower = value.lower()
        if lower.startswith("day "):
            return "day", value[4:]
        if lower.startswith("night "):
            return "night", value[6:]
        return "any", value

    current_time, current_rest = split_hint(current)
    incoming_time, incoming_rest = split_hint(incoming)
    if current_rest.lower() == incoming_rest.lower() and {current_time, incoming_time} == {"day", "night"}:
        return f"any {current_rest}"
    return current


def _semitone_span(plus: float, minus: float) -> tuple[float, float]:
    down = 12 * math.log2(1 - minus) if 0 < minus < 1 else 0.0
    up = 12 * math.log2(1 + plus) if plus > 0 else 0.0
    return round(down, 2), round(up, 2)


def _non_negative_ms(value: str | int | None) -> int:
    number = _to_int(value)
    return number if number > 0 else 0


def profile_from_kit_rows(kit_row: dict[str, str], advanced_row: dict[str, str] | None) -> dict[str, object]:
    advanced_row = advanced_row or {}
    pitch_down, pitch_up = _semitone_span(
        float(kit_row.get("PitchVariationPlus") or 0),
        float(kit_row.get("PitchVariationMinus") or 0),
    )
    flags = _to_int(kit_row.get("Flags"))
    return {
        "volume": round(float(kit_row.get("VolumeFloat") or 0), 3),
        "looping": bool(flags & 0x200),
        "flags": flags,
        "sound_type": _to_int(kit_row.get("SoundType")),
        "pitch_down": pitch_down,
        "pitch_up": pitch_up,
        "interval_min_ms": _non_negative_ms(advanced_row.get("TimeIntervalMin")),
        "interval_max_ms": _non_negative_ms(advanced_row.get("TimeIntervalMax")),
        "delay_min_ms": _non_negative_ms(advanced_row.get("DelayMin")),
        "delay_max_ms": _non_negative_ms(advanced_row.get("DelayMax")),
        "fade_in_ms": _non_negative_ms(advanced_row.get("VolumeFadeInTimeMin")),
        "fade_out_ms": _non_negative_ms(advanced_row.get("VolumeFadeOutTimeMin")),
        "pos_min": _non_negative_ms(advanced_row.get("MinRandomPosOffset")),
        "pos_max": _non_negative_ms(advanced_row.get("MaxRandomPosOffset")),
    }


def describe_playback(
    entry: AudioEntry,
    profile: dict[str, object] | None,
    music_silence_ms: tuple[int, int] | None = None,
) -> dict[str, object]:
    """Say which clock a clip is on: bed, atmosphere, detail, or music."""
    profile = profile or {}
    hint = (entry.hint or "").lower()
    looping = bool(profile.get("looping"))
    if entry.category == "music":
        role = "Intro" if entry.source_type == "ZoneIntroMusicTable" else "Music"
    elif "ambient loop" in hint:
        role = "Bed"
    elif entry.source_type == "SoundAmbienceFlavor" or not looping:
        role = "Detail"
    else:
        role = "Atmosphere"

    pitch_down = float(profile.get("pitch_down") or 0)
    pitch_up = float(profile.get("pitch_up") or 0)
    interval_min = int(profile.get("interval_min_ms") or 0)
    interval_max = int(profile.get("interval_max_ms") or 0)
    delay_min = int(profile.get("delay_min_ms") or 0)
    delay_max = int(profile.get("delay_max_ms") or 0)
    fade_in = int(profile.get("fade_in_ms") or 0)
    fade_out = int(profile.get("fade_out_ms") or 0)
    pos_min = int(profile.get("pos_min") or 0)
    pos_max = int(profile.get("pos_max") or 0)
    volume = profile.get("volume")

    if role == "Bed":
        how = "Always-on seamless loop. Birds and animal calls inside this recording share its clock."
    elif role == "Atmosphere":
        window = _seconds_window(interval_min, interval_max)
        if window:
            how = f"A short loop. The client picks another file from this kit every {window}."
        else:
            how = "A short loop that keeps running."
        if pitch_down or pitch_up:
            how += f" Pitch shifts about {pitch_down:g} to +{pitch_up:g} semitones."
    elif role == "Detail":
        window = _seconds_window(interval_min, interval_max) or "on a random gap"
        how = f"One-shot. Plays once, then waits {window}."
        if delay_min or delay_max:
            how += f" The first one waits {_seconds_window(delay_min, delay_max)} after you enter."
        if fade_in or fade_out:
            how += f" Fade in {fade_in / 1000:g}s, fade out {fade_out / 1000:g}s."
        if pos_max:
            how += f" Placed {pos_min}–{pos_max} yards from the listener, so it pans."
        if pitch_down or pitch_up:
            how += f" Pitch shifts about {pitch_down:g} to +{pitch_up:g} semitones."
    elif role == "Music" and music_silence_ms and any(music_silence_ms):
        how = (
            "Separate from the ambience. After each track the client stays silent for "
            f"{_seconds_window(music_silence_ms[0], music_silence_ms[1])}."
        )
    elif role == "Intro":
        how = "Plays once when you enter the zone."
    else:
        how = "Zone music playlist."

    return {
        "role": role,
        "how": how,
        "volume": volume if volume not in (None, 0, 0.0) else None,
        "looping": looping or role in {"Bed", "Atmosphere"},
        "pitch_down": pitch_down,
        "pitch_up": pitch_up,
        "interval_min_ms": interval_min,
        "interval_max_ms": interval_max,
        "delay_min_ms": delay_min,
        "delay_max_ms": delay_max,
        "fade_in_ms": fade_in,
        "fade_out_ms": fade_out,
        "pos_min": pos_min,
        "pos_max": pos_max,
        "sound_kit_id": entry.sound_kit_id,
        "time_of_day": entry.time_of_day,
        "hint": entry.hint,
    }


def _seconds_window(min_ms: int, max_ms: int) -> str:
    if min_ms <= 0 and max_ms <= 0:
        return ""
    start = (min_ms or max_ms) / 1000
    end = (max_ms or min_ms) / 1000
    if abs(start - end) < 0.05:
        return f"{start:g}s"
    return f"{start:g}–{end:g}s"


def import_defaults_for_entry(
    entry: AudioEntry,
    playback: dict[str, object] | None = None,
) -> dict[str, object]:
    playback = playback or {}
    role = str(playback.get("role") or "")
    volume = playback.get("volume")
    if entry.category == "music" or role in {"Music", "Intro"}:
        return {
            "type": "playlist" if role != "Intro" else "loop",
            "volume": volume if isinstance(volume, (int, float)) and volume else 0.58,
            "shuffle": role != "Intro",
            "repeat": role != "Intro",
            "crossfade_duration": 4.0,
        }

    if role == "Detail" or entry.suggested_layer_type == "intermittent":
        min_delay = (int(playback.get("interval_min_ms") or 0) / 1000) or 8
        max_delay = (int(playback.get("interval_max_ms") or 0) / 1000) or 24
        fade_in = (int(playback.get("fade_in_ms") or 0) / 1000) or 0.15
        fade_out = (int(playback.get("fade_out_ms") or 0) / 1000) or 0.65
        return {
            "type": "intermittent",
            "volume": volume if isinstance(volume, (int, float)) and volume else 0.48,
            "min_delay": min_delay,
            "max_delay": max(min_delay, max_delay),
            "randomize_delay": True,
            "randomize_volume": True,
            "min_volume": 0.75,
            "max_volume": 1.0,
            "fade_in": fade_in,
            "fade_out": fade_out,
        }

    return {
        "type": "loop",
        "volume": volume if isinstance(volume, (int, float)) and volume else 0.52,
        "shuffle": role == "Atmosphere",
        "repeat": True,
        "crossfade_duration": 1.5 if role == "Atmosphere" else 4.0,
    }


class DB2Cache:
    def __init__(self, cache_root: Path, logger):
        self.cache_root = cache_root
        self.logger = logger
        self.current_build: str | None = None
        self._table_rows_cache: dict[tuple[str, str], list[dict[str, str]]] = {}
        self._file_locks: dict[tuple[str, str], threading.Lock] = {}
        self._locks_guard = threading.Lock()

    def set_build(self, build: str) -> None:
        build = build.strip()
        if not build:
            raise ValueError("Build cannot be empty.")
        self.current_build = build

    def _file_lock(self, build: str, table_name: str) -> threading.Lock:
        key = (build, table_name)
        with self._locks_guard:
            lock = self._file_locks.get(key)
            if lock is None:
                lock = threading.Lock()
                self._file_locks[key] = lock
            return lock

    def get_latest_build(self, preferred_major: int | None = None) -> str:
        index_data = parse_wago_db_index()
        versions = index_data.get("props", {}).get("versions", [])
        if not versions:
            raise RuntimeError("No DB2 versions found from Wago.")
        if preferred_major is not None:
            major_text = str(preferred_major)
            for version in versions:
                if str(version).split(".", 1)[0] == major_text:
                    return str(version)
        return str(versions[0])

    def ensure_table_file(self, table_name: str, build: str | None = None) -> Path:
        build = (build or self.current_build or "").strip()
        if not build:
            raise RuntimeError("Build is not set.")

        table_path = self.cache_root / build / f"{table_name}.csv"
        if table_path.exists() and table_path.stat().st_size > 64:
            return table_path

        with self._file_lock(build, table_name):
            if table_path.exists() and table_path.stat().st_size > 64:
                return table_path
            table_path.parent.mkdir(parents=True, exist_ok=True)
            self.logger(f"Downloading table: {table_name} ({build})")
            url = WAGO_DB_TABLE_URL.format(table=table_name, build=build)
            raw = fetch_bytes(url, timeout=240)
            if len(raw) < 20:
                raise RuntimeError(f"Downloaded table is too small: {table_name}")
            temporary = table_path.with_suffix(".csv.part")
            temporary.write_bytes(raw)
            temporary.replace(table_path)
            return table_path

    def read_table_rows(self, table_name: str, build: str | None = None) -> list[dict[str, str]]:
        build = (build or self.current_build or "").strip()
        cache_key = (build, table_name)
        cached = self._table_rows_cache.get(cache_key)
        if cached is not None:
            return cached

        path = self.ensure_table_file(table_name, build)
        with path.open("r", encoding="utf-8", newline="") as handle:
            rows = list(csv.DictReader(handle))
        self._table_rows_cache[cache_key] = rows
        return rows

    def iter_table_rows(self, table_name: str, build: str | None = None):
        path = self.ensure_table_file(table_name, build)
        with path.open("r", encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle)
            for row in reader:
                yield row


class ListfileCache:
    def __init__(self, cache_root: Path, logger):
        self.cache_root = cache_root
        self.logger = logger
        self.listfile_path = self.cache_root / "community-listfile.csv"
        self._lookup_cache: dict[int, str | None] = {}

    def ensure_listfile(self) -> Path:
        self.cache_root.mkdir(parents=True, exist_ok=True)
        if self.listfile_path.exists() and self.listfile_path.stat().st_size > 1024:
            return self.listfile_path

        self.logger("Downloading community WoW listfile for filename lookups...")
        req = urllib.request.Request(WOW_LISTFILE_URL, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=240) as response, self.listfile_path.open("wb") as handle:
            shutil.copyfileobj(response, handle)
        return self.listfile_path

    def lookup_file_name(self, file_data_id: int) -> str | None:
        if file_data_id <= 0:
            return None
        return self.resolve_file_names([file_data_id]).get(file_data_id)

    def resolve_file_names(self, file_data_ids: list[int]) -> dict[int, str | None]:
        wanted = sorted({file_data_id for file_data_id in file_data_ids if file_data_id > 0})
        if not wanted:
            return {}

        missing = [file_data_id for file_data_id in wanted if file_data_id not in self._lookup_cache]
        if missing:
            path = self.ensure_listfile()
            target_ids = set(missing)
            highest_target = missing[-1]
            resolved_count = 0
            self.logger(f"Resolving friendly filenames for {pretty_count(len(missing))} clip IDs...")
            with path.open("r", encoding="utf-8", errors="replace", newline="") as handle:
                for line_number, line in enumerate(handle, start=1):
                    raw = line.strip()
                    if not raw or ";" not in raw:
                        continue
                    raw_id, raw_name = raw.split(";", 1)
                    try:
                        current_id = int(raw_id)
                    except ValueError:
                        continue

                    if current_id > highest_target:
                        break

                    if current_id in target_ids:
                        self._lookup_cache[current_id] = raw_name
                        resolved_count += 1
                        if resolved_count >= len(target_ids):
                            break

                    if line_number % 750000 == 0:
                        self.logger(
                            f"  scanned {pretty_count(line_number)} listfile rows "
                            f"({pretty_count(resolved_count)}/{pretty_count(len(target_ids))} matches)"
                        )

            for file_data_id in missing:
                self._lookup_cache.setdefault(file_data_id, None)
            self.logger(
                f"Resolved friendly filenames for {pretty_count(sum(1 for file_data_id in missing if self._lookup_cache.get(file_data_id)))} "
                f"of {pretty_count(len(missing))} requested IDs."
            )

        return {file_data_id: self._lookup_cache.get(file_data_id) for file_data_id in wanted}


class ZoneAudioResolver:
    def __init__(self, db: DB2Cache, logger):
        self.db = db
        self.logger = logger

        self._zones: list[ZoneInfo] | None = None
        self._zone_by_id: dict[int, ZoneInfo] = {}
        self._child_zone_ids: dict[int, list[int]] = {}  # parent_area_id -> [child_area_ids]
        self._zone_music_map: dict[int, dict[str, str]] | None = None
        self._zone_intro_map: dict[int, dict[str, str]] | None = None
        self._sound_ambience_map: dict[int, dict[str, str]] | None = None
        self._ambience_flavor_map: dict[int, list[dict[str, str]]] | None = None
        self._soundkit_to_files: dict[int, list[int]] | None = None
        self._kit_profiles: dict[int, dict[str, object]] | None = None
        self._audio_index_ready = False
        self._table_build = ""
        self._index_lock = threading.Lock()

    @property
    def audio_index_ready(self) -> bool:
        return self._audio_index_ready

    def clear_for_reload(self) -> None:
        with self._index_lock:
            self._zones = None
            self._zone_by_id = {}
            self._child_zone_ids = {}
            self._zone_music_map = None
            self._zone_intro_map = None
            self._sound_ambience_map = None
            self._ambience_flavor_map = None
            self._soundkit_to_files = None
            self._kit_profiles = None
            self._audio_index_ready = False

    def install_zones(self, zones: list[ZoneInfo]) -> list[ZoneInfo]:
        self._zones = list(zones)
        self._zone_by_id = {zone.area_id: zone for zone in self._zones}
        child_map: dict[int, list[int]] = defaultdict(list)
        for zone in self._zones:
            if zone.parent_area_id > 0:
                child_map[zone.parent_area_id].append(zone.area_id)
        self._child_zone_ids = dict(child_map)
        self.logger(f"Built parent->child index: {pretty_count(len(child_map))} parent zones with sub-areas.")
        return self._zones

    def install_audio_index(self, payload: dict) -> bool:
        if not isinstance(payload, dict) or payload.get("v") != 1:
            return False
        needed = (
            "zone_music_map",
            "zone_intro_map",
            "sound_ambience_map",
            "ambience_flavor_map",
            "soundkit_to_files",
            "kit_profiles",
        )
        if any(name not in payload or not isinstance(payload[name], dict) for name in needed):
            return False
        with self._index_lock:
            self._zone_music_map = payload["zone_music_map"]
            self._zone_intro_map = payload["zone_intro_map"]
            self._sound_ambience_map = payload["sound_ambience_map"]
            self._ambience_flavor_map = payload["ambience_flavor_map"]
            self._soundkit_to_files = payload["soundkit_to_files"]
            self._kit_profiles = payload["kit_profiles"]
            self._audio_index_ready = True
        return True

    def load_zones(self, build: str) -> list[ZoneInfo]:
        self._table_build = build
        if self._zones is not None:
            return self._zones

        rows = self.db.read_table_rows("AreaTable", build)
        area_labels: dict[int, str] = {}
        for row in rows:
            area_id = _to_int(row.get("ID"))
            if area_id <= 0:
                continue
            area_name = (row.get("AreaName_lang") or "").strip()
            zone_name = (row.get("ZoneName") or "").strip()
            label = area_name or zone_name
            if label:
                area_labels[area_id] = label

        zones: list[ZoneInfo] = []
        for row in rows:
            area_id = _to_int(row.get("ID"))
            if area_id <= 0:
                continue

            area_name = (row.get("AreaName_lang") or "").strip()
            zone_name = (row.get("ZoneName") or "").strip()
            label = area_name or zone_name
            if not label:
                continue

            ambience_id = _to_int(row.get("AmbienceID"))
            zone_music = _to_int(row.get("ZoneMusic"))
            uw_zone_music = _to_int(row.get("UwZoneMusic"))
            intro_sound = _to_int(row.get("IntroSound"))
            uw_intro_sound = _to_int(row.get("UwIntroSound"))

            if not any([ambience_id, zone_music, uw_zone_music, intro_sound, uw_intro_sound]):
                continue

            zones.append(
                ZoneInfo(
                    area_id=area_id,
                    area_name=label,
                    zone_name=zone_name,
                    parent_area_id=_to_int(row.get("ParentAreaID")),
                    parent_area_name=area_labels.get(_to_int(row.get("ParentAreaID")), ""),
                    ambience_id=ambience_id,
                    zone_music_id=zone_music,
                    uw_zone_music_id=uw_zone_music,
                    intro_sound_id=intro_sound,
                    uw_intro_sound_id=uw_intro_sound,
                )
            )

        zones.sort(key=lambda z: (z.area_name.lower(), z.area_id))
        return self.install_zones(zones)

    def _ensure_maps_for_music(self) -> None:
        if self._zone_music_map is not None and self._zone_intro_map is not None:
            return
        with self._index_lock:
            if self._zone_music_map is None:
                self.logger("Indexing ZoneMusic...")
                rows = self.db.read_table_rows("ZoneMusic", self._table_build)
                self._zone_music_map = {_to_int(row["ID"]): row for row in rows if _to_int(row.get("ID")) > 0}
            if self._zone_intro_map is None:
                self.logger("Indexing ZoneIntroMusicTable...")
                rows = self.db.read_table_rows("ZoneIntroMusicTable", self._table_build)
                self._zone_intro_map = {_to_int(row["ID"]): row for row in rows if _to_int(row.get("ID")) > 0}

    def _ensure_maps_for_ambient(self) -> None:
        if self._sound_ambience_map is not None and self._ambience_flavor_map is not None:
            return
        with self._index_lock:
            if self._sound_ambience_map is None:
                self.logger("Indexing SoundAmbience...")
                rows = self.db.read_table_rows("SoundAmbience", self._table_build)
                self._sound_ambience_map = {_to_int(row["ID"]): row for row in rows if _to_int(row.get("ID")) > 0}
            if self._ambience_flavor_map is None:
                self.logger("Indexing SoundAmbienceFlavor...")
                rows = self.db.read_table_rows("SoundAmbienceFlavor", self._table_build)
                mapping: dict[int, list[dict[str, str]]] = defaultdict(list)
                for row in rows:
                    amb_id = _to_int(row.get("SoundAmbienceID"))
                    if amb_id > 0:
                        mapping[amb_id].append(row)
                self._ambience_flavor_map = dict(mapping)

    def _ensure_soundkit_entry_index(self) -> None:
        if self._soundkit_to_files is not None:
            return
        with self._index_lock:
            if self._soundkit_to_files is not None:
                return
            self.logger("Indexing SoundKitEntry (this can take a moment)...")
            mapping: dict[int, set[int]] = defaultdict(set)
            count = 0
            for row in self.db.iter_table_rows("SoundKitEntry", self._table_build):
                soundkit_id = _to_int(row.get("SoundKitID"))
                file_data_id = _to_int(row.get("FileDataID"))
                if soundkit_id > 0 and file_data_id > 0:
                    mapping[soundkit_id].add(file_data_id)
                count += 1
                if count % 200000 == 0:
                    self.logger(f"  ... indexed {pretty_count(count)} SoundKitEntry rows ({pretty_count(len(mapping))} kits)")

            final_map: dict[int, list[int]] = {}
            for soundkit_id, file_ids in mapping.items():
                final_map[soundkit_id] = sorted(file_ids)
            self._soundkit_to_files = final_map
            self.logger(f"SoundKitEntry index complete: {pretty_count(len(final_map))} sound kits, {pretty_count(count)} rows.")

    def preload_all_tables(self, build: str) -> None:
        """Index one build's cached DB2 tables. A saved file is never downloaded again."""
        self._table_build = build
        self.logger(f"Reading cached tables for {build}.")
        self._ensure_maps_for_music()
        self._ensure_maps_for_ambient()
        self._ensure_soundkit_entry_index()
        self._ensure_kit_profiles()
        with self._index_lock:
            self._audio_index_ready = bool(self._soundkit_to_files) and bool(self._kit_profiles)
        if self._audio_index_ready:
            self.logger("All lookup tables loaded. This index is saved for the next launch.")

    def export_audio_index(self) -> dict | None:
        with self._index_lock:
            if not self._audio_index_ready:
                return None
            return {
                "v": 1,
                "zone_music_map": self._zone_music_map,
                "zone_intro_map": self._zone_intro_map,
                "sound_ambience_map": self._sound_ambience_map,
                "ambience_flavor_map": self._ambience_flavor_map,
                "soundkit_to_files": self._soundkit_to_files,
                "kit_profiles": self._kit_profiles,
            }

    def _ensure_kit_profiles(self) -> None:
        if self._kit_profiles is not None:
            return
        with self._index_lock:
            if self._kit_profiles is not None:
                return
            self._kit_profiles = {}
            try:
                self.logger("Indexing SoundKit and SoundKitAdvanced...")
                advanced: dict[int, dict[str, str]] = {}
                for row in self.db.iter_table_rows("SoundKitAdvanced", self._table_build):
                    advanced_id = _to_int(row.get("ID"))
                    if advanced_id > 0:
                        advanced[advanced_id] = row
                count = 0
                for row in self.db.iter_table_rows("SoundKit", self._table_build):
                    soundkit_id = _to_int(row.get("ID"))
                    if soundkit_id <= 0:
                        continue
                    advanced_row = advanced.get(_to_int(row.get("SoundKitAdvancedID")))
                    self._kit_profiles[soundkit_id] = profile_from_kit_rows(row, advanced_row)
                    count += 1
                self.logger(f"Sound kit profiles ready: {pretty_count(count)} kits.")
            except Exception as exc:  # noqa: BLE001
                self.logger(f"Sound kit profiles unavailable: {exc}")

    def kit_profile(self, soundkit_id: int) -> dict[str, object]:
        self._ensure_kit_profiles()
        assert self._kit_profiles is not None
        return dict(self._kit_profiles.get(soundkit_id) or {})

    def music_silence_ms(self, zone_music_id: int) -> tuple[int, int] | None:
        self._ensure_maps_for_music()
        assert self._zone_music_map is not None
        row = self._zone_music_map.get(zone_music_id)
        if not row:
            return None
        return (
            _non_negative_ms(row.get("SilenceIntervalMin_0")),
            _non_negative_ms(row.get("SilenceIntervalMax_0")),
        )

    def playback_for(self, entry: AudioEntry) -> dict[str, object]:
        silence = None
        if entry.source_type == "ZoneMusic":
            silence = self.music_silence_ms(entry.source_id)
        return describe_playback(entry, self.kit_profile(entry.sound_kit_id), silence)

    def _remember_kit(
        self,
        kit_sources: dict[int, tuple[str, int, str]],
        soundkit_id: int,
        source_type: str,
        source_id: int,
        hint: str,
    ) -> None:
        if soundkit_id <= 0:
            return
        current = kit_sources.get(soundkit_id)
        if current is None:
            kit_sources[soundkit_id] = (source_type, source_id, hint)
            return
        previous_type, previous_id, previous_hint = current
        kit_sources[soundkit_id] = (previous_type, previous_id, merge_time_hint(previous_hint, hint))

    def _music_entries_for_zone(
        self,
        zone: ZoneInfo,
        include_playlist: bool = True,
        include_intro: bool = True,
    ) -> list[AudioEntry]:
        self._ensure_maps_for_music()
        self._ensure_soundkit_entry_index()
        assert self._zone_music_map is not None
        assert self._zone_intro_map is not None
        assert self._soundkit_to_files is not None

        kit_sources: dict[int, tuple[str, int, str]] = {}

        for zone_music_id in ((zone.zone_music_id, zone.uw_zone_music_id) if include_playlist else ()):
            if zone_music_id <= 0:
                continue
            row = self._zone_music_map.get(zone_music_id)
            if not row:
                continue
            set_name = (row.get("SetName") or "").strip()
            for key, value in row.items():
                if not key.startswith("Sounds_"):
                    continue
                soundkit_id = _to_int(value)
                self._remember_kit(kit_sources, soundkit_id, "ZoneMusic", zone_music_id, set_name or key)

        for intro_id in ((zone.intro_sound_id, zone.uw_intro_sound_id) if include_intro else ()):
            if intro_id <= 0:
                continue
            row = self._zone_intro_map.get(intro_id)
            if not row:
                continue
            soundkit_id = _to_int(row.get("SoundID"))
            intro_name = (row.get("Name") or "").strip()
            self._remember_kit(kit_sources, soundkit_id, "ZoneIntroMusicTable", intro_id, intro_name or "intro")

        entries: list[AudioEntry] = []
        seen_file_ids: set[int] = set()
        for soundkit_id in sorted(kit_sources):
            source_type, source_id, hint = kit_sources[soundkit_id]
            for file_data_id in self._soundkit_to_files.get(soundkit_id, []):
                if file_data_id in seen_file_ids:
                    continue
                seen_file_ids.add(file_data_id)
                entries.append(
                    AudioEntry(
                        zone_id=zone.area_id,
                        zone_name=zone.friendly_name,
                        category="music",
                        sound_kit_id=soundkit_id,
                        file_data_id=file_data_id,
                        source_type=source_type,
                        source_id=source_id,
                        hint=hint,
                    )
                )
        return entries

    def _ambient_entries_for_zone(self, zone: ZoneInfo) -> list[AudioEntry]:
        if zone.ambience_id <= 0:
            return []

        self._ensure_maps_for_ambient()
        self._ensure_soundkit_entry_index()
        assert self._sound_ambience_map is not None
        assert self._ambience_flavor_map is not None
        assert self._soundkit_to_files is not None

        kit_sources: dict[int, tuple[str, int, str]] = {}
        row = self._sound_ambience_map.get(zone.ambience_id)
        if row:
            # Capture ALL soundkit columns from SoundAmbience:
            #   AmbienceID_0/1      = primary ambient loops (forest, nature, etc.)
            #   AmbienceStartID_0/1 = transition-in sounds
            #   AmbienceStopID_0/1  = transition-out sounds
            #   SoundKitID_0/1      = atmospheric/wind loops
            ambience_column_hints = {
                "AmbienceID_0": "day ambient loop",
                "AmbienceID_1": "night ambient loop",
                "AmbienceStartID_0": "day ambient start",
                "AmbienceStartID_1": "night ambient start",
                "AmbienceStopID_0": "day ambient stop",
                "AmbienceStopID_1": "night ambient stop",
                "SoundKitID_0": "day atmosphere",
                "SoundKitID_1": "night atmosphere",
            }
            for col_name, hint in ambience_column_hints.items():
                soundkit_id = _to_int(row.get(col_name))
                self._remember_kit(kit_sources, soundkit_id, "SoundAmbience", zone.ambience_id, hint)

        flavor_rows = self._ambience_flavor_map.get(zone.ambience_id, [])
        for flavor_row in flavor_rows:
            for key, hint in (("SoundEntriesIDDay", "day layer"), ("SoundEntriesIDNight", "night layer")):
                soundkit_id = _to_int(flavor_row.get(key))
                self._remember_kit(kit_sources, soundkit_id, "SoundAmbienceFlavor", zone.ambience_id, hint)

        entries: list[AudioEntry] = []
        seen_file_ids: set[int] = set()
        for soundkit_id in sorted(kit_sources):
            source_type, source_id, hint = kit_sources[soundkit_id]
            for file_data_id in self._soundkit_to_files.get(soundkit_id, []):
                if file_data_id in seen_file_ids:
                    continue
                seen_file_ids.add(file_data_id)
                entries.append(
                    AudioEntry(
                        zone_id=zone.area_id,
                        zone_name=zone.friendly_name,
                        category="ambient sounds",
                        sound_kit_id=soundkit_id,
                        file_data_id=file_data_id,
                        source_type=source_type,
                        source_id=source_id,
                        hint=hint,
                    )
                )
        return entries

    def entries_for_zone(
        self,
        zone: ZoneInfo,
        include_music: bool,
        include_ambient: bool,
        include_foreign_child_music: bool = False,
    ) -> list[AudioEntry]:
        output: list[AudioEntry] = []
        seen_file_ids: set[int] = set()

        def _add_entries(target_zone: ZoneInfo, allow_music: bool, allow_intro: bool) -> None:
            if include_music and (allow_music or allow_intro):
                for entry in self._music_entries_for_zone(
                    target_zone,
                    include_playlist=allow_music,
                    include_intro=allow_intro,
                ):
                    if entry.file_data_id not in seen_file_ids:
                        seen_file_ids.add(entry.file_data_id)
                        output.append(entry)
            if include_ambient:
                for entry in self._ambient_entries_for_zone(target_zone):
                    if entry.file_data_id not in seen_file_ids:
                        seen_file_ids.add(entry.file_data_id)
                        output.append(entry)

        _add_entries(zone, True, True)

        # Child areas add their own ambience. Their music is included when the
        # parent has none, or when the user asks for other subzone music.
        # A child that only swaps in a different playlist used to bury the
        # zone you actually selected.
        child_ids = self._child_zone_ids.get(zone.area_id, [])
        for child_id in child_ids:
            child_zone = self._zone_by_id.get(child_id)
            if not child_zone:
                continue
            same_playlist = child_zone.zone_music_id in (0, zone.zone_music_id)
            allow_music = include_foreign_child_music or zone.zone_music_id == 0 or same_playlist
            same_intro = child_zone.intro_sound_id in (0, zone.intro_sound_id)
            allow_intro = include_foreign_child_music or zone.intro_sound_id == 0 or same_intro
            _add_entries(child_zone, allow_music, allow_intro)

        output.sort(key=lambda item: (item.category, item.file_data_id))
        return output


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(f"{APP_TITLE} {APP_VERSION}")
        self.geometry("1480x900")
        self.minsize(1180, 720)
        self.configure(bg="#0C1219")

        self.ui_queue: queue.Queue = queue.Queue()
        self.worker_thread: threading.Thread | None = None
        self.zones: list[ZoneInfo] = []
        self.filtered_indices: list[int] = []
        self.asset_entries: list[AudioEntry] = []
        self.asset_entry_map: dict[str, AudioEntry] = {}
        self.entry_overrides: dict[str, dict[str, object]] = {}
        self.selected_entry_key: str | None = None
        self.zone_refresh_after_id: str | None = None
        self._bridge_job: dict | None = None
        self._agent_seen_at = 0.0
        self._index_generation = 0
        self._index_restore_started = False
        self._index_restore_build = ""
        self._index_payloads: dict[str, dict] = {}
        self._index_threads: dict[str, threading.Thread] = {}
        self._loaded_build = ""

        local_app_data = Path(os.getenv("LOCALAPPDATA", str(Path.home())))
        self.cache_root = local_app_data / "WoWZoneAudioExtractor" / "cache"
        self.preview_cache = local_app_data / "WoWZoneAudioExtractor" / "preview_cache"
        self.preview_cache.mkdir(parents=True, exist_ok=True)
        self.db = DB2Cache(self.cache_root, logger=self._log_from_worker)
        self.listfile_cache = ListfileCache(local_app_data / "WoWZoneAudioExtractor" / "listfile_cache", logger=self._log_from_worker)
        self.resolver = ZoneAudioResolver(self.db, logger=self._log_from_worker)

        self.wow_path_var = tk.StringVar()
        self.output_path_var = tk.StringVar(value=r"F:\WoW-Ambient")
        self.build_var = tk.StringVar()
        self.version_var = tk.StringVar()
        self._installs: list[WowInstall] = []
        self._install_by_choice: dict[str, WowInstall] = {}
        self.search_var = tk.StringVar()
        self.extract_music_var = tk.BooleanVar(value=True)
        self.extract_ambient_var = tk.BooleanVar(value=True)
        self.include_subzone_music_var = tk.BooleanVar(value=False)
        self.format_var = tk.StringVar(value="mp3")
        self.asset_search_var = tk.StringVar()
        self.asset_type_var = tk.StringVar(value="All")
        self.asset_time_var = tk.StringVar(value="All")
        self.asset_mood_var = tk.StringVar(value="All")
        self.asset_role_var = tk.StringVar(value="All")
        self.asset_tagged_only_var = tk.BooleanVar(value=False)
        self.status_var = tk.StringVar(value="Ready.")
        self.progress_var = tk.DoubleVar(value=0.0)
        self.zone_summary_var = tk.StringVar(value="Load zones to begin browsing.")
        self.asset_summary_var = tk.StringVar(value="Select one or more zones to retrieve audio.")
        self.selection_summary_var = tk.StringVar(value="No asset selected.")
        self.preview_status_var = tk.StringVar(value="Preview idle.")
        self.meta_area_var = tk.StringVar(value="-")
        self.meta_type_var = tk.StringVar(value="-")
        self.meta_time_var = tk.StringVar(value="-")
        self.meta_mood_var = tk.StringVar(value="-")
        self.meta_source_var = tk.StringVar(value="-")
        self.meta_file_data_var = tk.StringVar(value="-")
        self.meta_play_var = tk.StringVar(value="Select a clip to see how the client plays it.")
        self.agent_status_var = tk.StringVar(value=f"Agent listening on 127.0.0.1:{AGENT_PORT}")
        self.recipe_var = tk.StringVar(value="")

        self._apply_style()
        self._build_ui()
        self._scan_installs(restore=False)
        self._restore_remembered_zones()
        self._start_agent_bridge()
        self.after(120, self._poll_queue)

    def _apply_style(self) -> None:
        style = ttk.Style(self)
        style.theme_use("clam")

        # -- Base palette --
        bg_deep = "#080D14"
        bg_base = "#0C1219"
        bg_card = "#111A26"
        bg_field = "#0A1320"
        bg_hover = "#162940"
        accent = "#2DD4BF"        # teal-400
        accent_dim = "#0F766E"    # teal-700
        accent_glow = "#14B8A6"   # teal-500
        fg_primary = "#E8F0FA"
        fg_secondary = "#94A8C4"
        fg_muted = "#5E7591"
        fg_heading = "#B8D4F0"
        select_bg = "#1A4A6E"
        select_fg = "#FFFFFF"
        border_subtle = "#1C2A3E"
        danger = "#F87171"

        style.configure("TFrame", background=bg_base)
        style.configure("Card.TFrame", background=bg_card)
        style.configure("TLabel", background=bg_base, foreground=fg_primary, font=("Segoe UI", 10))
        style.configure("Title.TLabel", background=bg_base, foreground=accent, font=("Segoe UI Semibold", 20))
        style.configure("Subtitle.TLabel", background=bg_base, foreground=fg_secondary, font=("Segoe UI", 10))
        style.configure("CardTitle.TLabel", background=bg_card, foreground=fg_heading, font=("Segoe UI Semibold", 11))
        style.configure("SectionHeader.TLabel", background=bg_card, foreground=accent, font=("Segoe UI Semibold", 12))
        style.configure("Summary.TLabel", background=bg_card, foreground=fg_secondary, font=("Segoe UI", 9))
        style.configure("Meta.TLabel", background=bg_card, foreground=fg_muted, font=("Segoe UI", 9))
        style.configure("SectionValue.TLabel", background=bg_field, foreground=fg_primary, font=("Segoe UI", 10), padding=(6, 3))
        style.configure("Badge.TLabel", background=accent_dim, foreground=fg_primary, font=("Segoe UI Semibold", 9), padding=(6, 2))
        style.configure("Status.TLabel", background=bg_card, foreground=fg_secondary, font=("Segoe UI", 9))

        # Buttons
        style.configure("TButton", font=("Segoe UI", 10), padding=(10, 6), background=bg_hover, foreground=fg_primary)
        style.map("TButton", background=[("active", "#1E3A56"), ("!disabled", bg_hover)], foreground=[("disabled", fg_muted)])
        style.configure("Accent.TButton", font=("Segoe UI Semibold", 10), padding=(14, 7), background=accent_dim, foreground=fg_primary)
        style.map("Accent.TButton", background=[("active", accent_glow), ("!disabled", accent_dim)], foreground=[("disabled", fg_muted)])
        style.configure("Danger.TButton", font=("Segoe UI", 10), padding=(10, 6), background="#7F1D1D", foreground=fg_primary)
        style.map("Danger.TButton", background=[("active", danger), ("!disabled", "#7F1D1D")])

        # Check / Radio
        style.configure("TCheckbutton", background=bg_card, foreground=fg_primary, font=("Segoe UI", 10))
        style.map("TCheckbutton", background=[("active", bg_card)])

        # Entry / Combobox
        style.configure("TEntry", fieldbackground=bg_field, foreground=fg_primary, insertcolor=accent, font=("Segoe UI", 10))
        style.configure("TCombobox", fieldbackground=bg_field, foreground=fg_primary, font=("Segoe UI", 10))
        style.map(
            "TCombobox",
            fieldbackground=[("readonly", bg_field), ("disabled", bg_deep)],
            background=[("readonly", bg_hover), ("active", bg_hover)],
            foreground=[("readonly", fg_primary), ("disabled", fg_muted)],
            arrowcolor=[("readonly", accent), ("disabled", fg_muted)],
            selectbackground=[("readonly", select_bg)],
            selectforeground=[("readonly", select_fg)],
        )

        # Progress
        style.configure("TProgressbar", troughcolor=bg_deep, background=accent_glow, thickness=6)

        # Treeview
        style.configure("Treeview", background=bg_field, fieldbackground=bg_field, foreground=fg_primary, rowheight=26, font=("Segoe UI", 10))
        style.configure("Treeview.Heading", background=border_subtle, foreground=fg_heading, font=("Segoe UI Semibold", 10), padding=(4, 4))
        style.map("Treeview", background=[("selected", select_bg)], foreground=[("selected", select_fg)])
        style.map("Treeview.Heading", background=[("active", bg_hover)])

        # Combobox dropdown
        self.option_add("*TCombobox*Listbox.background", bg_field)
        self.option_add("*TCombobox*Listbox.foreground", fg_primary)
        self.option_add("*TCombobox*Listbox.selectBackground", select_bg)
        self.option_add("*TCombobox*Listbox.selectForeground", select_fg)
        self.option_add("*TCombobox*Listbox.font", ("Segoe UI", 10))

        # Separator
        style.configure("TSeparator", background=border_subtle)

        # Store palette for widget creation
        self._palette = {
            "bg_deep": bg_deep, "bg_base": bg_base, "bg_card": bg_card,
            "bg_field": bg_field, "bg_hover": bg_hover,
            "accent": accent, "accent_dim": accent_dim, "accent_glow": accent_glow,
            "fg_primary": fg_primary, "fg_secondary": fg_secondary, "fg_muted": fg_muted,
            "fg_heading": fg_heading, "select_bg": select_bg, "select_fg": select_fg,
            "border_subtle": border_subtle,
        }

    def _build_ui(self) -> None:
        p = self._palette
        root = ttk.Frame(self, padding=(16, 12))
        root.pack(fill=tk.BOTH, expand=True)
        root.rowconfigure(3, weight=1)
        root.columnconfigure(0, weight=1)

        # ── Header ──
        hdr = ttk.Frame(root)
        hdr.grid(row=0, column=0, sticky="ew", pady=(0, 4))
        ttk.Label(hdr, text=APP_TITLE, style="Title.TLabel").pack(side=tk.LEFT)
        ttk.Label(hdr, text=f"v{APP_VERSION}", style="Subtitle.TLabel").pack(side=tk.LEFT, padx=(8, 0), pady=(6, 0))
        ttk.Label(hdr, textvariable=self.agent_status_var, style="Status.TLabel").pack(side=tk.RIGHT)
        ttk.Label(
            root,
            text="A zone is a bed, plus atmosphere and one-shots on their own clocks. Music is a fourth clock.",
            style="Subtitle.TLabel",
        ).grid(row=1, column=0, sticky=tk.W, pady=(0, 10))

        # ── Compact Config Bar ──
        config = ttk.Frame(root, style="Card.TFrame", padding=(12, 8))
        config.grid(row=2, column=0, sticky="ew", pady=(0, 10))
        config.columnconfigure(1, weight=1)
        config.columnconfigure(4, weight=1)

        ttk.Label(config, text="WoW", style="CardTitle.TLabel").grid(row=0, column=0, sticky=tk.W, padx=(0, 6))
        ttk.Entry(config, textvariable=self.wow_path_var).grid(row=0, column=1, sticky="ew", padx=(0, 4))
        ttk.Button(config, text="Browse", command=self._browse_wow).grid(row=0, column=2, padx=2)
        ttk.Button(config, text="Detect", command=self._detect_clicked).grid(row=0, column=3, padx=(2, 12))
        ttk.Label(config, text="Output", style="CardTitle.TLabel").grid(row=0, column=4, sticky=tk.W, padx=(0, 6))
        # Re-use column 4 is the label; field goes in 5
        ttk.Entry(config, textvariable=self.output_path_var).grid(row=0, column=5, sticky="ew", padx=(0, 4))
        config.columnconfigure(5, weight=1)
        ttk.Button(config, text="Browse", command=self._browse_output).grid(row=0, column=6, padx=2)

        row2 = ttk.Frame(config, style="Card.TFrame")
        row2.grid(row=1, column=0, columnspan=7, sticky="ew", pady=(6, 0))
        ttk.Label(row2, text="Version", style="CardTitle.TLabel").pack(side=tk.LEFT, padx=(0, 6))
        self.version_combo = ttk.Combobox(row2, textvariable=self.version_var, width=32, state="readonly")
        self.version_combo.pack(side=tk.LEFT, padx=(0, 8))
        self.version_combo.bind("<<ComboboxSelected>>", self._on_version_selected)
        ttk.Button(row2, text="⟡ Load Zones", style="Accent.TButton", command=self._load_zones_clicked).pack(side=tk.LEFT, padx=(0, 16))
        ttk.Separator(row2, orient=tk.VERTICAL).pack(side=tk.LEFT, fill=tk.Y, padx=8, pady=2)
        ttk.Checkbutton(row2, text="Music", variable=self.extract_music_var).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Checkbutton(row2, text="Ambient", variable=self.extract_ambient_var).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Checkbutton(row2, text="Subzone music", variable=self.include_subzone_music_var).pack(side=tk.LEFT, padx=(0, 12))
        ttk.Label(row2, text="Format", style="CardTitle.TLabel").pack(side=tk.LEFT, padx=(0, 4))
        ttk.Combobox(row2, textvariable=self.format_var, values=("mp3", "wav", "m4a"), width=5, state="readonly").pack(side=tk.LEFT)

        # ── Three-Panel Body ──
        body = ttk.Panedwindow(root, orient=tk.HORIZONTAL)
        body.grid(row=3, column=0, sticky="nsew")

        left = ttk.Frame(body, style="Card.TFrame", padding=10)
        center = ttk.Frame(body, style="Card.TFrame", padding=10)
        right = ttk.Frame(body, style="Card.TFrame", padding=10)
        body.add(left, weight=2)
        body.add(center, weight=5)
        body.add(right, weight=3)

        # ── LEFT: Zone Browser ──
        ttk.Label(left, text="🌍  Zones", style="SectionHeader.TLabel").pack(anchor=tk.W)
        search_frame = ttk.Frame(left, style="Card.TFrame")
        search_frame.pack(fill=tk.X, pady=(8, 6))
        zone_search = ttk.Entry(search_frame, textvariable=self.search_var)
        zone_search.pack(side=tk.LEFT, fill=tk.X, expand=True)
        zone_search.bind("<KeyRelease>", lambda _e: self._apply_zone_filter())

        zone_list_frame = ttk.Frame(left, style="Card.TFrame")
        zone_list_frame.pack(fill=tk.BOTH, expand=True, pady=(0, 6))
        self.zone_listbox = tk.Listbox(
            zone_list_frame,
            selectmode=tk.EXTENDED,
            bg=p["bg_field"],
            fg=p["fg_primary"],
            selectbackground=p["select_bg"],
            selectforeground=p["select_fg"],
            activestyle="none",
            font=("Segoe UI", 10),
            relief=tk.FLAT,
            highlightthickness=0,
            borderwidth=0,
        )
        self.zone_listbox.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        zone_scroll = ttk.Scrollbar(zone_list_frame, orient=tk.VERTICAL, command=self.zone_listbox.yview)
        zone_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.zone_listbox.configure(yscrollcommand=zone_scroll.set)
        self.zone_listbox.bind("<<ListboxSelect>>", lambda _e: self._schedule_asset_refresh())

        zone_buttons = ttk.Frame(left, style="Card.TFrame")
        zone_buttons.pack(fill=tk.X)
        ttk.Button(zone_buttons, text="⟡ Retrieve Audio", style="Accent.TButton", command=self._load_assets_for_selected_zones).pack(side=tk.LEFT)
        ttk.Button(zone_buttons, text="Select All", command=self._select_all_filtered).pack(side=tk.LEFT, padx=(6, 0))
        ttk.Button(zone_buttons, text="Clear", command=self._clear_zone_selection).pack(side=tk.LEFT, padx=(6, 0))
        ttk.Label(left, textvariable=self.zone_summary_var, style="Summary.TLabel", wraplength=280).pack(anchor=tk.W, pady=(8, 0))

        # ── CENTER: Audio Explorer ──
        ttk.Label(center, text="🎵  Audio Explorer", style="SectionHeader.TLabel").pack(anchor=tk.W)

        # Filter row
        filter_bar = ttk.Frame(center, style="Card.TFrame")
        filter_bar.pack(fill=tk.X, pady=(8, 6))
        asset_search = ttk.Entry(filter_bar, textvariable=self.asset_search_var, width=20)
        asset_search.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 8))
        asset_search.bind("<KeyRelease>", lambda _e: self._render_asset_tree())
        ttk.Label(filter_bar, text="Type", style="Meta.TLabel").pack(side=tk.LEFT, padx=(0, 3))
        self.asset_type_combo = ttk.Combobox(filter_bar, textvariable=self.asset_type_var, width=14, state="readonly")
        self.asset_type_combo.pack(side=tk.LEFT, padx=(0, 6))
        self.asset_type_combo.bind("<<ComboboxSelected>>", lambda _e: self._render_asset_tree())
        ttk.Label(filter_bar, text="Time", style="Meta.TLabel").pack(side=tk.LEFT, padx=(0, 3))
        self.asset_time_combo = ttk.Combobox(filter_bar, textvariable=self.asset_time_var, width=10, state="readonly")
        self.asset_time_combo.pack(side=tk.LEFT, padx=(0, 6))
        self.asset_time_combo.bind("<<ComboboxSelected>>", lambda _e: self._render_asset_tree())
        ttk.Label(filter_bar, text="Role", style="Meta.TLabel").pack(side=tk.LEFT, padx=(0, 3))
        self.asset_role_combo = ttk.Combobox(filter_bar, textvariable=self.asset_role_var, width=12, state="readonly")
        self.asset_role_combo.pack(side=tk.LEFT, padx=(0, 6))
        self.asset_role_combo.bind("<<ComboboxSelected>>", lambda _e: self._render_asset_tree())
        ttk.Checkbutton(filter_bar, text="★ Tagged", variable=self.asset_tagged_only_var, command=self._render_asset_tree).pack(side=tk.LEFT, padx=(4, 0))

        # Treeview
        tree_frame = ttk.Frame(center, style="Card.TFrame")
        tree_frame.pack(fill=tk.BOTH, expand=True)
        asset_columns = ("extract", "role", "time", "volume", "tagged")
        self.asset_tree = ttk.Treeview(tree_frame, columns=asset_columns, show="tree headings", selectmode="extended")
        self.asset_tree.heading("#0", text="Layer / Clip", anchor=tk.W)
        self.asset_tree.heading("extract", text="Extract", anchor=tk.CENTER)
        self.asset_tree.heading("role", text="Role")
        self.asset_tree.heading("time", text="Time")
        self.asset_tree.heading("volume", text="Vol")
        self.asset_tree.heading("tagged", text="Tag")
        self.asset_tree.column("#0", width=360, anchor=tk.W)
        self.asset_tree.column("extract", width=64, anchor=tk.CENTER)
        self.asset_tree.column("role", width=100, anchor=tk.W)
        self.asset_tree.column("time", width=70, anchor=tk.CENTER)
        self.asset_tree.column("volume", width=50, anchor=tk.CENTER)
        self.asset_tree.column("tagged", width=40, anchor=tk.CENTER)
        self.asset_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        tree_scroll = ttk.Scrollbar(tree_frame, orient=tk.VERTICAL, command=self.asset_tree.yview)
        tree_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.asset_tree.configure(yscrollcommand=tree_scroll.set)
        self.asset_tree.bind("<<TreeviewSelect>>", lambda _e: self._on_asset_tree_select())
        self.asset_tree.bind("<Button-1>", self._handle_asset_tree_click)
        self.asset_tree.bind("<Double-1>", self._handle_asset_tree_double_click)
        self.asset_tree.bind("<space>", self._toggle_checked_for_tree_selection)

        ttk.Label(center, textvariable=self.asset_summary_var, style="Summary.TLabel", wraplength=620).pack(anchor=tk.W, pady=(6, 2))
        ttk.Label(center, textvariable=self.recipe_var, style="Summary.TLabel", wraplength=620).pack(anchor=tk.W, pady=(0, 6))

        # Action bar
        asset_actions = ttk.Frame(center, style="Card.TFrame")
        asset_actions.pack(fill=tk.X)
        ttk.Button(asset_actions, text="☑ Check Visible", command=self._check_visible_entries).pack(side=tk.LEFT)
        ttk.Button(asset_actions, text="☐ Uncheck Visible", command=self._uncheck_visible_entries).pack(side=tk.LEFT, padx=(6, 0))
        ttk.Separator(asset_actions, orient=tk.VERTICAL).pack(side=tk.LEFT, fill=tk.Y, padx=10, pady=2)
        ttk.Button(asset_actions, text="⟡ Extract Checked", style="Accent.TButton", command=self._extract_checked_entries).pack(side=tk.LEFT)
        ttk.Button(asset_actions, text="Extract All", command=self._extract_visible_entries).pack(side=tk.LEFT, padx=(6, 0))
        ttk.Button(asset_actions, text="★ Extract Tagged", command=self._extract_tagged_entries).pack(side=tk.LEFT, padx=(6, 0))

        # ── RIGHT: Inspector ──
        ttk.Label(right, text="🔍  Inspector", style="SectionHeader.TLabel").pack(anchor=tk.W)
        ttk.Label(right, textvariable=self.selection_summary_var, style="Summary.TLabel", wraplength=380).pack(anchor=tk.W, pady=(6, 8))

        # Quick actions
        qa = ttk.Frame(right, style="Card.TFrame")
        qa.pack(fill=tk.X, pady=(0, 8))
        ttk.Button(qa, text="▶ Play", style="Accent.TButton", command=self._preview_selected_entry).pack(side=tk.LEFT)
        ttk.Button(qa, text="■ Stop", style="Danger.TButton", command=self._stop_preview).pack(side=tk.LEFT, padx=(6, 0))
        ttk.Separator(qa, orient=tk.VERTICAL).pack(side=tk.LEFT, fill=tk.Y, padx=8, pady=2)
        ttk.Button(qa, text="★ Tag", command=self._toggle_tag_for_selection).pack(side=tk.LEFT)
        ttk.Button(qa, text="Save Edits", command=self._save_selected_metadata).pack(side=tk.LEFT, padx=(6, 0))

        ttk.Label(
            right,
            text="Double-click a clip to preview. Click ☑ column to toggle extraction.",
            style="Summary.TLabel",
            wraplength=380,
        ).pack(anchor=tk.W, pady=(0, 8))

        # Metadata grid
        meta_grid = ttk.Frame(right, style="Card.TFrame")
        meta_grid.pack(fill=tk.X, pady=(0, 8))
        meta_rows = [
            ("Area", self.meta_area_var),
            ("Role", self.meta_type_var),
            ("Time", self.meta_time_var),
            ("Layer", self.meta_mood_var),
            ("Source", self.meta_source_var),
            ("File ID", self.meta_file_data_var),
        ]
        for row_idx, (label, var) in enumerate(meta_rows):
            ttk.Label(meta_grid, text=label, style="Meta.TLabel").grid(row=row_idx, column=0, sticky=tk.W, padx=(0, 10), pady=2)
            ttk.Label(meta_grid, textvariable=var, style="SectionValue.TLabel").grid(row=row_idx, column=1, sticky="ew", pady=2)
        meta_grid.columnconfigure(1, weight=1)
        ttk.Label(right, text="How it plays", style="CardTitle.TLabel").pack(anchor=tk.W, pady=(4, 0))
        ttk.Label(right, textvariable=self.meta_play_var, style="Summary.TLabel", wraplength=360).pack(anchor=tk.W, pady=(2, 8))

        # Editable fields
        ttk.Label(right, text="Title", style="CardTitle.TLabel").pack(anchor=tk.W, pady=(4, 0))
        self.title_entry = ttk.Entry(right)
        self.title_entry.pack(fill=tk.X, pady=(4, 8))

        ttk.Label(right, text="Description", style="CardTitle.TLabel").pack(anchor=tk.W)
        self.description_text = tk.Text(
            right,
            height=6,
            bg=p["bg_field"],
            fg=p["fg_primary"],
            insertbackground=p["accent"],
            relief=tk.FLAT,
            font=("Segoe UI", 10),
            wrap=tk.WORD,
            borderwidth=0,
            padx=6,
            pady=4,
        )
        self.description_text.pack(fill=tk.BOTH, expand=True, pady=(4, 8))
        ttk.Label(right, textvariable=self.preview_status_var, style="Status.TLabel", wraplength=380).pack(anchor=tk.W)
        self.title_entry.bind("<FocusOut>", lambda _e: self._persist_current_editor())
        self.description_text.bind("<FocusOut>", lambda _e: self._persist_current_editor())

        # ── Bottom: Status + Log ──
        bottom = ttk.Frame(root, style="Card.TFrame", padding=(10, 6))
        bottom.grid(row=4, column=0, sticky="ew", pady=(8, 0))
        bottom.columnconfigure(0, weight=1)

        status_row = ttk.Frame(bottom, style="Card.TFrame")
        status_row.pack(fill=tk.X)
        ttk.Progressbar(status_row, variable=self.progress_var, mode="determinate", maximum=100).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 10))
        ttk.Label(status_row, textvariable=self.status_var, style="Status.TLabel").pack(side=tk.LEFT)

        # Collapsible log
        self.log_text = tk.Text(
            bottom,
            height=6,
            bg=p["bg_deep"],
            fg=p["fg_secondary"],
            insertbackground=p["fg_secondary"],
            relief=tk.FLAT,
            font=("Consolas", 9),
            wrap=tk.WORD,
            borderwidth=0,
            padx=6,
            pady=4,
        )
        self.log_text.pack(fill=tk.X, pady=(6, 0))
        self.log_text.configure(state=tk.DISABLED)
        self.asset_type_combo.configure(values=("All",))
        self.asset_time_combo.configure(values=("All",))
        self.asset_role_combo.configure(values=("All", "Bed", "Atmosphere", "Detail", "Music", "Intro"))
        self._reset_inspector()

    def _log_ui(self, message: str) -> None:
        self.log_text.configure(state=tk.NORMAL)
        self.log_text.insert(tk.END, f"{message}\n")
        self.log_text.see(tk.END)
        self.log_text.configure(state=tk.DISABLED)

    def _log_from_worker(self, message: str) -> None:
        self.ui_queue.put(("log", message))

    def _set_status(self, message: str) -> None:
        self.status_var.set(message)

    def _set_progress(self, value: float) -> None:
        self.progress_var.set(max(0.0, min(100.0, value)))

    def _browse_wow(self) -> None:
        chosen = filedialog.askdirectory(title="Select the World of Warcraft folder")
        if not chosen:
            return
        self.wow_path_var.set(chosen)
        self._scan_installs(restore=True)

    def _detect_clicked(self) -> None:
        self._scan_installs(restore=True)

    def _selection_path(self) -> Path:
        return self.cache_root.parent / "selected_version.json"

    def _saved_product(self) -> str:
        path = self._selection_path()
        if not path.exists():
            return "wow"
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return "wow"
        if isinstance(payload, dict):
            return str(payload.get("product") or "wow")
        return "wow"

    def _save_selected_product(self, product: str) -> None:
        path = self._selection_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({"product": product}), encoding="utf-8")
        except OSError as exc:
            self._log_ui(f"Could not remember the selected WoW version: {exc}")

    def _scan_installs(self, restore: bool) -> None:
        start = Path(self.wow_path_var.get().strip()) if self.wow_path_var.get().strip() else None
        root = locate_wow_root(start)
        installs = discover_wow_installs(root) if root else []
        self._installs = installs
        self._install_by_choice = {item.choice: item for item in installs}
        self.version_combo["values"] = [item.choice for item in installs]
        if not installs:
            self.version_var.set("")
            self._log_ui("No installed WoW clients found. Browse to the World of Warcraft folder.")
            return
        saved = self._saved_product()
        chosen = next((item for item in installs if item.product == saved), None)
        if chosen is None:
            chosen = next((item for item in installs if item.product == "wow"), installs[0])
        self.version_var.set(chosen.choice)
        self._apply_install(chosen, restore=restore)

    def _on_version_selected(self, _event=None) -> None:
        install = self._install_by_choice.get(self.version_var.get())
        if install:
            self._apply_install(install, restore=True)

    def _apply_install(self, install: WowInstall, restore: bool) -> None:
        self.wow_path_var.set(str(install.folder))
        self.build_var.set(install.version)
        self.resolver._table_build = install.version
        self._save_selected_product(install.product)
        self._log_ui(f"Using {install.label} {install.version}.")
        if not restore:
            return
        if self._loaded_build == install.version and self.zones:
            self._set_status(f"Ready — {pretty_count(len(self.zones))} zones remembered.")
            return
        self._loaded_build = ""
        self.zones = []
        if self.resolver._zones is not None:
            self.resolver.clear_for_reload()
        self._apply_zone_filter()
        self._restore_remembered_zones()

    def _browse_output(self) -> None:
        chosen = filedialog.askdirectory(title="Select output folder")
        if chosen:
            self.output_path_var.set(chosen)

    def _auto_detect_wow_folder(self) -> None:
        candidates = [
            Path.cwd(),
            Path(__file__).resolve().parent,
            Path(r"C:\Program Files (x86)\World of Warcraft\_retail_"),
            Path(r"C:\Program Files\World of Warcraft\_retail_"),
            Path(r"C:\Program Files (x86)\World of Warcraft"),
            Path(r"C:\Program Files\World of Warcraft"),
        ]
        for candidate in candidates:
            retail = detect_retail_dir(candidate)
            if retail:
                self.wow_path_var.set(str(retail))
                self._log_ui(f"Detected WoW retail folder: {retail}")
                return
        self._log_ui("Auto-detect could not find WoW retail folder. Please browse manually.")

    def _run_worker(self, target, *args, warn_if_busy: bool = True):
        if self.worker_thread and self.worker_thread.is_alive():
            if warn_if_busy:
                messagebox.showwarning(APP_TITLE, "A task is already running. Please wait for it to finish.")
            return False
        self.worker_thread = threading.Thread(target=target, args=args, daemon=True)
        self.worker_thread.start()
        return True

    def _resolved_build(self) -> str:
        typed = self.build_var.get().strip()
        if typed:
            return typed
        wow_dir_raw = self.wow_path_var.get().strip()
        retail = detect_retail_dir(Path(wow_dir_raw)) if wow_dir_raw else None
        if not retail:
            return ""
        return parse_local_retail_build(detect_wow_root_from_retail(retail)) or ""

    def _remember_zones(self, build: str, zones: list[ZoneInfo]) -> None:
        if not build or not zones:
            return
        try:
            write_zone_catalog(self.cache_root / build / ZONE_CATALOG_NAME, build, zones)
        except OSError as exc:
            self.ui_queue.put(("log", f"Could not save the zone list: {exc}"))

    def _remember_audio_index(self, build: str) -> None:
        if not build or not self.resolver.audio_index_ready:
            return
        path = self.cache_root / build / AUDIO_INDEX_NAME
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_suffix(".pkl.tmp")
            with self.resolver._index_lock:
                if not self.resolver.audio_index_ready:
                    return
                payload = {
                    "v": 1,
                    "zone_music_map": self.resolver._zone_music_map,
                    "zone_intro_map": self.resolver._zone_intro_map,
                    "sound_ambience_map": self.resolver._sound_ambience_map,
                    "ambience_flavor_map": self.resolver._ambience_flavor_map,
                    "soundkit_to_files": self.resolver._soundkit_to_files,
                    "kit_profiles": self.resolver._kit_profiles,
                }
                with temporary.open("wb") as handle:
                    pickle.dump(payload, handle, protocol=pickle.HIGHEST_PROTOCOL)
            temporary.replace(path)
            self.ui_queue.put(("log", f"Saved sound indexes for build {build}."))
        except OSError as exc:
            self.ui_queue.put(("log", f"Could not save the sound indexes: {exc}"))

    def _write_audio_index(self, build: str, payload: dict) -> None:
        path = self.cache_root / build / AUDIO_INDEX_NAME
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".pkl.part")
        with temporary.open("wb") as handle:
            pickle.dump(payload, handle, protocol=pickle.HIGHEST_PROTOCOL)
        temporary.replace(path)

    def _index_job(self, build: str) -> None:
        try:
            path = self.cache_root / build / AUDIO_INDEX_NAME
            payload = None
            if path.exists() and path.stat().st_size > 64:
                try:
                    with path.open("rb") as handle:
                        loaded = pickle.load(handle)
                    if isinstance(loaded, dict) and loaded.get("v") == 1:
                        payload = loaded
                except Exception as exc:  # noqa: BLE001
                    self.ui_queue.put(("log", f"Could not read saved indexes for {build}: {exc}"))
            if payload is None:
                self.ui_queue.put(("status", f"Reading saved tables for {build}."))
                self.ui_queue.put(("progress", 30.0))
                builder = ZoneAudioResolver(self.db, logger=self._log_from_worker)
                builder.preload_all_tables(build)
                payload = builder.export_audio_index()
                if not payload:
                    self.ui_queue.put(("log", f"Sound indexes for {build} were not built."))
                    return
                self._write_audio_index(build, payload)
                self.ui_queue.put(("log", f"Saved sound indexes for build {build}."))
            self._index_payloads[build] = payload
            self.ui_queue.put(("index_ready", build))
        except Exception as exc:  # noqa: BLE001
            self.ui_queue.put(("log", f"Sound index restore failed for {build}: {exc}"))
            self.ui_queue.put(("status", "Zone list is ready. Sound indexes will build when you open a zone."))

    def _start_index_restore(self, build: str) -> None:
        if not build:
            return
        payload = self._index_payloads.get(build)
        if payload:
            self.ui_queue.put(("index_ready", build))
            return
        running = self._index_threads.get(build)
        if running and running.is_alive():
            self._set_status(f"Preparing sound indexes for {build}...")
            return
        thread = threading.Thread(target=self._index_job, args=(build,), name=f"wow-index-{build}", daemon=True)
        self._index_threads[build] = thread
        thread.start()

    def _restore_remembered_zones(self) -> None:
        build = self._resolved_build()
        if not build:
            return
        zones = read_zone_catalog(self.cache_root / build / ZONE_CATALOG_NAME, build)
        if zones:
            self.db.set_build(build)
            self.resolver.install_zones(zones)
            self.zones = zones
            self._loaded_build = build
            self.build_var.set(build)
            self._apply_zone_filter()
            self._set_status(f"Ready — {pretty_count(len(zones))} zones remembered.")
            self._log_ui(f"Restored {pretty_count(len(zones))} zones for build {build}.")
            self._start_index_restore(build)
            return
        self.build_var.set(build)
        self._set_status(f"Loading zones for {build}...")
        self._run_worker(self._worker_load_zones, False)

    def _load_zones_clicked(self) -> None:
        self._run_worker(self._worker_load_zones, False)

    def _worker_load_zones(self, force: bool = False) -> None:
        try:
            self.ui_queue.put(("status", "Loading zones..."))
            self.ui_queue.put(("progress", 5.0))
            wow_dir_raw = self.wow_path_var.get().strip()
            retail = detect_retail_dir(Path(wow_dir_raw)) if wow_dir_raw else None
            if not retail:
                raise RuntimeError("WoW folder is invalid. Pick your main WoW folder or _retail_ folder.")

            wow_root = detect_wow_root_from_retail(retail)
            local_build = parse_local_retail_build(wow_root)
            desired_build = self.build_var.get().strip() or (local_build or "")

            if not desired_build:
                desired_build = self.db.get_latest_build(preferred_major=12)

            same_build = self._loaded_build == desired_build and self.resolver._zones is not None
            if same_build and not force:
                self.ui_queue.put(("zones_cached", desired_build))
                self.ui_queue.put(("status", f"Ready — {pretty_count(len(self.resolver._zones or []))} zones remembered."))
                self.ui_queue.put(("progress", 100.0))
                self._start_index_restore(desired_build)
                return

            if force or self.resolver._zones is not None:
                self._loaded_build = ""
                self.resolver.clear_for_reload()

            self.db.set_build(desired_build)
            self.resolver._table_build = desired_build
            self.ui_queue.put(("status", "Loading zone table..."))
            self.ui_queue.put(("progress", 15.0))
            zones = self.resolver.load_zones(desired_build)
            self._remember_zones(desired_build, zones)

            self.ui_queue.put(("zones_loaded", zones, desired_build))
            self.ui_queue.put(("status", f"Ready — {pretty_count(len(zones))} zones loaded. Select a zone to browse audio."))
            self.ui_queue.put(("progress", 40.0))
            self._start_index_restore(desired_build)
        except Exception as exc:  # noqa: BLE001
            self.ui_queue.put(("error", f"Load zones failed: {exc}"))
            self.ui_queue.put(("status", "Load failed."))
            self.ui_queue.put(("progress", 0.0))

    def _apply_zone_filter(self) -> None:
        query = self.search_var.get().strip().lower()
        self.zone_listbox.delete(0, tk.END)
        self.filtered_indices.clear()

        for idx, zone in enumerate(self.zones):
            text = f"{zone.area_name} {zone.family_name} {zone.area_id}".lower()
            if query and query not in text:
                continue
            self.filtered_indices.append(idx)
            self.zone_listbox.insert(tk.END, zone.display_name)

        self.zone_summary_var.set(f"Showing {pretty_count(len(self.filtered_indices))} zones. Select one or more to retrieve audio.")

    def _select_all_filtered(self) -> None:
        if self.zone_listbox.size() <= 0:
            return
        self.zone_listbox.select_set(0, tk.END)
        self._schedule_asset_refresh()

    def _clear_zone_selection(self) -> None:
        self.zone_listbox.selection_clear(0, tk.END)
        self.asset_entries = []
        self.asset_entry_map.clear()
        self._render_asset_tree()
        self.asset_summary_var.set("No zones selected.")
        self._reset_inspector()

    def _selected_zones(self) -> list[ZoneInfo]:
        indices = list(self.zone_listbox.curselection())
        output: list[ZoneInfo] = []
        for selected_idx in indices:
            if selected_idx < 0 or selected_idx >= len(self.filtered_indices):
                continue
            zone_idx = self.filtered_indices[selected_idx]
            if zone_idx < 0 or zone_idx >= len(self.zones):
                continue
            output.append(self.zones[zone_idx])
        return output

    def _current_zone_token(self) -> tuple[int, ...]:
        return tuple(sorted(zone.area_id for zone in self._selected_zones()))

    def _schedule_asset_refresh(self) -> None:
        if self.zone_refresh_after_id:
            self.after_cancel(self.zone_refresh_after_id)
        self.zone_refresh_after_id = self.after(180, self._load_assets_for_selected_zones)

    def _load_assets_for_selected_zones(self) -> bool:
        self.zone_refresh_after_id = None
        zones = self._selected_zones()
        if not zones:
            self.asset_entries = []
            self.asset_entry_map.clear()
            self.asset_summary_var.set("Select one or more zones to retrieve audio.")
            self._render_asset_tree()
            self._reset_inspector()
            return False

        include_music = bool(self.extract_music_var.get())
        include_ambient = bool(self.extract_ambient_var.get())
        include_subzone_music = bool(self.include_subzone_music_var.get())
        if not include_music and not include_ambient:
            self.asset_entries = []
            self.asset_entry_map.clear()
            self.asset_summary_var.set("Enable Music and/or Ambient Sounds to retrieve assets.")
            self._render_asset_tree()
            self._reset_inspector()
            return False

        zone_token = tuple(sorted(zone.area_id for zone in zones))
        started = self._run_worker(
            self._worker_load_assets,
            zones,
            include_music,
            include_ambient,
            include_subzone_music,
            zone_token,
            warn_if_busy=False,
        )
        if not started:
            self._set_status("Busy. Retrieval will refresh after the current task finishes.")
        return bool(started)

    def _worker_load_assets(
        self,
        zones: list[ZoneInfo],
        include_music: bool,
        include_ambient: bool,
        include_subzone_music: bool,
        zone_token: tuple[int, ...],
    ) -> None:
        try:
            zone_names = ", ".join(zone.friendly_name for zone in zones[:3])
            if len(zones) > 3:
                zone_names += f" +{len(zones) - 3} more"
            self.ui_queue.put(("status", f"Retrieving audio for {zone_names}..."))
            entries: list[AudioEntry] = []
            for zone in zones:
                entries.extend(
                    self.resolver.entries_for_zone(
                        zone,
                        include_music=include_music,
                        include_ambient=include_ambient,
                        include_foreign_child_music=include_subzone_music,
                    )
                )
            self.ui_queue.put(("status", f"Resolving friendly names for {pretty_count(len(entries))} clips..."))
            lookup_map = self.listfile_cache.resolve_file_names([entry.file_data_id for entry in entries])
            resolved_defaults = {
                self._entry_key(entry): self._resolve_entry_defaults(entry, lookup_map.get(entry.file_data_id))
                for entry in entries
            }
            self.ui_queue.put(("assets_ready", zone_token, entries, resolved_defaults))
            self.ui_queue.put(("status", f"Retrieved {pretty_count(len(entries))} clips from {pretty_count(len(zones))} zone(s)."))
        except Exception as exc:  # noqa: BLE001
            self.ui_queue.put(("error", f"Audio retrieval failed: {exc}"))
            self.ui_queue.put(("status", "Audio retrieval failed."))

    def _entry_key(self, entry: AudioEntry) -> str:
        return f"{entry.zone_id}:{entry.file_data_id}"

    def _resolve_entry_defaults(self, entry: AudioEntry, file_path: str | None) -> dict[str, object]:
        title = pretty_sound_title_from_path(file_path, entry.file_data_id) or entry.default_title
        description = entry.default_description
        if file_path:
            description = (
                f"{entry.type_label} for {entry.zone_name}. "
                f"Time of day: {entry.time_of_day}. "
                f"Mood: {entry.mood_label}. "
                f"Client file: {file_path}. "
                f"Source: {entry.source_label}."
            )
        return {
            "lookup_path": file_path or "",
            "title": title,
            "description": description,
        }

    def _apply_resolved_defaults(self, entries: list[AudioEntry], resolved_defaults: dict[str, dict[str, object]]) -> None:
        for entry in entries:
            override = self._entry_override(entry)
            resolved = resolved_defaults.get(self._entry_key(entry), {})
            if not str(override.get("title", "")).strip():
                override["title"] = resolved.get("title", entry.default_title)
            if not str(override.get("description", "")).strip():
                override["description"] = resolved.get("description", entry.default_description)
            if resolved.get("lookup_path"):
                override["lookup_path"] = resolved["lookup_path"]
            override.setdefault("checked", True)

    def _entry_override(self, entry: AudioEntry) -> dict[str, object]:
        key = self._entry_key(entry)
        if key not in self.entry_overrides:
            self.entry_overrides[key] = {}
        return self.entry_overrides[key]

    def _entry_title(self, entry: AudioEntry) -> str:
        override = self._entry_override(entry)
        title = str(override.get("title", "")).strip()
        return title or entry.default_title

    def _entry_description(self, entry: AudioEntry) -> str:
        override = self._entry_override(entry)
        description = str(override.get("description", "")).strip()
        return description or entry.default_description

    def _entry_tagged(self, entry: AudioEntry) -> bool:
        return bool(self._entry_override(entry).get("tagged", False))

    def _entry_checked(self, entry: AudioEntry) -> bool:
        return bool(self._entry_override(entry).get("checked", True))

    def _entry_playback(self, entry: AudioEntry) -> dict[str, object]:
        try:
            return self.resolver.playback_for(entry)
        except Exception:  # noqa: BLE001
            return describe_playback(entry, None, None)

    def _layer_summary(self, entries: list[AudioEntry] | None = None) -> list[dict[str, object]]:
        chosen = self.asset_entries if entries is None else entries
        grouped: dict[tuple, dict[str, object]] = {}
        for entry in chosen:
            playback = self._entry_playback(entry)
            key = (entry.sound_kit_id, playback.get("role"), entry.time_of_day)
            layer = grouped.get(key)
            if layer is None:
                layer = {
                    "role": playback.get("role"),
                    "time_of_day": entry.time_of_day,
                    "sound_kit_id": entry.sound_kit_id,
                    "how": playback.get("how"),
                    "volume": playback.get("volume"),
                    "clip_count": 0,
                    "file_data_ids": [],
                }
                grouped[key] = layer
            layer["clip_count"] = int(layer["clip_count"]) + 1
            ids = layer["file_data_ids"]
            assert isinstance(ids, list)
            ids.append(entry.file_data_id)
        role_order = {"Bed": 0, "Atmosphere": 1, "Detail": 2, "Music": 3, "Intro": 4}
        return sorted(grouped.values(), key=lambda item: (role_order.get(str(item.get("role")), 9), str(item.get("time_of_day"))))

    def _refresh_recipe_label(self) -> None:
        layers = self._layer_summary()
        if not layers:
            self.recipe_var.set("")
            return
        parts = []
        for layer in layers:
            volume = layer.get("volume")
            volume_text = f" @ {float(volume):.2f}" if isinstance(volume, (int, float)) else ""
            parts.append(f"{layer['role']} {layer['time_of_day']} ({layer['clip_count']}{volume_text})")
        self.recipe_var.set("Layers: " + " · ".join(parts))

    def _checkbox_label(self, checked_count: int, total_count: int) -> str:
        if total_count <= 0 or checked_count <= 0:
            return "[ ]"
        if checked_count >= total_count:
            return "[x]"
        return "[-]"

    def _update_filter_values(self) -> None:
        type_values = ["All"] + sorted({entry.type_label for entry in self.asset_entries})
        time_values = ["All"] + sorted({entry.time_of_day for entry in self.asset_entries})
        role_values = ["All"] + sorted({str(self._entry_playback(entry).get("role") or "Detail") for entry in self.asset_entries})

        self.asset_type_combo.configure(values=type_values)
        self.asset_time_combo.configure(values=time_values)
        self.asset_role_combo.configure(values=role_values)

        if self.asset_type_var.get() not in type_values:
            self.asset_type_var.set("All")
        if self.asset_time_var.get() not in time_values:
            self.asset_time_var.set("All")
        if self.asset_role_var.get() not in role_values:
            self.asset_role_var.set("All")

    def _filtered_asset_entries(self) -> list[AudioEntry]:
        query = self.asset_search_var.get().strip().lower()
        selected_type = self.asset_type_var.get().strip() or "All"
        selected_time = self.asset_time_var.get().strip() or "All"
        selected_role = self.asset_role_var.get().strip() or "All"
        tagged_only = bool(self.asset_tagged_only_var.get())

        output: list[AudioEntry] = []
        for entry in self.asset_entries:
            playback = self._entry_playback(entry)
            if selected_type != "All" and entry.type_label != selected_type:
                continue
            if selected_time != "All" and entry.time_of_day != selected_time:
                continue
            if selected_role != "All" and playback.get("role") != selected_role:
                continue
            if tagged_only and not self._entry_tagged(entry):
                continue
            if query:
                haystack = " ".join(
                    [
                        entry.zone_name,
                        entry.group_label,
                        entry.type_label,
                        entry.time_of_day,
                        str(playback.get("role") or ""),
                        entry.mood_label,
                        self._entry_title(entry),
                        self._entry_description(entry),
                        str(entry.file_data_id),
                    ]
                ).lower()
                if query not in haystack:
                    continue
            output.append(entry)
        return output

    def _render_asset_tree(self) -> None:
        for item in self.asset_tree.get_children():
            self.asset_tree.delete(item)

        filtered_entries = self._filtered_asset_entries()
        self.asset_summary_var.set(
            f"Showing {pretty_count(len(filtered_entries))} of {pretty_count(len(self.asset_entries))} retrieved clips."
        )

        if not filtered_entries:
            self.selection_summary_var.set("No asset selected.")
            return

        role_order = {"Bed": 0, "Atmosphere": 1, "Detail": 2, "Music": 3, "Intro": 4}
        filtered_entries.sort(
            key=lambda entry: (
                entry.area_label.lower(),
                role_order.get(str(self._entry_playback(entry).get("role")), 9),
                entry.time_of_day.lower(),
                self._entry_title(entry).lower(),
                entry.file_data_id,
            )
        )

        def ensure_group(parent: str, group_id: str, label: str, open_item: bool = True) -> str:
            if self.asset_tree.exists(group_id):
                return group_id
            self.asset_tree.insert(parent, tk.END, iid=group_id, text=label, values=("[ ]", "", "", "", ""))
            self.asset_tree.item(group_id, open=open_item)
            return group_id

        for entry in filtered_entries:
            key = self._entry_key(entry)
            playback = self._entry_playback(entry)
            role = str(playback.get("role") or "Detail")
            volume = playback.get("volume")
            volume_text = f"{float(volume):.2f}" if isinstance(volume, (int, float)) else ""
            area_id = ensure_group("", f"area::{slugify_name(entry.area_label, 'area')}", entry.area_label)
            role_id = ensure_group(area_id, f"{area_id}::role::{slugify_name(role, 'role')}", role)
            time_id = ensure_group(role_id, f"{role_id}::time::{slugify_name(entry.time_of_day, 'time')}", entry.time_of_day, open_item=True)
            self.asset_tree.insert(
                time_id,
                tk.END,
                iid=key,
                text=self._entry_title(entry),
                values=(
                    "[x]" if self._entry_checked(entry) else "[ ]",
                    role,
                    entry.time_of_day,
                    volume_text,
                    "★" if self._entry_tagged(entry) else "",
                ),
            )

        self._update_group_extract_states()

        if self.selected_entry_key and self.asset_tree.exists(self.selected_entry_key):
            self.asset_tree.selection_set(self.selected_entry_key)
            self.asset_tree.see(self.selected_entry_key)
        else:
            first_key = self._entry_key(filtered_entries[0])
            if self.asset_tree.exists(first_key):
                self.asset_tree.selection_set(first_key)
                self.asset_tree.see(first_key)
        self._on_asset_tree_select()

    def _collect_leaf_keys(self, item_id: str) -> list[str]:
        if item_id in self.asset_entry_map:
            return [item_id]
        output: list[str] = []
        for child in self.asset_tree.get_children(item_id):
            output.extend(self._collect_leaf_keys(child))
        return output

    def _selected_entry_keys_from_tree(self) -> list[str]:
        keys: list[str] = []
        for item_id in self.asset_tree.selection():
            keys.extend(self._collect_leaf_keys(item_id))
        return list(dict.fromkeys(keys))

    def _selected_leaf_entries(self) -> list[AudioEntry]:
        return [self.asset_entry_map[key] for key in self._selected_entry_keys_from_tree() if key in self.asset_entry_map]

    def _update_group_extract_states(self, item_id: str = "") -> tuple[int, int]:
        total_leaves = 0
        checked_leaves = 0
        for child in self.asset_tree.get_children(item_id):
            if child in self.asset_entry_map:
                total_leaves += 1
                if self._entry_checked(self.asset_entry_map[child]):
                    checked_leaves += 1
                continue
            child_total, child_checked = self._update_group_extract_states(child)
            total_leaves += child_total
            checked_leaves += child_checked
            self.asset_tree.item(child, values=(self._checkbox_label(child_checked, child_total), "", "", "", ""))
        return total_leaves, checked_leaves

    def _persist_current_editor(self, refresh_tree: bool = False) -> bool:
        if not self.selected_entry_key or self.selected_entry_key not in self.asset_entry_map:
            return False
        entry = self.asset_entry_map[self.selected_entry_key]
        override = self._entry_override(entry)
        title = self.title_entry.get().strip()
        description = self.description_text.get("1.0", tk.END).strip()
        changed = (
            title != str(override.get("title", "")).strip()
            or description != str(override.get("description", "")).strip()
        )
        override["title"] = title
        override["description"] = description
        if changed and refresh_tree:
            self._render_asset_tree()
        return changed

    def _on_asset_tree_select(self) -> None:
        self._persist_current_editor()
        selected_entries = self._selected_leaf_entries()
        if len(selected_entries) == 1:
            entry = selected_entries[0]
            self.selected_entry_key = self._entry_key(entry)
            override = self._entry_override(entry)
            playback = self._entry_playback(entry)
            self.selection_summary_var.set(
                f"{self._entry_title(entry)} | {playback.get('role')} | {entry.time_of_day}"
            )
            self.meta_area_var.set(entry.area_label)
            self.meta_type_var.set(str(playback.get("role") or entry.type_label))
            self.meta_time_var.set(entry.time_of_day)
            self.meta_mood_var.set(entry.group_label)
            self.meta_source_var.set(str(override.get("lookup_path") or entry.source_label))
            self.meta_file_data_var.set(str(entry.file_data_id))
            self.meta_play_var.set(str(playback.get("how") or ""))
            self.title_entry.delete(0, tk.END)
            self.title_entry.insert(0, self._entry_title(entry))
            self.description_text.delete("1.0", tk.END)
            self.description_text.insert("1.0", self._entry_description(entry))
        elif selected_entries:
            self.selected_entry_key = None
            checked_count = sum(1 for entry in selected_entries if self._entry_checked(entry))
            self.selection_summary_var.set(
                f"{pretty_count(len(selected_entries))} clips selected. {pretty_count(checked_count)} marked for extraction."
            )
            self.title_entry.delete(0, tk.END)
            self.description_text.delete("1.0", tk.END)
            self.meta_area_var.set("Multiple")
            self.meta_type_var.set("Multiple")
            self.meta_time_var.set("Multiple")
            self.meta_mood_var.set("Multiple")
            self.meta_source_var.set("Multiple")
            self.meta_file_data_var.set("-")
            self.meta_play_var.set("Several clips. Each keeps the clock of its own layer.")
        else:
            self.selected_entry_key = None
            self._reset_inspector()

    def _reset_inspector(self) -> None:
        self.selection_summary_var.set("Select a clip to edit metadata, tag it, preview it, or extract it.")
        self.meta_area_var.set("-")
        self.meta_type_var.set("-")
        self.meta_time_var.set("-")
        self.meta_mood_var.set("-")
        self.meta_source_var.set("-")
        self.meta_file_data_var.set("-")
        if hasattr(self, "meta_play_var"):
            self.meta_play_var.set("Select a clip to see how the client plays it.")
        if hasattr(self, "title_entry"):
            self.title_entry.delete(0, tk.END)
        if hasattr(self, "description_text"):
            self.description_text.delete("1.0", tk.END)

    def _save_selected_metadata(self) -> None:
        selected_entries = self._selected_leaf_entries()
        if len(selected_entries) != 1:
            messagebox.showinfo(APP_TITLE, "Select a single clip to edit its title and description.")
            return

        entry = selected_entries[0]
        self._persist_current_editor(refresh_tree=True)
        self.selection_summary_var.set(f"Saved metadata for {self._entry_title(entry)}.")

    def _toggle_tag_for_selection(self) -> None:
        self._persist_current_editor()
        selected_entries = self._selected_leaf_entries()
        if not selected_entries:
            messagebox.showinfo(APP_TITLE, "Select one or more clips or categories to tag.")
            return

        should_tag = not all(self._entry_tagged(entry) for entry in selected_entries)
        for entry in selected_entries:
            self._entry_override(entry)["tagged"] = should_tag
        self._render_asset_tree()
        self.selection_summary_var.set(
            f"{'Tagged' if should_tag else 'Untagged'} {pretty_count(len(selected_entries))} clip(s)."
        )

    def _set_checked_for_entries(self, entries: list[AudioEntry], checked: bool) -> int:
        updated = 0
        for entry in entries:
            override = self._entry_override(entry)
            if bool(override.get("checked", True)) != checked:
                updated += 1
            override["checked"] = checked
        return updated

    def _handle_asset_tree_click(self, event) -> str | None:
        row_id = self.asset_tree.identify_row(event.y)
        column_id = self.asset_tree.identify_column(event.x)
        if not row_id or column_id != "#1":
            return None
        keys = self._collect_leaf_keys(row_id)
        if not keys:
            return "break"
        entries = [self.asset_entry_map[key] for key in keys if key in self.asset_entry_map]
        if not entries:
            return "break"
        should_check = not all(self._entry_checked(entry) for entry in entries)
        self._set_checked_for_entries(entries, should_check)
        self.asset_tree.selection_set(row_id)
        self._render_asset_tree()
        self.selection_summary_var.set(
            f"{'Marked' if should_check else 'Removed'} {pretty_count(len(entries))} clip(s) for extraction."
        )
        return "break"

    def _handle_asset_tree_double_click(self, event) -> str | None:
        row_id = self.asset_tree.identify_row(event.y)
        if not row_id or row_id not in self.asset_entry_map:
            return None
        self.asset_tree.selection_set(row_id)
        self._on_asset_tree_select()
        self._preview_selected_entry()
        return "break"

    def _toggle_checked_for_tree_selection(self, _event=None) -> str:
        selected_entries = self._selected_leaf_entries()
        if not selected_entries:
            return "break"
        should_check = not all(self._entry_checked(entry) for entry in selected_entries)
        self._set_checked_for_entries(selected_entries, should_check)
        self._render_asset_tree()
        self.selection_summary_var.set(
            f"{'Marked' if should_check else 'Removed'} {pretty_count(len(selected_entries))} selected clip(s) for extraction."
        )
        return "break"

    def _preview_selected_entry(self) -> None:
        self._persist_current_editor()
        selected_entries = self._selected_leaf_entries()
        if len(selected_entries) != 1:
            messagebox.showinfo(APP_TITLE, "Select a single clip to preview.")
            return

        ffmpeg = find_ffmpeg()
        if not ffmpeg:
            messagebox.showerror(APP_TITLE, "ffmpeg was not found. Install ffmpeg or add it to PATH.")
            return

        self._stop_preview()
        self.preview_status_var.set("Preparing preview...")
        self._run_worker(self._worker_preview_entry, selected_entries[0], ffmpeg)

    def _worker_preview_entry(self, entry: AudioEntry, ffmpeg_path: Path) -> None:
        try:
            raw_file = self.preview_cache / f"{entry.file_data_id}.bin"
            preview_file = self.preview_cache / f"{entry.file_data_id}.wav"

            if not raw_file.exists() or raw_file.stat().st_size < 128:
                data = fetch_bytes(WAGO_CASC_URL.format(file_data_id=entry.file_data_id), timeout=120)
                raw_file.write_bytes(data)

            if not preview_file.exists() or preview_file.stat().st_size < 512:
                ffmpeg_cmd = [
                    str(ffmpeg_path),
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-y",
                    "-i",
                    str(raw_file),
                    "-vn",
                    "-ac",
                    "2",
                    "-ar",
                    "44100",
                    "-c:a",
                    "pcm_s16le",
                    str(preview_file),
                ]
                run = subprocess.run(ffmpeg_cmd, capture_output=True, text=True, check=False)
                if run.returncode != 0 or not preview_file.exists():
                    stderr = (run.stderr or "").strip()
                    raise RuntimeError(stderr or "Preview conversion failed")

            self.ui_queue.put(("preview_ready", str(preview_file), self._entry_title(entry)))
            self.ui_queue.put(("status", f"Preview ready for {self._entry_title(entry)}"))
        except Exception as exc:  # noqa: BLE001
            self.ui_queue.put(("error", f"Preview failed: {exc}"))
            self.ui_queue.put(("status", "Preview failed."))

    def _stop_preview(self) -> None:
        winsound.PlaySound(None, winsound.SND_PURGE)
        self.preview_status_var.set("Preview stopped.")

    def _checked_entries(self) -> list[AudioEntry]:
        return [entry for entry in self.asset_entries if self._entry_checked(entry)]

    def _check_visible_entries(self) -> None:
        entries = self._filtered_asset_entries()
        if not entries:
            messagebox.showinfo(APP_TITLE, "There are no visible clips to mark for extraction.")
            return
        self._set_checked_for_entries(entries, True)
        self._render_asset_tree()
        self.selection_summary_var.set(f"Marked {pretty_count(len(entries))} visible clip(s) for extraction.")

    def _uncheck_visible_entries(self) -> None:
        entries = self._filtered_asset_entries()
        if not entries:
            messagebox.showinfo(APP_TITLE, "There are no visible clips to remove from extraction.")
            return
        self._set_checked_for_entries(entries, False)
        self._render_asset_tree()
        self.selection_summary_var.set(f"Removed {pretty_count(len(entries))} visible clip(s) from extraction.")

    def _extract_checked_entries(self) -> None:
        self._start_extract_entries(self._checked_entries(), "checked clips")

    def _extract_visible_entries(self) -> None:
        self._start_extract_entries(self._filtered_asset_entries(), "visible clips")

    def _extract_tagged_entries(self) -> None:
        self._start_extract_entries([entry for entry in self.asset_entries if self._entry_tagged(entry)], "tagged clips")

    def _extract_selected_entries(self) -> None:
        self._start_extract_entries(self._selected_leaf_entries(), "selected clips")

    def _start_extract_entries(self, entries: list[AudioEntry], label: str) -> None:
        self._persist_current_editor()
        if not entries:
            messagebox.showinfo(APP_TITLE, f"There are no {label} to extract.")
            return

        output_dir = Path(self.output_path_var.get().strip()).expanduser()
        if not output_dir:
            messagebox.showerror(APP_TITLE, "Choose an output folder.")
            return

        ffmpeg = find_ffmpeg()
        if not ffmpeg:
            messagebox.showerror(APP_TITLE, "ffmpeg was not found. Install ffmpeg or add it to PATH.")
            return

        fmt = self.format_var.get().strip().lower()
        metadata_snapshot = {key: dict(value) for key, value in self.entry_overrides.items()}
        zone_lookup = {zone.area_id: zone for zone in self.zones}
        self._set_progress(0.0)
        self._run_worker(
            self._worker_extract,
            entries,
            metadata_snapshot,
            zone_lookup,
            output_dir,
            ffmpeg,
            fmt,
            label,
        )

    def _worker_extract(
        self,
        entries: list[AudioEntry],
        metadata_snapshot: dict[str, dict[str, object]],
        zone_lookup: dict[int, ZoneInfo],
        output_dir: Path,
        ffmpeg_path: Path,
        out_fmt: str,
        label: str,
    ) -> None:
        try:
            output_dir.mkdir(parents=True, exist_ok=True)
            ext, codec_args = ffmpeg_args_for_format(out_fmt)
            raw_cache = output_dir / "_raw_cache"
            raw_cache.mkdir(parents=True, exist_ok=True)
            export_index = {
                "type": "wow_zone_audio_export_index",
                "version": APP_VERSION,
                "generated_at": utc_timestamp(),
                "format": ext,
                "zones": [],
            }

            if not entries:
                self.ui_queue.put(("status", "No clips found for extraction."))
                return

            grouped_by_zone: dict[int, list[AudioEntry]] = defaultdict(list)
            for entry in entries:
                grouped_by_zone[entry.zone_id].append(entry)

            total_clips = len(entries)
            self.ui_queue.put(("log", f"Planned clips: {pretty_count(total_clips)} from {pretty_count(len(grouped_by_zone))} zone(s) using {label}."))
            completed = 0
            failed = 0

            for zone_id, zone_entries in grouped_by_zone.items():
                zone = zone_lookup.get(zone_id)
                if not zone:
                    zone = ZoneInfo(
                        area_id=zone_id,
                        area_name=zone_entries[0].zone_name,
                        zone_name=zone_entries[0].zone_name,
                        parent_area_id=0,
                        parent_area_name="",
                        ambience_id=0,
                        zone_music_id=0,
                        uw_zone_music_id=0,
                        intro_sound_id=0,
                        uw_intro_sound_id=0,
                    )

                self.ui_queue.put(("log", f"Zone: {zone.friendly_name} ({zone.area_id}) - {pretty_count(len(zone_entries))} clips"))
                zone_root = (
                    output_dir
                    / "zones"
                    / sanitize_path_name(zone.family_name, "Standalone Zones")
                    / sanitize_path_name(zone.friendly_name, f"Zone_{zone.area_id}")
                )
                zone_root.mkdir(parents=True, exist_ok=True)

                grouped_entries: dict[tuple[str, str, str, str, str], list[AudioEntry]] = defaultdict(list)
                for entry in zone_entries:
                    grouped_entries[
                        (
                            entry.category_slug,
                            entry.time_of_day,
                            entry.mood_label,
                            entry.type_label,
                            entry.group_slug,
                        )
                    ].append(entry)

                zone_manifest_rows: list[dict[str, str | int | float]] = []
                zone_group_payloads: list[dict[str, object]] = []

                for (_category_slug, _time_of_day, _mood_label, _type_label, _group_slug), group in sorted(
                    grouped_entries.items(),
                    key=lambda item: (
                        item[0][0],
                        item[0][1].lower(),
                        item[0][2].lower(),
                        item[0][3].lower(),
                        item[1][0].group_label.lower(),
                    ),
                ):
                    exemplar = group[0]
                    group_dir = (
                        zone_root
                        / exemplar.category_slug
                        / sanitize_path_name(exemplar.time_of_day, "Any")
                        / sanitize_path_name(exemplar.mood_label, "Mood")
                        / sanitize_path_name(
                            f"{exemplar.type_label}__{exemplar.group_label}",
                            f"Group_{exemplar.sound_kit_id}",
                        )
                    )
                    group_dir.mkdir(parents=True, exist_ok=True)
                    manifest_rows: list[dict[str, str | int | float]] = []
                    group_files: list[dict[str, object]] = []

                    for index, entry in enumerate(group, start=1):
                        completed += 1
                        progress = (completed / total_clips) * 100.0
                        self.ui_queue.put(("progress", progress))
                        self.ui_queue.put(("status", f"Extracting {completed}/{total_clips}"))

                        raw_file = raw_cache / f"{entry.file_data_id}.bin"
                        entry_key = self._entry_key(entry)
                        override = metadata_snapshot.get(entry_key, {})
                        title = str(override.get("title") or entry.default_title).strip() or entry.default_title
                        description = str(override.get("description") or entry.default_description).strip() or entry.default_description
                        tagged = bool(override.get("tagged", False))
                        out_name = f"{index:03d}__fd{entry.file_data_id}__{sanitize_path_name(title, 'clip')}.{ext}"
                        out_file = group_dir / out_name

                        try:
                            if not raw_file.exists() or raw_file.stat().st_size < 128:
                                data = fetch_bytes(WAGO_CASC_URL.format(file_data_id=entry.file_data_id), timeout=120)
                                raw_file.write_bytes(data)

                            ffmpeg_cmd = [
                                str(ffmpeg_path),
                                "-hide_banner",
                                "-loglevel",
                                "error",
                                "-y",
                                "-i",
                                str(raw_file),
                                "-vn",
                                "-ac",
                                "2",
                                "-ar",
                                "44100",
                                *codec_args,
                                str(out_file),
                            ]
                            run = subprocess.run(ffmpeg_cmd, capture_output=True, text=True, check=False)
                            if run.returncode != 0 or not out_file.exists() or out_file.stat().st_size < 256:
                                stderr = (run.stderr or "").strip()
                                raise RuntimeError(stderr or "ffmpeg conversion failed")

                            relative_path = out_file.relative_to(zone_root).as_posix()
                            row = {
                                "zone_id": zone.area_id,
                                "zone_name": zone.friendly_name,
                                "parent_area_name": zone.parent_area_name,
                                "category": entry.category,
                                "time_of_day": entry.time_of_day,
                                "mood": entry.mood_label,
                                "type": entry.type_label,
                                "title": title,
                                "description": description,
                                "tagged": tagged,
                                "group_label": entry.group_label,
                                "suggested_layer_type": entry.suggested_layer_type,
                                "file_data_id": entry.file_data_id,
                                "sound_kit_id": entry.sound_kit_id,
                                "source_type": entry.source_type,
                                "source_id": entry.source_id,
                                "source_label": entry.source_label,
                                "hint": entry.hint,
                                "output_file": out_file.name,
                                "relative_path": relative_path,
                            }
                            manifest_rows.append(row)
                            zone_manifest_rows.append(row)
                            group_files.append(
                                {
                                    "file_data_id": entry.file_data_id,
                                    "sound_kit_id": entry.sound_kit_id,
                                    "source_type": entry.source_type,
                                    "source_id": entry.source_id,
                                    "source_label": entry.source_label,
                                    "time_of_day": entry.time_of_day,
                                    "mood": entry.mood_label,
                                    "type": entry.type_label,
                                    "title": title,
                                    "description": description,
                                    "tagged": tagged,
                                    "hint": entry.hint,
                                    "file_name": out_file.name,
                                    "relative_path": relative_path,
                                }
                            )
                        except Exception as exc:  # noqa: BLE001
                            failed += 1
                            self.ui_queue.put(("log", f"  Failed FileDataID {entry.file_data_id}: {exc}"))

                    if manifest_rows:
                        manifest = group_dir / "manifest.csv"
                        with manifest.open("w", encoding="utf-8", newline="") as handle:
                            writer = csv.DictWriter(handle, fieldnames=list(manifest_rows[0].keys()))
                            writer.writeheader()
                            writer.writerows(manifest_rows)

                        zone_group_payloads.append(
                            {
                                "id": exemplar.group_slug,
                                "name": exemplar.group_label,
                                "category": exemplar.category,
                                "category_slug": exemplar.category_slug,
                                "time_of_day": exemplar.time_of_day,
                                "mood": exemplar.mood_label,
                                "type": exemplar.type_label,
                                "suggested_layer_type": exemplar.suggested_layer_type,
                                "sound_kit_id": exemplar.sound_kit_id,
                                "source_type": exemplar.source_type,
                                "source_id": exemplar.source_id,
                                "source_label": exemplar.source_label,
                                "hint": exemplar.hint,
                                "import_defaults": import_defaults_for_entry(exemplar, self.resolver.playback_for(exemplar)),
                                "playback": self.resolver.playback_for(exemplar),
                                "files": group_files,
                            }
                        )

                if zone_manifest_rows:
                    zone_manifest_csv = zone_root / "manifest.csv"
                    with zone_manifest_csv.open("w", encoding="utf-8", newline="") as handle:
                        writer = csv.DictWriter(handle, fieldnames=list(zone_manifest_rows[0].keys()))
                        writer.writeheader()
                        writer.writerows(zone_manifest_rows)

                    zone_manifest = {
                        "type": "wow_zone_audio_bundle",
                        "version": APP_VERSION,
                        "generated_at": utc_timestamp(),
                        "format": ext,
                        "zone": {
                            "id": zone.area_id,
                            "name": zone.friendly_name,
                            "display_name": zone.display_name,
                            "zone_name": zone.zone_name,
                            "family_name": zone.family_name,
                            "parent_area_id": zone.parent_area_id,
                            "parent_area_name": zone.parent_area_name,
                        },
                        "summary": {
                            "clip_count": len(zone_manifest_rows),
                            "group_count": len(zone_group_payloads),
                        },
                        "groups": zone_group_payloads,
                    }
                    zone_manifest_path = zone_root / ZONE_MANIFEST_FILENAME
                    zone_manifest_path.write_text(json.dumps(zone_manifest, indent=2), encoding="utf-8")
                    export_index["zones"].append(
                        {
                            "zone_id": zone.area_id,
                            "zone_name": zone.friendly_name,
                            "family_name": zone.family_name,
                            "clip_count": len(zone_manifest_rows),
                            "group_count": len(zone_group_payloads),
                            "manifest": zone_manifest_path.relative_to(output_dir).as_posix(),
                        }
                    )

            if export_index["zones"]:
                export_index["zones"].sort(key=lambda row: (str(row["family_name"]).lower(), str(row["zone_name"]).lower()))
                (output_dir / EXPORT_INDEX_FILENAME).write_text(json.dumps(export_index, indent=2), encoding="utf-8")

            self.ui_queue.put(("progress", 100.0))
            self.ui_queue.put(("status", "Extraction complete."))
            self.ui_queue.put(("log", f"Done. Failed clips: {failed}"))
            self.ui_queue.put(("bridge_done", {"ok": True, "failed": failed, "output": str(output_dir)}))
        except Exception as exc:  # noqa: BLE001
            self.ui_queue.put(("error", f"Extraction failed: {exc}"))
            self.ui_queue.put(("log", traceback.format_exc()))
            self.ui_queue.put(("status", "Extraction failed."))

    def _start_agent_bridge(self) -> None:
        try:
            self._agent_bridge = AgentBridge(self.ui_queue, port=AGENT_PORT)
            self._agent_bridge.start()
            self.agent_status_var.set(f"Agent listening on 127.0.0.1:{AGENT_PORT}")
            self._log_ui(f"Agent bridge ready at http://127.0.0.1:{AGENT_PORT}")
        except OSError as exc:
            self.agent_status_var.set("Agent bridge did not start")
            self._log_ui(f"Agent bridge failed: {exc}")

    def _note_agent(self) -> None:
        self._agent_seen_at = time.time()
        self.agent_status_var.set(f"Agent connected · 127.0.0.1:{AGENT_PORT}")

    def _begin_bridge_job(self, kind: str, box: dict, event: threading.Event) -> None:
        if self._bridge_job is not None:
            raise RuntimeError("Another agent request is still running.")
        box["pending"] = True
        self._bridge_job = {"kind": kind, "box": box, "event": event}

    def _finish_bridge_job(self, result: dict) -> None:
        job = self._bridge_job
        if not job:
            return
        self._bridge_job = None
        job["box"]["result"] = result
        job["event"].set()

    def _agent_status(self) -> dict[str, object]:
        busy = bool(self.worker_thread and self.worker_thread.is_alive())
        return {
            "ok": True,
            "app": APP_TITLE,
            "version": APP_VERSION,
            "port": AGENT_PORT,
            "build": self.build_var.get(),
            "version": APP_VERSION,
            "client": self.version_var.get(),
            "versions": [item.choice for item in self._installs],
            "wow_path": self.wow_path_var.get(),
            "status": self.status_var.get(),
            "output": self.output_path_var.get(),
            "zones_loaded": len(self.zones),
            "clips_loaded": len(self.asset_entries),
            "busy": busy,
            "selected_zones": [zone.area_id for zone in self._selected_zones()],
            "layers": self._layer_summary() if self.asset_entries else [],
        }

    def _agent_search_zones(self, body: dict) -> dict[str, object]:
        if not self.zones:
            return {"ok": False, "error": "Load zones first."}
        query = str(body.get("query") or "").strip().lower()
        limit = max(1, min(50, _to_int(body.get("limit")) or 20))
        matches = []
        for zone in self.zones:
            blob = f"{zone.area_name} {zone.friendly_name} {zone.zone_name} {zone.parent_area_name} {zone.area_id}".lower()
            if query and query not in blob:
                continue
            matches.append(
                {
                    "area_id": zone.area_id,
                    "name": zone.friendly_name,
                    "parent": zone.parent_area_name,
                    "ambience_id": zone.ambience_id,
                    "zone_music_id": zone.zone_music_id,
                }
            )
            if len(matches) >= limit:
                break
        return {"ok": True, "matches": matches}

    def _find_zone_for_agent(self, body: dict) -> ZoneInfo:
        area_id = _to_int(body.get("area_id"))
        if area_id:
            found = next((zone for zone in self.zones if zone.area_id == area_id), None)
            if not found:
                raise RuntimeError(f"No loaded zone with area id {area_id}.")
            return found
        name = str(body.get("name") or "").strip().lower()
        if not name:
            raise RuntimeError("Pass area_id or name.")
        exact = [zone for zone in self.zones if zone.area_name.lower() == name or zone.friendly_name.lower() == name]
        if exact:
            exact.sort(key=lambda zone: (zone.parent_area_id != 0, zone.area_id))
            return exact[0]
        partial = [zone for zone in self.zones if name in zone.area_name.lower() or name in zone.friendly_name.lower()]
        if len(partial) == 1:
            return partial[0]
        sample = ", ".join(f"{zone.friendly_name} [{zone.area_id}]" for zone in partial[:8])
        raise RuntimeError(f"Zone name is not unique. Matches: {sample}" if sample else f"No zone named {name}.")

    def _select_zone_ids(self, area_ids: list[int]) -> list[ZoneInfo]:
        wanted = set(area_ids)
        self.search_var.set("")
        self._apply_zone_filter()
        self.zone_listbox.selection_clear(0, tk.END)
        chosen: list[ZoneInfo] = []
        for list_idx, zone_idx in enumerate(self.filtered_indices):
            zone = self.zones[zone_idx]
            if zone.area_id in wanted:
                self.zone_listbox.selection_set(list_idx)
                self.zone_listbox.see(list_idx)
                chosen.append(zone)
        return chosen

    def _agent_clip_row(self, entry: AudioEntry) -> dict[str, object]:
        playback = self._entry_playback(entry)
        override = self._entry_override(entry)
        return {
            "key": self._entry_key(entry),
            "area_id": entry.zone_id,
            "area": entry.zone_name,
            "file_data_id": entry.file_data_id,
            "sound_kit_id": entry.sound_kit_id,
            "title": self._entry_title(entry),
            "role": playback.get("role"),
            "time_of_day": entry.time_of_day,
            "volume": playback.get("volume"),
            "how": playback.get("how"),
            "checked": self._entry_checked(entry),
            "tagged": self._entry_tagged(entry),
            "client_file": override.get("lookup_path") or "",
        }

    def _agent_dispatch(self, path: str, body: dict, box: dict, event: threading.Event) -> None:
        self._note_agent()
        body = body or {}
        if path == "/status":
            box["result"] = self._agent_status()
            return
        if path == "/zones/search":
            box["result"] = self._agent_search_zones(body)
            return
        if path == "/zones/load":
            if body.get("wow_path"):
                self.wow_path_var.set(str(body["wow_path"]))
            if body.get("build"):
                self.build_var.set(str(body["build"]))
            if self.zones and not body.get("force"):
                box["result"] = {"ok": True, "cached": True, "build": self.build_var.get(), "zones": len(self.zones)}
                return
            self._begin_bridge_job("load_zones", box, event)
            if not self._run_worker(self._worker_load_zones, bool(body.get("force")), warn_if_busy=False):
                self._bridge_job = None
                box["pending"] = False
                box["result"] = {"ok": False, "error": "A task is already running."}
            return
        if path == "/audio/open":
            if not self.zones:
                box["result"] = {"ok": False, "error": "Load zones first."}
                return
            zone = self._find_zone_for_agent(body)
            if "include_music" in body:
                self.extract_music_var.set(bool(body.get("include_music")))
            if "include_ambient" in body:
                self.extract_ambient_var.set(bool(body.get("include_ambient")))
            if "include_subzone_music" in body:
                self.include_subzone_music_var.set(bool(body.get("include_subzone_music")))
            chosen = self._select_zone_ids([zone.area_id])
            if not chosen:
                box["result"] = {"ok": False, "error": f"Could not select {zone.friendly_name}."}
                return
            self._begin_bridge_job("open_zone", box, event)
            if not self._load_assets_for_selected_zones():
                self._bridge_job = None
                box["pending"] = False
                box["result"] = {"ok": False, "error": self.asset_summary_var.get() or "Audio retrieval did not start."}
            return
        if path == "/recipe":
            box["result"] = {"ok": True, "layers": self._layer_summary()}
            return
        if path == "/clips":
            query = str(body.get("query") or "").strip().lower()
            role = str(body.get("role") or "").strip()
            time_of_day = str(body.get("time") or "").strip()
            limit = max(1, min(200, _to_int(body.get("limit")) or 40))
            rows = []
            for entry in self.asset_entries:
                clip = self._agent_clip_row(entry)
                if role and clip["role"] != role:
                    continue
                if time_of_day and clip["time_of_day"] != time_of_day:
                    continue
                if query and query not in json.dumps(clip).lower():
                    continue
                rows.append(clip)
                if len(rows) >= limit:
                    break
            box["result"] = {"ok": True, "shown": len(rows), "loaded": len(self.asset_entries), "clips": rows}
            return
        if path == "/clips/check":
            wanted_ids = { _to_int(value) for value in body.get("file_data_ids") or [] }
            wanted_keys = {str(value) for value in body.get("keys") or []}
            checked = bool(body.get("checked", True))
            matched = [
                entry for entry in self.asset_entries
                if entry.file_data_id in wanted_ids or self._entry_key(entry) in wanted_keys
            ]
            if not matched:
                box["result"] = {"ok": False, "error": "No loaded clips matched."}
                return
            self._set_checked_for_entries(matched, checked)
            self._render_asset_tree()
            box["result"] = {"ok": True, "updated": len(matched), "checked": checked}
            return
        if path == "/extract":
            which = str(body.get("which") or "checked")
            if body.get("format"):
                self.format_var.set(str(body["format"]))
            if body.get("output"):
                self.output_path_var.set(str(body["output"]))
            wanted_ids = {_to_int(value) for value in body.get("file_data_ids") or []}
            if wanted_ids:
                entries = [entry for entry in self.asset_entries if entry.file_data_id in wanted_ids]
            elif which == "visible":
                entries = self._filtered_asset_entries()
            elif which == "tagged":
                entries = [entry for entry in self.asset_entries if self._entry_tagged(entry)]
            else:
                entries = self._checked_entries()
            if not entries:
                box["result"] = {"ok": False, "error": f"There are no {which} clips to extract."}
                return
            self._begin_bridge_job("extract", box, event)
            before = self.worker_thread
            self._start_extract_entries(entries, f"agent {which} clips")
            started = self.worker_thread is not before and self.worker_thread is not None and self.worker_thread.is_alive()
            if not started and self._bridge_job:
                self._bridge_job = None
                box["pending"] = False
                box["result"] = {"ok": False, "error": "Extraction did not start."}
            return
        if path == "/preview":
            file_data_id = _to_int(body.get("file_data_id"))
            match = next((entry for entry in self.asset_entries if entry.file_data_id == file_data_id), None)
            if not match:
                box["result"] = {"ok": False, "error": "That clip is not loaded."}
                return
            ffmpeg = find_ffmpeg()
            if not ffmpeg:
                box["result"] = {"ok": False, "error": "ffmpeg was not found."}
                return
            self.asset_tree.selection_set(self._entry_key(match))
            self._stop_preview()
            self._begin_bridge_job("preview", box, event)
            if not self._run_worker(self._worker_preview_entry, match, ffmpeg, warn_if_busy=False):
                self._bridge_job = None
                box["pending"] = False
                box["result"] = {"ok": False, "error": "A task is already running."}
            return
        if path == "/preview/stop":
            self._stop_preview()
            box["result"] = {"ok": True}
            return
        box["result"] = {"ok": False, "error": f"Unknown agent path: {path}"}

    def _poll_queue(self) -> None:
        try:
            while True:
                item = self.ui_queue.get_nowait()
                kind = item[0]

                if kind == "log":
                    self._log_ui(item[1])
                elif kind == "status":
                    self._set_status(item[1])
                elif kind == "progress":
                    self._set_progress(float(item[1]))
                elif kind == "error":
                    self._log_ui(item[1])
                    agent_waiting = self._bridge_job is not None
                    self._finish_bridge_job({"ok": False, "error": item[1]})
                    if not agent_waiting:
                        messagebox.showerror(APP_TITLE, item[1])
                elif kind == "zones_loaded":
                    zones, build = item[1], item[2]
                    if build == self.build_var.get().strip():
                        self.zones = zones
                        self._loaded_build = build
                        self.build_var.set(build)
                        self._apply_zone_filter()
                        self._log_ui(f"Loaded {pretty_count(len(zones))} zones using build {build}.")
                    self._finish_bridge_job({"ok": True, "build": build, "zones": len(zones)})
                elif kind == "index_ready":
                    build = item[1]
                    payload = self._index_payloads.get(build)
                    if payload and build == self.build_var.get().strip() and self.resolver.install_audio_index(payload):
                        self._log_ui(f"Restored sound indexes for build {build}.")
                        self._set_status(f"Ready — {pretty_count(len(self.zones))} zones remembered.")
                        self.progress_var.set(100.0)
                elif kind == "zones_cached":
                    self._finish_bridge_job({"ok": True, "cached": True, "build": item[1], "zones": len(self.zones)})
                elif kind == "assets_ready":
                    zone_token, entries, resolved_defaults = item[1], item[2], item[3]
                    if zone_token != self._current_zone_token():
                        self._finish_bridge_job({"ok": False, "error": "Zone selection changed before retrieval finished."})
                        self._schedule_asset_refresh()
                        continue
                    self.asset_entries = entries
                    self.asset_entry_map = {self._entry_key(entry): entry for entry in entries}
                    self._apply_resolved_defaults(entries, resolved_defaults)
                    self._update_filter_values()
                    self._refresh_recipe_label()
                    self._render_asset_tree()
                    self._finish_bridge_job({"ok": True, "clips": len(entries), "layers": self._layer_summary()})
                elif kind == "preview_ready":
                    preview_path, title = item[1], item[2]
                    winsound.PlaySound(preview_path, winsound.SND_ASYNC | winsound.SND_FILENAME)
                    self.preview_status_var.set(f"Playing preview: {title}")
                    self._finish_bridge_job({"ok": True, "playing": title})
                elif kind == "bridge_done":
                    self._finish_bridge_job(item[1])
                elif kind == "agent":
                    path, body, box, event = item[1], item[2], item[3], item[4]
                    try:
                        self._agent_dispatch(path, body, box, event)
                    except Exception as exc:  # noqa: BLE001
                        box["result"] = {"ok": False, "error": str(exc)}
                        event.set()
                    else:
                        if not box.get("pending"):
                            event.set()
        except queue.Empty:
            pass
        self.after(120, self._poll_queue)


def main() -> None:
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()
