"""omi-pipeline CLI: audio -> transcript -> LLM -> Wiki.js + Todoist."""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import shutil
import sys
import time
from pathlib import Path

from . import core, providers as P

AUDIO_EXT = {".m4a", ".mp3", ".wav", ".opus", ".ogg", ".webm", ".flac"}
WIKI_BASE = "https://wiki.k8s.inxaos.com"


def process(audio: Path, args) -> dict:
    archive = Path(args.archive_dir).expanduser()
    archive.mkdir(parents=True, exist_ok=True)
    date = dt.datetime.fromtimestamp(audio.stat().st_mtime).strftime("%Y-%m-%d")
    log = lambda m: print(f"[omi] {m}", file=sys.stderr)

    log(f"transcribing {audio.name} via {args.whisper_url} ({args.whisper_model})")
    raw = P.transcribe(audio, args.whisper_url, args.whisper_model)
    log(f"transcript: {len(raw)} chars")
    result = {"audio": str(audio), "transcript": raw, "date": date}

    guard = core.work_guard(raw)
    if guard.is_work:
        result.update(status="flagged-work", hits=guard.hits)
        log(f"WORK GUARD tripped ({guard.hits}); not publishing")
        return _finish(result, audio, archive, args)

    a = core.parse_analysis(P.get_llm(args.llm, args.llm_model).complete(P.PROMPT.format(t=raw)))
    if a.is_work:
        result.update(status="flagged-work", hits=["llm:is_work"])
        log("LLM classified as work; not publishing")
        return _finish(result, audio, archive, args)

    base = f"journal/monologues/{date}-{core.slugify(a.title)}"
    tags = ["monologue", "journal"] + a.tags + (["test"] if args.test else [])
    result.update(title=a.title, summary=a.summary, action_items=a.action_items, tags=tags)
    md = core.render_page(a, raw, date, audio.name)
    md_file = archive / f"{audio.stem}.md"
    md_file.write_text(md)

    if args.dry_run:
        result.update(status="dry-run", wiki_path=base, markdown=str(md_file))
        log(f"dry-run: would publish {base} and create {len(a.action_items)} tasks")
        return _finish(result, audio, archive, args)

    path = P.free_wiki_path(base)
    P.wiki_publish(path, md_file, a.title, tags)
    url = f"{WIKI_BASE}/{path}"
    result.update(wiki_path=path, wiki_url=url)
    log(f"wiki: {url}")

    inbox = P.todoist_inbox_id()
    tasks = []
    for item in a.action_items:
        t = P.todoist_create(item, f"From monologue: [{a.title}]({url})", inbox, args.label)
        tasks.append({"id": t["id"], "content": item})
    result.update(status="published", todoist=tasks)
    log(f"todoist: {len(tasks)} tasks")
    return _finish(result, audio, archive, args)


def _finish(result: dict, audio: Path, archive: Path, args) -> dict:
    # Keep raw audio local; never delete (retention policy undecided).
    if args.keep_in_archive and audio.parent.resolve() != archive.resolve():
        dest = archive / audio.name
        if not dest.exists():
            shutil.copy2(audio, dest)
    (archive / f"{audio.stem}.result.json").write_text(json.dumps(result, indent=2))
    return result


def watch(folder: Path, args) -> None:
    """Stub watched-folder mode: poll for new audio files, process each once."""
    done_marker = lambda f: f.with_suffix(f.suffix + ".done")
    print(f"[omi] watching {folder} every {args.interval}s", file=sys.stderr)
    while True:
        for f in sorted(folder.iterdir()):
            if f.suffix.lower() in AUDIO_EXT and not done_marker(f).exists():
                if time.time() - f.stat().st_mtime < 10:  # still being written
                    continue
                try:
                    r = process(f, args)
                    done_marker(f).write_text(r.get("status", ""))
                except Exception as e:  # keep watching
                    print(f"[omi] {f.name} failed: {e}", file=sys.stderr)
        if args.once:
            return
        time.sleep(args.interval)


def main(argv=None) -> int:
    P.load_env()
    ap = argparse.ArgumentParser(prog="omi-pipeline", description=__doc__)
    ap.add_argument("file", nargs="?", help="audio file (m4a/mp3/wav/opus)")
    ap.add_argument("--watch", metavar="DIR", help="watch a drop folder instead")
    ap.add_argument("--once", action="store_true", help="with --watch: single scan")
    ap.add_argument("--interval", type=int, default=30)
    ap.add_argument("--dry-run", action="store_true", help="transcribe+LLM only; write nothing external")
    ap.add_argument("--test", action="store_true", help="tag wiki page 'test'")
    ap.add_argument("--llm", default=os.environ.get("OMI_LLM", "anthropic"), choices=list(P.PROVIDERS))
    ap.add_argument("--llm-model", default=None)
    ap.add_argument("--whisper-url", default=os.environ.get(
        "OMI_WHISPER_URL", "http://192.168.2.25:8000/v1/audio/transcriptions"))
    ap.add_argument("--whisper-model", default=os.environ.get("OMI_WHISPER_MODEL", "Systran/faster-whisper-large-v3"))
    ap.add_argument("--archive-dir", default=os.environ.get("OMI_ARCHIVE_DIR", "~/omi-monologues"))
    ap.add_argument("--no-archive-copy", dest="keep_in_archive", action="store_false")
    ap.add_argument("--label", default="monologue")
    args = ap.parse_args(argv)
    if args.watch:
        watch(Path(args.watch).expanduser(), args)
        return 0
    if not args.file:
        ap.error("file or --watch required")
    r = process(Path(args.file).expanduser(), args)
    print(json.dumps({k: v for k, v in r.items() if k != "transcript"}, indent=2))
    return 0 if r["status"] in ("published", "dry-run") else 3


if __name__ == "__main__":
    sys.exit(main())
