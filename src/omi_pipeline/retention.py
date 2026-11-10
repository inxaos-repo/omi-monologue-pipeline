"""Raw-audio retention (#4, boss decision 2026-11-10: keep 7 days, then delete).

Only audio that was **successfully published** (transcribed + Wiki.js + Todoist) is ever
deleted, and only once it is older than `days` (file mtime = recording time):
- intake dir: audio whose `.omi-state.json` entry has status `published`;
- archive dir: audio whose `<stem>.result.json` has status `published`.
Never touched: failed/deferred/dry-run/unknown files, `.part` files, `.spool/` (a leftover
spool means conversion never finished -> it's recovery data, not processed audio),
and the `.md` / `.result.json` outputs.
`days <= 0` disables retention. Configure with OMI_RAW_RETENTION_DAYS (default 7).
"""
from __future__ import annotations

import json
import sys
import time
from collections.abc import Callable
from pathlib import Path

from .watcher import AUDIO_EXT, STATE_NAME

DEFAULT_DAYS = 7.0
DONE = "published"


def log(m: str) -> None:
    print(f"[omi-retention] {m}", file=sys.stderr, flush=True)


def _audio(d: Path) -> list[Path]:
    if not d.is_dir():
        return []
    return sorted(f for f in d.iterdir() if f.is_file() and not f.name.startswith(".")
                  and f.suffix.lower() in AUDIO_EXT)


def _published_in_archive(f: Path) -> bool:
    r = f.with_name(f"{f.stem}.result.json")
    try:
        return json.loads(r.read_text()).get("status") == DONE
    except (OSError, ValueError, AttributeError):
        return False


def purge(intake: Path | None, archive: Path | None, days: float,
          now: Callable[[], float] = time.time) -> list[Path]:
    """Delete published raw audio older than `days`. Returns deleted paths."""
    if days <= 0:
        return []
    cutoff = now() - days * 86400
    deleted: list[Path] = []

    def rm(f: Path, why: str) -> bool:
        try:
            if f.stat().st_mtime >= cutoff:
                return False
            f.unlink()
        except OSError as e:
            log(f"could not delete {f}: {e}")
            return False
        log(f"deleted {f} ({why}, older than {days:g}d)")
        deleted.append(f)
        return True

    if intake is not None:
        sp = Path(intake) / STATE_NAME
        state = json.loads(sp.read_text()) if sp.exists() else {}
        changed = False
        for f in _audio(Path(intake)):
            s = state.get(f.name)
            if s and s.get("status") == DONE and rm(f, "intake, published"):
                s["purged"] = now()
                changed = True
        if changed:
            tmp = sp.with_suffix(".tmp")
            tmp.write_text(json.dumps(state, indent=2, sort_keys=True))
            tmp.replace(sp)
    if archive is not None and (intake is None or Path(archive).resolve() != Path(intake).resolve()):
        for f in _audio(Path(archive)):
            if _published_in_archive(f):
                rm(f, "archive, published")
    return deleted


class Periodic:
    """Call purge() at most every `every` seconds; used as the watcher's tick hook."""

    def __init__(self, intake, archive, days: float, every: float = 3600,
                 now: Callable[[], float] = time.time):
        self.intake, self.archive, self.days, self.every, self.now = intake, archive, days, every, now
        self.last = float("-inf")

    def __call__(self) -> list[Path]:
        if self.days <= 0 or self.now() - self.last < self.every:
            return []
        self.last = self.now()
        try:
            return purge(self.intake, self.archive, self.days, self.now)
        except Exception as e:  # never kill the watcher
            log(f"retention pass failed: {e}")
            return []
