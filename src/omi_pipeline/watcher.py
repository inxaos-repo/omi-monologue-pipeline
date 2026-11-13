"""Intake-folder watcher (replaces the #3 polling stub).

Semantics:
- Picks up audio files (AUDIO_EXT) in the intake dir; ignores `*.part` and dotfiles.
- A file is "ready" once its size+mtime are unchanged across two scans and it is
  older than `settle` seconds (receiver writes .part then renames, phone uploads may not).
- State lives in `<intake>/.omi-state.json`: {name: {status, attempts, size, mtime, ...}}.
  Successful files are never reprocessed; failures retry with backoff up to `max_attempts`.
- Uses inotify via `watchdog` when installed (uv sync --extra watch) to wake immediately;
  otherwise polls every `interval` seconds. Either way the scan logic is identical.
- Never deletes or moves audio itself; the optional `tick` hook (retention.Periodic,
  7-day raw-audio retention, #4) runs after each scan and may delete published audio.
"""
from __future__ import annotations

import json
import signal
import socket
import urllib.error
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

AUDIO_EXT = {".m4a", ".mp3", ".wav", ".opus", ".ogg", ".webm", ".flac"}
STATE_NAME = ".omi-state.json"


def is_transient(e: BaseException) -> bool:
    """Network-ish failures (speaches on Ix down, DNS, timeouts, 5xx/429) -> retry later."""
    if isinstance(e, urllib.error.HTTPError):
        return e.code >= 500 or e.code in (408, 429)
    if isinstance(e, (urllib.error.URLError, ConnectionError, TimeoutError, socket.timeout, socket.gaierror)):
        return True
    cause = e.__cause__ or e.__context__
    return cause is not None and cause is not e and is_transient(cause)


def _count(name: str) -> None:
    from . import metrics
    metrics.inc(name)


def log(m: str) -> None:
    print(f"[omi-watch] {m}", file=sys.stderr, flush=True)


@dataclass
class Watcher:
    folder: Path
    handler: Callable[[Path], dict]
    settle: float = 10.0
    max_attempts: int = 3
    backoff: float = 300.0
    defer: float = 120.0   # wait after a transient failure; does not use up attempts
    now: Callable[[], float] = time.time
    tick: Callable[[], object] | None = None   # called after each scan (retention)
    state: dict = field(default_factory=dict)
    _seen: dict = field(default_factory=dict)

    def __post_init__(self):
        self.folder = Path(self.folder)
        p = self.folder / STATE_NAME
        if p.exists():
            self.state = json.loads(p.read_text())

    def save(self) -> None:
        p = self.folder / STATE_NAME
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state, indent=2, sort_keys=True))
        tmp.replace(p)

    def candidates(self) -> list[Path]:
        return sorted(f for f in self.folder.iterdir()
                      if f.is_file() and not f.name.startswith(".")
                      and f.suffix.lower() in AUDIO_EXT)

    def ready(self, f: Path) -> bool:
        st = f.stat()
        sig = (st.st_size, st.st_mtime)
        prev = self._seen.get(f.name)
        self._seen[f.name] = sig
        if st.st_size == 0 or prev != sig:
            return False
        return self.now() - st.st_mtime >= self.settle

    def due(self, name: str) -> bool:
        s = self.state.get(name)
        if not s:
            return True
        if s["status"] in ("published", "dry-run", "transcribed"):
            return False
        if s["status"] == "deferred":
            return self.now() - s.get("last", 0) >= self.defer
        if s.get("attempts", 0) >= self.max_attempts:
            return False
        return self.now() - s.get("last", 0) >= self.backoff * s.get("attempts", 1)

    def scan(self) -> list[str]:
        processed = []
        for f in self.candidates():
            if not self.due(f.name) or not self.ready(f):
                continue
            s = self.state.get(f.name, {"attempts": 0})
            s.update(last=self.now(), size=f.stat().st_size)
            try:
                r = self.handler(f)
                s.update(status=r.get("status", "unknown"), wiki=r.get("wiki_url") or r.get("wiki_path"))
                s.pop("error", None)
                log(f"{f.name}: {s['status']}")
                _count("omi_segments_transcribed_total")
                if s["status"] == "published":
                    _count("omi_segments_published_total")
            except Exception as e:  # keep watching
                if is_transient(e):
                    s.update(status="deferred", error=str(e)[:500], deferrals=s.get("deferrals", 0) + 1)
                    _count("omi_segments_deferred_total")
                    log(f"{f.name} deferred (transient: {e}); retry in {self.defer:.0f}s")
                else:
                    s.update(status="failed", error=str(e)[:500], attempts=s.get("attempts", 0) + 1)
                    _count("omi_segments_failed_total")
                    log(f"{f.name} failed (attempt {s['attempts']}/{self.max_attempts}): {e}")
            self.state[f.name] = s
            self.save()
            processed.append(f.name)
        return processed

    def run(self, interval: float = 30, once: bool = False) -> None:
        wake = threading.Event()
        stop = threading.Event()
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                signal.signal(sig, lambda *_: (stop.set(), wake.set()))
            except ValueError:  # not main thread
                pass
        observer = _inotify(self.folder, wake) if not once else None
        log(f"watching {self.folder} ({'inotify' if observer else 'polling'}, interval {interval}s)")
        try:
            while not stop.is_set():
                self.scan()
                self._tick()
                if once:
                    # second pass so stable files get processed in one invocation
                    time.sleep(min(1.0, self.settle))
                    self.scan()
                    return
                wake.wait(interval if not self._pending() else min(interval, 5))
                wake.clear()
        finally:
            if observer:
                observer.stop()

    def _tick(self) -> None:
        if self.tick is None:
            return
        if self.tick():  # retention may have updated the state file
            p = self.folder / STATE_NAME
            if p.exists():
                self.state = json.loads(p.read_text())

    def _pending(self) -> bool:
        return any(f.name not in self.state for f in self.candidates())

    def stats(self) -> dict:
        out: dict = {}
        for s in self.state.values():
            out[s.get("status", "?")] = out.get(s.get("status", "?"), 0) + 1
        return out


def _inotify(folder: Path, wake: threading.Event):
    try:
        from watchdog.events import FileSystemEventHandler
        from watchdog.observers import Observer
    except ImportError:
        return None

    class H(FileSystemEventHandler):
        def on_any_event(self, event):
            wake.set()

    obs = Observer()
    obs.schedule(H(), str(folder), recursive=False)
    obs.start()
    return obs
