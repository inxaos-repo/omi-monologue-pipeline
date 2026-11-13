"""Read-only transcript review UI (GET /review), served on its own port (OMI_REVIEW_PORT, 8081).

Security model: the review port is NOT exposed on the LAN LoadBalancer. It is only reached via
the Authentik embedded outpost (proxy provider), which adds X-authentik-username. Requests
without that header are refused unless OMI_REVIEW_REQUIRE_SSO=0 (local dev).

Data sources (no writes, ever):
- <intake>/.omi-state.json      status per file (stored/deferred/failed/published)
- <archive>/<stem>.result.json  transcript, title, summary, wiki_url, todoist tasks
- audio in intake or archive    played via /review/audio/<name>; gone after 7-day retention
"""
from __future__ import annotations

import datetime as dt
import html
import json
import os
import re
import struct
import wave
from pathlib import Path
from urllib.parse import quote, urlencode

from .metrics import device_of
from .watcher import AUDIO_EXT, STATE_NAME

_TS_RE = re.compile(r"-(\d{8}-\d{6})(?:-\d+)?\.[A-Za-z0-9]+$")
_SAFE = re.compile(r"^[A-Za-z0-9_.-]+$")
MIME = {".ogg": "audio/ogg", ".opus": "audio/ogg", ".wav": "audio/wav", ".m4a": "audio/mp4",
        ".mp3": "audio/mpeg", ".flac": "audio/flac", ".webm": "audio/webm"}


def duration(f: Path) -> float | None:
    """Seconds of audio; Ogg via last page granule (48 kHz Opus), WAV via header."""
    try:
        if f.suffix.lower() in (".ogg", ".opus"):
            size = f.stat().st_size
            with f.open("rb") as fh:
                fh.seek(max(0, size - 65536))
                tail = fh.read()
                fh.seek(0)
                head = fh.read(4096)
            i = tail.rfind(b"OggS")
            if i < 0 or i + 14 > len(tail):
                return None
            (gran,) = struct.unpack_from("<q", tail, i + 6)
            pre = 0
            j = head.find(b"OpusHead")
            if j >= 0 and j + 12 <= len(head):
                (pre,) = struct.unpack_from("<H", head, j + 10)
            return max(0.0, (gran - pre) / 48000) if gran > 0 else None
        if f.suffix.lower() == ".wav":
            with wave.open(str(f)) as w:
                return w.getnframes() / float(w.getframerate())
    except (OSError, wave.Error, struct.error, EOFError):
        return None
    return None


def _when(name: str, mtime: float | None) -> dt.datetime:
    m = _TS_RE.search(name)
    if m:
        try:
            return dt.datetime.strptime(m.group(1), "%Y%m%d-%H%M%S")
        except ValueError:
            pass
    return dt.datetime.fromtimestamp(mtime or 0)


def _audio_dirs(intake: Path, archive: Path | None) -> list[Path]:
    return [d for d in (intake, archive) if d is not None and Path(d).is_dir()]


def find_audio(name: str, intake: Path, archive: Path | None) -> Path | None:
    if not _SAFE.match(name) or Path(name).suffix.lower() not in AUDIO_EXT:
        return None
    for d in _audio_dirs(intake, archive):
        p = Path(d) / name
        if p.is_file():
            return p
    return None


def load_records(intake: Path, archive: Path | None) -> list[dict]:
    intake = Path(intake)
    try:
        state = json.loads((intake / STATE_NAME).read_text())
    except (OSError, ValueError):
        state = {}
    names: dict[str, dict] = {}
    for d in _audio_dirs(intake, archive):
        for f in Path(d).iterdir():
            if f.is_file() and not f.name.startswith(".") and f.suffix.lower() in AUDIO_EXT \
                    and not f.name.endswith(".part"):
                names.setdefault(f.name, {})["audio"] = f
    for n in state:
        names.setdefault(n, {})
    results: dict[str, dict] = {}
    chunks: dict[str, dict] = {}
    if archive is not None and Path(archive).is_dir():
        for cf in Path(archive).glob("*.chunk.json"):
            try:
                chunks[cf.name[: -len(".chunk.json")]] = json.loads(cf.read_text())
            except (OSError, ValueError):
                continue
    if archive is not None and Path(archive).is_dir():
        for rf in Path(archive).glob("*.result.json"):
            try:
                r = json.loads(rf.read_text())
            except (OSError, ValueError):
                continue
            stem = rf.name[: -len(".result.json")]
            an = Path(r.get("audio") or "").name or stem
            results[stem] = r
            if Path(an).stem == stem and Path(an).suffix:
                names.setdefault(an, {})
    out = []
    for n, info in names.items():
        s = state.get(n, {})
        r = results.get(Path(n).stem) or chunks.get(Path(n).stem, {})
        audio = info.get("audio")
        mtime = None
        if audio is not None:
            try:
                mtime = audio.stat().st_mtime
            except OSError:
                audio = None
        status = s.get("status") or r.get("status") or "stored"
        if status == "dry-run":
            status = "published (dry-run)"
        out.append({
            "name": n,
            "when": _when(n, mtime),
            "device": device_of(n),
            "status": status,
            "error": s.get("error"),
            "attempts": s.get("attempts", 0),
            "deferrals": s.get("deferrals", 0),
            "audio": audio,
            "duration": duration(audio) if audio is not None else None,
            "transcript": r.get("transcript"),
            "title": r.get("title"),
            "summary": r.get("summary"),
            "wiki_url": r.get("wiki_url") or s.get("wiki"),
            "todoist": r.get("todoist") or [],
            "session": s.get("session") or r.get("session"),
        })
    out.sort(key=lambda x: (x["when"], x["name"]), reverse=True)
    return out


def load_sessions(archive: Path | None) -> dict[str, dict]:
    out: dict[str, dict] = {}
    if archive is None or not Path(archive).is_dir():
        return out
    for f in Path(archive).glob("*.session.json"):
        try:
            out[f.name[: -len(".session.json")]] = json.loads(f.read_text())
        except (OSError, ValueError):
            continue
    return out


def _filter(recs: list[dict], q: str, day: str) -> list[dict]:
    if day:
        recs = [r for r in recs if r["when"].strftime("%Y-%m-%d") == day]
    if q:
        ql = q.lower()
        recs = [r for r in recs if any(ql in (r.get(k) or "").lower()
                                       for k in ("transcript", "title", "summary", "name", "device"))]
    return recs


CSS = """
body{font-family:system-ui,sans-serif;margin:0 auto;max-width:960px;padding:1rem;background:#111;color:#ddd}
a{color:#7ab7ff} h1{font-size:1.4rem} h2{font-size:1.1rem;border-bottom:1px solid #333;padding-top:1rem}
form{display:flex;gap:.5rem;flex-wrap:wrap} input{background:#222;color:#ddd;border:1px solid #444;padding:.4rem}
.rec{border:1px solid #2a2a2a;border-radius:6px;padding:.6rem .8rem;margin:.6rem 0;background:#181818}
.meta{font-size:.85rem;color:#999;display:flex;gap:.8rem;flex-wrap:wrap;align-items:center}
.st{padding:0 .4rem;border-radius:4px;font-weight:600;color:#000}
.st-published{background:#5c5}.st-deferred{background:#fc5}.st-failed{background:#f66}.st-stored{background:#9bf}
.tx{white-space:pre-wrap;margin:.4rem 0}.muted{color:#777;font-style:italic}.err{color:#f99;font-size:.8rem}
.sess{border-color:#355}.chunk{margin:.3rem 0 .3rem 1rem;padding:.3rem .6rem}
.st-transcribed{background:#ccc}.st-held{background:#c9f}
audio{height:32px;vertical-align:middle} .days a{margin-right:.5rem}
"""


def _fmt_dur(s: float | None) -> str:
    if s is None:
        return "?"
    s = int(round(s))
    return f"{s // 60}:{s % 60:02d}"


def _chunk_html(r: dict, compact: bool = False) -> list[str]:
    e = html.escape
    p = []
    st = r["status"].split(" ")[0]
    p.append('<div class="rec chunk"><div class="meta">' if compact else '<div class="rec"><div class="meta">')
    p.append(f'<b>{e(r["when"].strftime("%H:%M:%S"))}</b>')
    if not compact or st != "published":
        label = "waiting for session to close" if st == "transcribed" else r["status"]
        p.append(f'<span class="st st-{e(st)}">{e(label)}</span>')
    p.append(f'<span>{e(_fmt_dur(r["duration"]))}</span>')
    if not compact:
        p.append(f'<span>device {e(r["device"])}</span>')
    if r["audio"] is not None:
        mime = MIME.get(r["audio"].suffix.lower(), "audio/ogg")
        p.append(f'<audio controls preload="none"><source src="/review/audio/{quote(r["name"])}" type="{mime}"></audio>')
    else:
        p.append('<span class="muted">audio deleted (7-day retention)</span>')
    if r["wiki_url"] and not compact:
        p.append(f'<a href="{e(r["wiki_url"])}" target="_blank" rel="noopener">wiki page</a>')
    p.append("</div>")
    if r["title"] and not compact:
        p.append(f"<div><b>{e(r['title'])}</b></div>")
    if r["transcript"]:
        p.append(f'<div class="tx">{e(r["transcript"])}</div>')
    else:
        p.append('<div class="muted">no transcript yet</div>')
    if r["todoist"] and not compact:
        p.append(_todo_html(r["todoist"]))
    if r["error"] and r["status"] in ("deferred", "failed"):
        p.append(f'<div class="err">{e(r["error"][:300])} (attempts {r["attempts"]}, deferrals {r["deferrals"]})</div>')
    p.append(f'<div class="meta">{e(r["name"])}</div></div>')
    return p


def _todo_html(tasks: list[dict]) -> str:
    e = html.escape
    return "<div>Todoist: " + "; ".join(
        f'<a href="https://app.todoist.com/app/task/{quote(str(t.get("id", "")))}" target="_blank" rel="noopener">'
        f'{e(t.get("content", ""))}</a>' for t in tasks) + "</div>"


def render_page(recs: list[dict], q: str = "", day: str = "", user: str | None = None,
                sessions: dict[str, dict] | None = None) -> str:
    sessions = sessions or {}
    e = html.escape
    days: dict[str, int] = {}
    for r in recs:
        k = r["when"].strftime("%Y-%m-%d")
        days[k] = days.get(k, 0) + 1
    shown = _filter(recs, q, day)
    p = ['<!doctype html><html><head><meta charset="utf-8">',
         '<meta name="viewport" content="width=device-width,initial-scale=1">',
         f"<title>Omi review</title><style>{CSS}</style></head><body>",
         '<h1><a href="/review">Omi transcripts</a></h1>',
         '<form method="get" action="/review">',
         f'<input type="search" name="q" value="{e(q)}" placeholder="search transcripts" size="30">',
         f'<input type="date" name="day" value="{e(day)}"><input type="submit" value="Filter">',
         '<a href="/review">clear</a></form>',
         '<p class="days">Days: ' + " ".join(
             f'<a href="/review?{urlencode({"day": k})}">{k}</a> ({n})' for k, n in sorted(days.items(), reverse=True)[:30])
         + "</p>",
         f'<p class="meta">{len(shown)} of {len(recs)} recordings. Read-only. Raw audio is kept 7 days.'
         + (f" Signed in as {e(user)}." if user else "") + "</p>"]
    cur = None
    done: set[str] = set()
    for r in shown:
        d = r["when"].strftime("%Y-%m-%d (%a)")
        if d != cur:
            p.append(f"<h2>{e(d)}</h2>")
            cur = d
        sid = r.get("session")
        if not sid:
            p += _chunk_html(r)
            continue
        if sid in done:
            continue
        done.add(sid)
        members = sorted((x for x in shown if x.get("session") == sid), key=lambda x: x["when"])
        sm = sessions.get(sid, {})
        total = sum(x["duration"] or 0 for x in members)
        p.append('<div class="rec sess"><div class="meta">')
        p.append(f'<b>{e(members[0]["when"].strftime("%H:%M"))}-{e(members[-1]["when"].strftime("%H:%M"))}</b>')
        held = sm.get("status") == "held" or r["status"] == "held"
        p.append('<span class="st st-held">session held (not published)</span>' if held
                 else '<span class="st st-published">session</span>')
        p.append(f'<span>{len(members)} chunks, {e(_fmt_dur(total))}</span><span>device {e(r["device"])}</span>')
        if sm.get("wiki_url") or r["wiki_url"]:
            p.append(f'<a href="{e(sm.get("wiki_url") or r["wiki_url"])}" target="_blank" rel="noopener">wiki page</a>')
        p.append("</div>")
        if sm.get("title") or r["title"]:
            p.append(f"<div><b>{e(sm.get('title') or r['title'])}</b></div>")
        if sm.get("summary"):
            p.append(f'<div class="tx">{e(sm["summary"])}</div>')
        elif sm.get("note"):
            p.append(f'<div class="muted">{e(sm["note"])} (not published)</div>')
        if sm.get("todoist") or r["todoist"]:
            p.append(_todo_html(sm.get("todoist") or r["todoist"]))
        p.append(f'<details><summary>{len(members)} chunks: transcript and audio</summary>')
        for m in members:
            p += _chunk_html(m, compact=True)
        p.append(f'</details><div class="meta">{e(sid)}</div></div>')
    if not shown:
        p.append('<p class="muted">No recordings match.</p>')
    p.append("</body></html>")
    return "\n".join(p)


def make_review_handler(intake: Path, archive: Path | None, require_sso: bool | None = None):
    from http.server import BaseHTTPRequestHandler
    from urllib.parse import parse_qs, unquote, urlparse
    if require_sso is None:
        require_sso = os.environ.get("OMI_REVIEW_REQUIRE_SSO", "1") != "0"

    class R(BaseHTTPRequestHandler):
        server_version = "omi-review/1"

        def log_message(self, fmt, *a):
            pass

        def _send(self, code, body: bytes, ctype: str, extra: dict | None = None):
            self.send_response(code)
            self.send_header("content-type", ctype)
            self.send_header("content-length", str(len(body)))
            self.send_header("cache-control", "no-store")
            self.send_header("x-content-type-options", "nosniff")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def do_HEAD(self):
            self.do_GET()

        def do_GET(self):
            u = urlparse(self.path)
            if u.path == "/healthz":
                return self._send(200, b"ok", "text/plain")
            user = self.headers.get("x-authentik-username")
            if require_sso and not user:
                return self._send(403, b"forbidden: open via https://omi.k8s.inxaos.com/review (SSO)", "text/plain")
            if u.path in ("/", "/review/"):
                return self._send(302, b"", "text/plain", {"location": "/review"})
            if u.path == "/review":
                q = {k: v[0] for k, v in parse_qs(u.query).items()}
                day = q.get("day", "")
                if day and not re.match(r"^\d{4}-\d{2}-\d{2}$", day):
                    day = ""
                recs = load_records(intake, archive)
                body = render_page(recs, q.get("q", "")[:200], day, user, load_sessions(archive)).encode()
                return self._send(200, body, "text/html; charset=utf-8")
            if u.path.startswith("/review/audio/"):
                f = find_audio(unquote(u.path[len("/review/audio/"):]), intake, archive)
                if f is None:
                    return self._send(404, b"audio not found (deleted after 7 days?)", "text/plain")
                return self._send_file(f)
            self._send(404, b"not found", "text/plain")

        def _send_file(self, f: Path):
            data = f.read_bytes()
            ctype = MIME.get(f.suffix.lower(), "application/octet-stream")
            rng = self.headers.get("range", "")
            m = re.match(r"bytes=(\d*)-(\d*)$", rng.strip())
            if m and (m.group(1) or m.group(2)):
                n = len(data)
                if m.group(1):
                    a = int(m.group(1))
                    b = int(m.group(2)) if m.group(2) else n - 1
                else:
                    a, b = max(0, n - int(m.group(2))), n - 1
                b = min(b, n - 1)
                if a > b:
                    return self._send(416, b"", "text/plain", {"content-range": f"bytes */{n}"})
                return self._send(206, data[a:b + 1], ctype,
                                  {"content-range": f"bytes {a}-{b}/{n}", "accept-ranges": "bytes"})
            self._send(200, data, ctype, {"accept-ranges": "bytes"})

    return R


def serve_review_background(intake: Path, archive: Path | None, host: str, port: int):
    import sys
    import threading
    from http.server import ThreadingHTTPServer
    srv = ThreadingHTTPServer((host, port), make_review_handler(Path(intake), archive and Path(archive)))
    threading.Thread(target=srv.serve_forever, name="review", daemon=True).start()
    print(f"[omi-review] listening on {host}:{srv.server_address[1]} (read-only)", file=sys.stderr, flush=True)
    return srv
