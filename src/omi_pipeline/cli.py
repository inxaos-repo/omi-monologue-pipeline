"""omi-pipeline CLI: audio -> transcript -> LLM -> Wiki.js + Todoist."""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import shutil
import sys
from pathlib import Path

from . import core
from . import providers as P

AUDIO_EXT = {".m4a", ".mp3", ".wav", ".opus", ".ogg", ".webm", ".flac"}
WIKI_BASE = "https://wiki.k8s.inxaos.com"


def is_test(audio: Path, args) -> bool:
    """Test recordings: --test, or files from a device id starting with 'test' (omi-test*-...)."""
    return bool(getattr(args, "test", False)) or audio.name.startswith("omi-test")


def wiki_prefix(test: bool) -> str:
    if test:
        return os.environ.get("OMI_WIKI_TEST_PREFIX", "test/omi-monologues")
    return os.environ.get("OMI_WIKI_PREFIX", "journal/monologues")


def process(audio: Path, args) -> dict:
    archive = Path(args.archive_dir).expanduser()
    archive.mkdir(parents=True, exist_ok=True)
    date = dt.datetime.fromtimestamp(audio.stat().st_mtime).strftime("%Y-%m-%d")
    log = lambda m: print(f"[omi] {m}", file=sys.stderr)

    if args.transcriber == "local":
        log(f"transcribing {audio.name} locally on CPU ({args.local_model})")
        raw = P.transcribe_local(audio, args.local_model)
    else:
        log(f"transcribing {audio.name} via {args.whisper_url} ({args.whisper_model})")
        try:
            raw = P.transcribe(audio, args.whisper_url, args.whisper_model)
        except Exception as e:
            if not args.fallback_local:
                raise
            log(f"remote whisper failed ({e}); falling back to local CPU ({args.local_model})")
            raw = P.transcribe_local(audio, args.local_model)
    log(f"transcript: {len(raw)} chars")
    result = {"audio": str(audio), "transcript": raw, "date": date}

    a = core.parse_analysis(P.get_llm(args.llm, args.llm_model).complete(P.PROMPT.format(t=raw)))

    test = is_test(audio, args)
    if test:
        a.title = f"TEST {a.title}"
    base = f"{wiki_prefix(test)}/{date}-{core.slugify(a.title)}"
    tags = ["monologue", "journal"] + a.tags + (["test"] if test else [])
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
        t = P.todoist_create(f"TEST {item}" if test else item, f"From monologue: [{a.title}]({url})",
                             inbox, "omi-test" if test else args.label)
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
    """Watch an intake folder and run process() on each new, settled audio file."""
    from .watcher import Watcher
    folder.mkdir(parents=True, exist_ok=True)
    Watcher(folder, lambda f: process(f, args), settle=args.settle, max_attempts=args.max_attempts,
            defer=args.defer).run(interval=args.interval, once=args.once)


def serve(folder: Path, args) -> None:
    """Container entrypoint: HTTP ingest for ESP32 bridges + intake watcher in one process."""
    from .ingest import serve_background
    folder.mkdir(parents=True, exist_ok=True)
    token = os.environ.get("OMI_INGEST_TOKEN") or None
    srv = serve_background(folder, args.host, args.port, token)
    try:
        watch(folder, args)
    finally:
        srv.shutdown()


def main(argv=None) -> int:
    P.load_env()
    ap = argparse.ArgumentParser(prog="omi-pipeline", description=__doc__)
    ap.add_argument("file", nargs="?", help="audio file (m4a/mp3/wav/opus)")
    ap.add_argument("--watch", metavar="DIR", help="watch a drop folder instead")
    ap.add_argument("--serve", metavar="DIR", nargs="?", const=os.environ.get("OMI_INTAKE_DIR", "/data/intake"),
                    help="run HTTP ingest + watcher on DIR (container mode; default $OMI_INTAKE_DIR)")
    ap.add_argument("--host", default=os.environ.get("OMI_INGEST_HOST", "0.0.0.0"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("OMI_INGEST_PORT", "8080")))
    ap.add_argument("--defer", type=float, default=float(os.environ.get("OMI_DEFER_SECONDS", "120")),
                    help="seconds to wait before retrying after a transient (network) failure")
    ap.add_argument("--once", action="store_true", help="with --watch: single scan")
    ap.add_argument("--interval", type=int, default=30, help="with --watch: max seconds between scans")
    ap.add_argument("--settle", type=float, default=10, help="with --watch: min file age before processing")
    ap.add_argument("--max-attempts", type=int, default=3, help="with --watch: retries per failing file")
    ap.add_argument("--dry-run", action="store_true", help="transcribe+LLM only; write nothing external")
    ap.add_argument("--test", action="store_true", help="tag wiki page 'test'")
    ap.add_argument("--llm", default=os.environ.get("OMI_LLM", "anthropic"), choices=list(P.PROVIDERS))
    ap.add_argument("--llm-model", default=None)
    ap.add_argument("--whisper-url", default=os.environ.get(
        "OMI_WHISPER_URL", "http://192.168.2.25:8001/v1/audio/transcriptions"))
    ap.add_argument("--whisper-model", default=os.environ.get("OMI_WHISPER_MODEL", "deepdml/faster-whisper-large-v3-turbo-ct2"))
    ap.add_argument("--transcriber", default=os.environ.get("OMI_TRANSCRIBER", "remote"), choices=["remote", "local"])
    ap.add_argument("--fallback-local", action="store_true", help="use local CPU whisper if remote fails")
    ap.add_argument("--local-model", default=os.environ.get("OMI_LOCAL_WHISPER_MODEL", "small.en"))
    ap.add_argument("--archive-dir", default=os.environ.get("OMI_ARCHIVE_DIR", "~/omi-monologues"))
    ap.add_argument("--no-archive-copy", dest="keep_in_archive", action="store_false")
    ap.add_argument("--label", default="monologue")
    args = ap.parse_args(argv)
    if args.serve:
        serve(Path(args.serve).expanduser(), args)
        return 0
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
