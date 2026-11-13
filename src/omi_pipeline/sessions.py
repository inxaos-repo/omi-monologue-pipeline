"""Session aggregation: many short bridge chunks -> one monologue page.

Stage 1 (per file, in the watcher): transcribe the chunk, write <archive>/<stem>.chunk.json,
state status = "transcribed".
Stage 2 (tick, after each scan): per device, group transcribed chunks into sessions split by a
pause (>= OMI_SESSION_PAUSE seconds without audio, default 300) or by a day change. A session
closes once its last chunk is older than the pause (or the day changed). Closing = ONE LLM pass
over the joined transcript, ONE wiki page, one Todoist pass; every chunk is then marked
"published" with its session id (so retention can delete the audio after 7 days).
After each publish the wiki index page (<prefix>, e.g. journal/monologues) is regenerated.
"""
from __future__ import annotations

import datetime as dt
import json
import sys
import time
from pathlib import Path
from typing import Callable

from . import core
from .watcher import STATE_NAME

INDEX_MARKER = "<!-- omi-pipeline:index (generated; edits are overwritten) -->"


def log(m: str) -> None:
    print(f"[omi-session] {m}", file=sys.stderr, flush=True)


def chunk_meta(audio: Path, duration: float | None) -> dict:
    from .metrics import device_of
    end = audio.stat().st_mtime
    return {"device": device_of(audio.name), "start": end - (duration or 0), "end": end}


def _day(ts: float) -> str:
    return dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%d")


def group(chunks: list[dict], pause: float) -> list[list[dict]]:
    """Split chunks (any devices) into sessions: same device, gap < pause, same local day."""
    out: list[list[dict]] = []
    by_dev: dict[str, list[dict]] = {}
    for c in chunks:
        by_dev.setdefault(c["device"], []).append(c)
    for dev, cs in by_dev.items():
        cs.sort(key=lambda c: (c["start"], c["name"]))
        cur: list[dict] = []
        for c in cs:
            if cur and (c["start"] - cur[-1]["end"] >= pause or _day(c["start"]) != _day(cur[0]["start"])):
                out.append(cur)
                cur = []
            cur.append(c)
        if cur:
            out.append(cur)
    return out


def is_closed(sess: list[dict], all_groups: list[list[dict]], pause: float, now: float) -> bool:
    last = sess[-1]
    later = any(g is not sess and g[0]["device"] == last["device"] and g[0]["start"] > last["start"]
                for g in all_groups)
    return later or now - last["end"] >= pause or _day(now) != _day(sess[0]["start"])


def session_id(sess: list[dict]) -> str:
    return f"session-{sess[0]['device']}-{dt.datetime.fromtimestamp(sess[0]['start']).strftime('%Y%m%d-%H%M%S')}"


class SessionCloser:
    """Watcher tick hook. publish(session_chunks, sid) -> result dict (does the external writes)."""

    def __init__(self, intake: Path, archive: Path, publish: Callable[[list[dict], str], dict],
                 pause: float = 300, retry: float = 300, now: Callable[[], float] = time.time):
        self.intake, self.archive = Path(intake), Path(archive)
        self.publish, self.pause, self.retry, self.now = publish, pause, retry, now
        self._failed: dict[str, float] = {}

    def _load(self) -> tuple[dict, list[dict]]:
        p = self.intake / STATE_NAME
        state = json.loads(p.read_text()) if p.exists() else {}
        chunks = []
        for name, s in state.items():
            if s.get("status") != "transcribed":
                continue
            cf = self.archive / f"{Path(name).stem}.chunk.json"
            try:
                c = json.loads(cf.read_text())
            except (OSError, ValueError):
                continue
            c["name"] = name
            chunks.append(c)
        return state, chunks

    def __call__(self) -> bool:
        try:
            return self._run()
        except Exception as e:  # never kill the watcher
            log(f"session pass failed: {e}")
            return False

    def _run(self) -> bool:
        from . import metrics
        state, chunks = self._load()
        if not chunks:
            return False
        groups = group(chunks, self.pause)
        now, changed = self.now(), False
        for sess in groups:
            if not is_closed(sess, groups, self.pause, now):
                continue
            sid = session_id(sess)
            if now - self._failed.get(sid, float("-inf")) < self.retry:
                continue
            try:
                r = self.publish(sess, sid)
            except Exception as e:
                self._failed[sid] = now
                metrics.inc("omi_sessions_failed_total")
                log(f"{sid}: publish failed ({e}); retry in {self.retry:.0f}s")
                continue
            self._failed.pop(sid, None)
            r.update(id=sid, device=sess[0]["device"], start=sess[0]["start"], end=sess[-1]["end"],
                     chunks=[c["name"] for c in sess])
            (self.archive / f"{sid}.session.json").write_text(json.dumps(r, indent=2))
            # re-read state: the watcher may have added files meanwhile (same thread, but be safe)
            p = self.intake / STATE_NAME
            state = json.loads(p.read_text()) if p.exists() else state
            st = "held" if r.get("status") == "held" else "published"
            for c in sess:
                s = state.setdefault(c["name"], {})
                s.update(status=st, session=sid, wiki=r.get("wiki_url"))
                res = {"status": st, "session": sid, "transcript": c.get("transcript", ""),
                       "audio": c.get("audio"), "wiki_url": r.get("wiki_url"), "title": r.get("title"),
                       "todoist": r.get("todoist", []), "date": _day(c["start"])}
                (self.archive / f"{Path(c['name']).stem}.result.json").write_text(json.dumps(res, indent=2))
                if st == "published":
                    metrics.inc("omi_segments_published_total")
            tmp = p.with_suffix(".tmp")
            tmp.write_text(json.dumps(state, indent=2, sort_keys=True))
            tmp.replace(p)
            metrics.inc("omi_sessions_published_total" if st == "published" else "omi_sessions_held_total")
            log(f"{sid}: {len(sess)} chunks -> {r.get('status')} {r.get('wiki_url') or ''}")
            changed = True
        return changed


# ---- wiki index page ------------------------------------------------------
import re as _re
import threading as _th

_DATE = _re.compile(r"^(\d{4}-\d{2}-\d{2})")


def index_from_pages(pages: list[dict], prefix: str) -> list[dict]:
    """Wiki page list (source of truth) -> entries newest day first, newest page first within a day."""
    out = []
    for p in pages:
        path = p["path"]
        if not path.startswith(prefix + "/"):
            continue
        leaf = path[len(prefix) + 1:]
        m = _DATE.match(leaf)
        out.append({"date": m.group(1) if m else (p.get("createdAt") or "")[:10] or "undated",
                    "created": p.get("createdAt") or "", "path": path, "title": p.get("title") or leaf})
    out.sort(key=lambda e: (e["date"], e["created"], e["path"]), reverse=True)
    return out


def render_index(entries: list[dict], title: str) -> str:
    lines = [f"# {title}", "", INDEX_MARKER, "",
             "Auto-index of Omi monologue pages, newest day first. Rebuilt from the wiki page list by "
             "omi-monologue-pipeline on every publish and at startup. "
             "Transcript review: <https://omi.k8s.inxaos.com/review>"]
    cur = None
    for e in entries:
        if e["date"] != cur:
            cur = e["date"]
            lines += ["", f"## {cur}", ""]
        lines.append(f"- [{e['title']}](/{e['path']})")
    if not entries:
        lines += ["", "_No monologues yet._"]
    return "\n".join(lines) + "\n"


class IndexKeeper:
    """Keeps <prefix> (e.g. journal/monologues) in sync with the wiki. request() marks it dirty;
    a background thread does a FULL rebuild from the wiki page list, retrying with backoff
    (30 s doubling to 15 min) until it succeeds. Never skips; failures are logged + counted."""

    def __init__(self, prefix: str, title: str, lister=None, writer=None,
                 base: float = 30, cap: float = 900, sleep=time.sleep):
        from . import providers as P
        self.prefix, self.title = prefix, title
        self.lister = lister or P.wiki_list_paths
        self.writer = writer or P.wiki_write_index
        self.base, self.cap, self.sleep = base, cap, sleep
        self._dirty = _th.Event()
        self._thread: _th.Thread | None = None

    def rebuild_once(self) -> dict:
        from . import metrics
        md = render_index(index_from_pages(self.lister(self.prefix), self.prefix), self.title)
        r = self.writer(self.prefix, md, self.title)
        metrics.set_gauge("omi_wiki_index_last_success_timestamp_seconds", time.time(), prefix=self.prefix)
        log(f"index {self.prefix}: {r.get('action')} (page {r.get('id')})")
        return r

    def run_until_ok(self) -> dict:
        from . import metrics
        delay = self.base
        while True:
            try:
                return self.rebuild_once()
            except Exception as e:
                metrics.inc("omi_wiki_index_failures_total", prefix=self.prefix)
                log(f"index {self.prefix} rebuild FAILED ({e}); retry in {delay:.0f}s")
                self.sleep(delay)
                delay = min(delay * 2, self.cap)

    def request(self) -> None:
        self._dirty.set()

    def start(self, rebuild_now: bool = True) -> "IndexKeeper":
        def loop():
            while True:
                self._dirty.wait()
                self._dirty.clear()   # publishes during a rebuild set it again -> another pass
                self.run_until_ok()
        self._thread = _th.Thread(target=loop, name=f"index-{self.prefix}", daemon=True)
        self._thread.start()
        if rebuild_now:
            self.request()            # startup full rebuild (self-heal)
        return self


def core_page(a: core.Analysis, sess: list[dict], date: str) -> str:
    raw = "\n\n".join(f"[{dt.datetime.fromtimestamp(c['start']).strftime('%H:%M:%S')}] {c.get('transcript', '').strip()}"
                      for c in sess if (c.get("transcript") or "").strip())
    span = (f"{dt.datetime.fromtimestamp(sess[0]['start']).strftime('%H:%M')}-"
            f"{dt.datetime.fromtimestamp(sess[-1]['end']).strftime('%H:%M')}")
    return core.render_page(a, raw, f"{date} {span}", f"{len(sess)} chunks from {sess[0]['device']}")
