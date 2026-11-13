"""Prometheus text-format metrics, stdlib only (no prometheus_client dependency).

Counters are process-local (reset on restart; use rate()/increase() in Grafana).
Gauges that describe state on disk (pending queue, last segment per device) are computed
at scrape time from the intake folder, so they survive restarts.
"""
from __future__ import annotations

import json
import re
import threading
import time
import urllib.request
from pathlib import Path

_lock = threading.Lock()
_counters: dict[tuple[str, tuple], float] = {}
_bridge: dict[str, dict] = {}   # device -> {field: value, "_ts": epoch}

COUNTER_HELP = {
    "omi_segments_stored_total": "Audio segments received by the ingest endpoint",
    "omi_segments_transcribed_total": "Segments that were transcribed (handler returned a result)",
    "omi_segments_published_total": "Segments published to Wiki.js/Todoist",
    "omi_segments_deferred_total": "Processing attempts deferred by a transient error",
    "omi_segments_failed_total": "Processing attempts that failed",
    "omi_bridge_health_reports_total": "Bridge health reports received",
    "omi_wiki_index_failures_total": "Failed rebuilds of the journal/monologues wiki index (retried)",
    "omi_sessions_published_total": "Sessions closed and published",
    "omi_sessions_held_total": "Sessions closed while publishing was paused (not published)",
    "omi_sessions_failed_total": "Session publish attempts that failed (retried)",
}
# bridge health fields accepted by /v1/bridge-health (all numeric)
BRIDGE_FIELDS = ("rssi", "wifi", "ble", "ram_q", "ram_kb", "spool", "spool_free_kb",
                 "posts_ok", "posts_fail", "lost", "psram_free_kb", "uptime_s")
_NAME_RE = re.compile(r"^omi-(?P<dev>.+)-(?P<ts>\d{8}-\d{6})(?:-\d+)?\.[A-Za-z0-9]+$")


def inc(name: str, value: float = 1, **labels) -> None:
    key = (name, tuple(sorted(labels.items())))
    with _lock:
        _counters[key] = _counters.get(key, 0) + value


_gauges: dict[tuple[str, tuple], float] = {}


def set_gauge(name: str, value: float, **labels) -> None:
    with _lock:
        _gauges[(name, tuple(sorted(labels.items())))] = value


def reset() -> None:  # tests
    with _lock:
        _counters.clear()
        _bridge.clear()
        _gauges.clear()
    _speaches_cache.update(t=0.0, up=None)


def record_bridge(device: str, data: dict, now: float | None = None) -> dict:
    vals = {}
    for k in BRIDGE_FIELDS:
        v = data.get(k)
        if isinstance(v, bool):
            v = int(v)
        if isinstance(v, (int, float)):
            vals[k] = float(v)
    vals["_ts"] = now or time.time()
    with _lock:
        _bridge[device] = vals
    inc("omi_bridge_health_reports_total", device=device)
    return vals


def device_of(name: str) -> str:
    m = _NAME_RE.match(name)
    return m.group("dev") if m else "unknown"


def _esc(v) -> str:
    return str(v).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _fmt(name: str, labels: dict | tuple, v: float) -> str:
    items = labels.items() if isinstance(labels, dict) else labels
    lab = ",".join(f'{k}="{_esc(x)}"' for k, x in items)
    v = float(v)
    val = str(int(v)) if v.is_integer() and abs(v) < 2**53 else repr(v)
    return f"{name}{{{lab}}} {val}" if lab else f"{name} {val}"


_speaches_cache = {"t": 0.0, "up": None}


def speaches_up(whisper_url: str | None, ttl: float = 30, timeout: float = 2) -> int | None:
    """1 if the speaches/Whisper server answers /health (or /v1/models), else 0. Cached ttl s."""
    if not whisper_url:
        return None
    now = time.time()
    if _speaches_cache["up"] is not None and now - _speaches_cache["t"] < ttl:
        return _speaches_cache["up"]
    base = whisper_url.split("/v1/")[0].rstrip("/")
    up = 0
    for path in ("/health", "/v1/models"):
        try:
            with urllib.request.urlopen(base + path, timeout=timeout) as r:
                if r.status < 400:
                    up = 1
                    break
        except Exception:
            continue
    _speaches_cache.update(t=now, up=up)
    return up


def _intake_gauges(intake: Path | None) -> list[str]:
    from .watcher import AUDIO_EXT, STATE_NAME
    out: list[str] = []
    if intake is None or not Path(intake).is_dir():
        return out
    intake = Path(intake)
    try:
        state = json.loads((intake / STATE_NAME).read_text())
    except (OSError, ValueError):
        state = {}
    pending, by_status, last = 0, {}, {}
    for f in intake.iterdir():
        if f.name.startswith(".") or f.suffix.lower() not in AUDIO_EXT or not f.is_file():
            continue
        s = state.get(f.name, {}).get("status")
        if s not in ("published", "dry-run", "held"):
            pending += 1
        try:
            mt = f.stat().st_mtime
        except OSError:
            continue
        d = device_of(f.name)
        last[d] = max(last.get(d, 0), mt)
    for s in state.values():
        st = s.get("status", "unknown")
        by_status[st] = by_status.get(st, 0) + 1
    out += ["# HELP omi_pending_queue Audio files in intake not yet published (new, deferred or failed)",
            "# TYPE omi_pending_queue gauge", _fmt("omi_pending_queue", {}, pending)]
    out += ["# HELP omi_state_files Files in the watcher state by status",
            "# TYPE omi_state_files gauge"]
    out += [_fmt("omi_state_files", {"status": k}, v) for k, v in sorted(by_status.items())]
    out += ["# HELP omi_last_segment_timestamp_seconds Start time (mtime) of the newest audio file per device in intake",
            "# TYPE omi_last_segment_timestamp_seconds gauge"]
    out += [_fmt("omi_last_segment_timestamp_seconds", {"device": d}, t) for d, t in sorted(last.items())]
    return out


def render(intake: Path | None = None, whisper_url: str | None = None) -> str:
    lines: list[str] = []
    with _lock:
        counters = dict(_counters)
        bridge = {d: dict(v) for d, v in _bridge.items()}
        gauges = dict(_gauges)
    for name, help_ in COUNTER_HELP.items():
        lines += [f"# HELP {name} {help_}", f"# TYPE {name} counter"]
        series = [(k[1], v) for k, v in counters.items() if k[0] == name]
        if not series and name not in ("omi_bridge_health_reports_total", "omi_wiki_index_failures_total"):
            series = [((), 0)]
        lines += [_fmt(name, lab, v) for lab, v in sorted(series)]
    lines += _intake_gauges(intake)
    for gname in sorted({k[0] for k in gauges}):
        lines += [f"# TYPE {gname} gauge"]
        lines += [_fmt(gname, lab, v) for (n, lab), v in sorted(gauges.items()) if n == gname]
    up = speaches_up(whisper_url)
    if up is not None:
        lines += ["# HELP omi_speaches_up 1 if the Whisper (speaches) server on Ix answers",
                  "# TYPE omi_speaches_up gauge", _fmt("omi_speaches_up", {}, up)]
    if bridge:
        for k in BRIDGE_FIELDS + ("_ts",):
            name = "omi_bridge_last_report_timestamp_seconds" if k == "_ts" else f"omi_bridge_{k}"
            rows = [(d, v[k]) for d, v in sorted(bridge.items()) if k in v]
            if rows:
                lines += [f"# HELP {name} Bridge health report field {k}", f"# TYPE {name} gauge"]
                lines += [_fmt(name, {"device": d}, x) for d, x in rows]
    return "\n".join(lines) + "\n"
