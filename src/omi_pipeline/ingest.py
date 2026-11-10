"""HTTP ingest endpoint for ESP32-S3 BLE bridges (and anything else that has audio).

Runs in the same container as the watcher (`omi-pipeline serve`). Bridges POST either
finished files or raw Opus frames; everything lands in the intake dir as `<name>.part`
then is atomically renamed, so the watcher only sees complete files.

Endpoints (all POST bodies raw, `Authorization: Bearer $OMI_INGEST_TOKEN` if set):
  GET  /healthz                         -> 200 "ok"
  POST /v1/segments?device=D&start=EPOCH&ext=ogg
        body = complete audio file (ogg/opus/wav/m4a/mp3/flac/webm)
  POST /v1/frames?device=D&start=EPOCH[&format=lp16|omi-sd][&codec=21]
        body = raw Opus frames, muxed here into Ogg Opus:
          lp16   (default): repeated [len u16 LE][opus frame]   - what a live-stream bridge sends
          omi-sd          : pendant storage bytes (440-byte blocks of [len u8][frame]),
                            forwarded verbatim from a 3.0.19 storage READ (block-aligned)
Response: 201 {"file": "<name>", "frames": N, "bytes": N}
Errors: 400 bad input, 401 bad token, 413 too large, 415 unsupported codec/ext.

`start` (unix epoch of first sample) becomes the file mtime, which the pipeline uses as the
page date; if missing/implausible the server receive time is used.
"""
from __future__ import annotations

import datetime as dt
import hmac
import json
import os
import re
import struct
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import omi_protocol as O

SEGMENT_EXT = {"ogg", "opus", "wav", "m4a", "mp3", "flac", "webm"}
MAX_BODY = int(os.environ.get("OMI_INGEST_MAX_BYTES", 64 * 1024 * 1024))
_lock = threading.Lock()


def log(m: str) -> None:
    print(f"[omi-ingest] {m}", file=sys.stderr, flush=True)


def parse_lp16(body: bytes) -> list[bytes]:
    frames, o = [], 0
    while o < len(body):
        if o + 2 > len(body):
            raise ValueError("truncated length prefix")
        (n,) = struct.unpack_from("<H", body, o)
        if n == 0 or n > 1275 or o + 2 + n > len(body):
            raise ValueError(f"bad frame length {n} at offset {o}")
        frames.append(body[o + 2:o + 2 + n])
        o += 2 + n
    return frames


def _safe(s: str, default: str) -> str:
    s = re.sub(r"[^A-Za-z0-9_-]", "", s or "")[:32]
    return s or default


def store(intake: Path, data: bytes, ext: str, device: str, start: float) -> Path:
    intake.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.fromtimestamp(start).strftime("%Y%m%d-%H%M%S")
    with _lock:
        base = f"omi-{device}-{stamp}"
        final, n = intake / f"{base}.{ext}", 2
        while final.exists() or final.with_name(final.name + ".part").exists():
            final, n = intake / f"{base}-{n}.{ext}", n + 1
        part = final.with_name(final.name + ".part")
        part.write_bytes(data)
    os.utime(part, (start, start))
    part.rename(final)
    return final


def make_handler(intake: Path, token: str | None):
    class H(BaseHTTPRequestHandler):
        server_version = "omi-ingest/1"

        def log_message(self, fmt, *a):  # quieter default logging
            log(f"{self.client_address[0]} {fmt % a}")

        def _reply(self, code: int, obj) -> None:
            body = (json.dumps(obj) if not isinstance(obj, str) else obj).encode()
            self.send_response(code)
            self.send_header("content-type", "application/json" if not isinstance(obj, str) else "text/plain")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if urlparse(self.path).path == "/healthz":
                return self._reply(200, "ok")
            self._reply(404, {"error": "not found"})

        def do_POST(self):
            u = urlparse(self.path)
            q = {k: v[0] for k, v in parse_qs(u.query).items()}
            if token:
                got = self.headers.get("authorization", "")
                if not hmac.compare_digest(got, f"Bearer {token}"):
                    return self._reply(401, {"error": "unauthorized"})
            try:
                n = int(self.headers.get("content-length", "0"))
            except ValueError:
                return self._reply(400, {"error": "bad content-length"})
            if n <= 0:
                return self._reply(400, {"error": "empty body"})
            if n > MAX_BODY:
                return self._reply(413, {"error": f"body > {MAX_BODY}"})
            body = self.rfile.read(n)
            device = _safe(q.get("device", ""), "bridge")
            try:
                start = float(q.get("start", "0"))
            except ValueError:
                start = 0
            if not 1_600_000_000 < start < time.time() + 86400:
                start = time.time()
            try:
                if u.path == "/v1/segments":
                    ext = _safe(q.get("ext", "ogg"), "ogg").lower()
                    if ext not in SEGMENT_EXT:
                        return self._reply(415, {"error": f"ext {ext} not allowed"})
                    f = store(intake, body, ext, device, start)
                    frames = None
                elif u.path == "/v1/frames":
                    codec = int(q.get("codec", O.CODEC_OPUS_FS320))
                    if codec not in (O.CODEC_OPUS, O.CODEC_OPUS_FS320):
                        return self._reply(415, {"error": f"codec {codec} unsupported (Opus only)"})
                    fmt = q.get("format", "lp16")
                    if fmt == "lp16":
                        fl = parse_lp16(body)
                    elif fmt == "omi-sd":
                        fl = list(O.iter_stored_frames(body))
                    else:
                        return self._reply(400, {"error": f"unknown format {fmt}"})
                    if not fl:
                        return self._reply(400, {"error": "no frames"})
                    f = store(intake, O.ogg_opus(fl), "ogg", device, start)
                    frames = len(fl)
                else:
                    return self._reply(404, {"error": "not found"})
            except ValueError as e:
                return self._reply(400, {"error": str(e)})
            log(f"stored {f.name} ({len(body)} B, frames={frames}) from {device}")
            self._reply(201, {"file": f.name, "frames": frames, "bytes": len(body)})

    return H


def make_server(intake: Path, host: str = "0.0.0.0", port: int = 8080, token: str | None = None):
    return ThreadingHTTPServer((host, port), make_handler(Path(intake), token))


def serve_background(intake: Path, host: str, port: int, token: str | None):
    srv = make_server(intake, host, port, token)
    t = threading.Thread(target=srv.serve_forever, name="ingest", daemon=True)
    t.start()
    log(f"listening on {host}:{srv.server_address[1]} -> {intake} (auth {'on' if token else 'OFF'})")
    return srv
