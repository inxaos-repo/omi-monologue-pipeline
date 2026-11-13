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
            for c in sess:
                s = state.setdefault(c["name"], {})
                s.update(status="published", session=sid, wiki=r.get("wiki_url"))
                res = {"status": "published", "session": sid, "transcript": c.get("transcript", ""),
                       "audio": c.get("audio"), "wiki_url": r.get("wiki_url"), "title": r.get("title"),
                       "todoist": r.get("todoist", []), "date": _day(c["start"])}
                (self.archive / f"{Path(c['name']).stem}.result.json").write_text(json.dumps(res, indent=2))
                metrics.inc("omi_segments_published_total")
            tmp = p.with_suffix(".tmp")
            tmp.write_text(json.dumps(state, indent=2, sort_keys=True))
            tmp.replace(p)
            metrics.inc("omi_sessions_published_total")
            log(f"{sid}: {len(sess)} chunks -> {r.get('status')} {r.get('wiki_url') or ''}")
            changed = True
        return changed


# ---- wiki index page ------------------------------------------------------
def index_entries(archive: Path, prefix: str) -> list[dict]:
    """All published pages under prefix: sessions + legacy one-page-per-file results."""
    seen, out = set(), []
    archive = Path(archive)
    for f in sorted(archive.glob("*.session.json")) + sorted(archive.glob("*.result.json")):
        try:
            r = json.loads(f.read_text())
        except (OSError, ValueError):
            continue
        path = r.get("wiki_path")
        if not path or not path.startswith(prefix + "/") or path in seen or r.get("status") != "published":
            continue
        seen.add(path)
        date = r.get("date") or (_day(r["start"]) if r.get("start") else path.split("/")[-1][:10])
        t = dt.datetime.fromtimestamp(r["start"]).strftime("%H:%M") if r.get("start") else ""
        out.append({"date": date, "time": t, "path": path, "title": r.get("title") or path,
                    "chunks": len(r.get("chunks") or [1])})
    out.sort(key=lambda e: (e["date"], e["time"], e["path"]), reverse=True)
    return out


def render_index(entries: list[dict], title: str) -> str:
    lines = [f"# {title}", "", INDEX_MARKER, "",
             "*Generated by omi-monologue-pipeline on each publish. "
             "Transcript review: <https://omi.k8s.inxaos.com/review>*", ""]
    cur = None
    for e in entries:
        if e["date"] != cur:
            cur = e["date"]
            lines += ["", f"## {cur}", ""]
        when = f"{e['time']} " if e["time"] else ""
        n = f" ({e['chunks']} chunks)" if e["chunks"] > 1 else ""
        lines.append(f"- {when}[{e['title']}](/{e['path']}){n}")
    if not entries:
        lines.append("_No monologues yet._")
    return "\n".join(lines) + "\n"


def core_page(a: core.Analysis, sess: list[dict], date: str) -> str:
    raw = "\n\n".join(f"[{dt.datetime.fromtimestamp(c['start']).strftime('%H:%M:%S')}] {c.get('transcript', '').strip()}"
                      for c in sess if (c.get("transcript") or "").strip())
    span = (f"{dt.datetime.fromtimestamp(sess[0]['start']).strftime('%H:%M')}-"
            f"{dt.datetime.fromtimestamp(sess[-1]['end']).strftime('%H:%M')}")
    return core.render_page(a, raw, f"{date} {span}", f"{len(sess)} chunks from {sess[0]['device']}")
