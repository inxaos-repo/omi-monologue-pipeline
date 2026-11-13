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

    raw = _transcribe(audio, args)
    result = {"audio": str(audio), "transcript": raw, "date": date}
    return _analyse_publish(audio, raw, result, date, archive, args)


def _transcribe(audio: Path, args) -> str:
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
    return raw


def _analyse_publish(audio: Path, raw: str, result: dict, date: str, archive: Path, args) -> dict:
    log = lambda m: print(f"[omi] {m}", file=sys.stderr)

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

    tasks = _todoist(a, url, test, args)
    result.update(status="published", todoist=tasks)
    log(f"todoist: {len(tasks)} tasks")
    out = _finish(result, audio, archive, args)
    update_index(archive, test)
    return out


def _todoist(a, url: str, test: bool, args) -> list[dict]:
    if not a.action_items:
        return []
    inbox = P.todoist_inbox_id()
    tasks = []
    for item in a.action_items:
        t = P.todoist_create(f"TEST {item}" if test else item, f"From monologue: [{a.title}]({url})",
                             inbox, "omi-test" if test else args.label)
        tasks.append({"id": t["id"], "content": item})
    return tasks


_KEEPERS: dict[str, object] = {}


def update_index(archive: Path | None, test: bool) -> None:
    """Rebuild the wiki index page (<prefix>) after any monologue page create/update.
    In --serve/--watch the background IndexKeeper does it (retry with backoff, never skipped);
    in one-shot CLI mode it runs synchronously with the same retries."""
    from . import sessions as S
    if os.environ.get("OMI_WIKI_INDEX", "1") == "0":
        return
    prefix = wiki_prefix(test)
    k = _KEEPERS.get(prefix)
    if k is not None:
        k.request()
        return
    S.IndexKeeper(prefix, "Test monologues" if test else "Monologues").run_until_ok()


def start_index_keepers() -> None:
    from . import sessions as S
    if os.environ.get("OMI_WIKI_INDEX", "1") == "0":
        return
    prefix = wiki_prefix(False)
    _KEEPERS[prefix] = S.IndexKeeper(prefix, "Monologues").start()


def transcribe_chunk(audio: Path, args) -> dict:
    """Session mode stage 1: transcribe one chunk, keep it for the session closer."""
    from .review import duration
    from .sessions import chunk_meta
    archive = Path(args.archive_dir).expanduser()
    archive.mkdir(parents=True, exist_ok=True)
    raw = _transcribe(audio, args)
    meta = chunk_meta(audio, duration(audio))
    meta.update(audio=str(audio), transcript=raw)
    if args.keep_in_archive and audio.parent.resolve() != archive.resolve():
        dest = archive / audio.name
        if not dest.exists():
            shutil.copy2(audio, dest)
    (archive / f"{audio.stem}.chunk.json").write_text(json.dumps(meta, indent=2))
    return {"status": "transcribed"}


def publish_session(sess: list[dict], sid: str, args) -> dict:
    """Session mode stage 2: one LLM pass, one wiki page, one Todoist pass for the whole session."""
    from . import sessions as S
    archive = Path(args.archive_dir).expanduser()
    log = lambda m: print(f"[omi] {m}", file=sys.stderr)
    date = dt.datetime.fromtimestamp(sess[0]["start"]).strftime("%Y-%m-%d")
    joined = "\n".join((c.get("transcript") or "").strip() for c in sess).strip()
    test = bool(getattr(args, "test", False)) or sess[0]["name"].startswith("omi-test")
    if len(joined) < int(os.environ.get("OMI_SESSION_MIN_CHARS", "20")):
        log(f"{sid}: only {len(joined)} chars of speech; not publishing")
        return {"status": "published", "note": "no speech", "date": date, "transcript": joined}
    a = core.parse_analysis(P.get_llm(args.llm, args.llm_model).complete(P.PROMPT.format(t=joined)))
    if test:
        a.title = f"TEST {a.title}"
    base = f"{wiki_prefix(test)}/{date}-{core.slugify(a.title)}"
    tags = ["monologue", "journal"] + a.tags + (["test"] if test else [])
    md_file = archive / f"{sid}.md"
    md_file.write_text(S.core_page(a, sess, date))
    r = {"date": date, "title": a.title, "summary": a.summary, "action_items": a.action_items,
         "tags": tags, "transcript": joined}
    if args.dry_run:
        r.update(status="dry-run", wiki_path=base)
        return r
    path = P.free_wiki_path(base)
    P.wiki_publish(path, md_file, a.title, tags)
    url = f"{WIKI_BASE}/{path}"
    r.update(wiki_path=path, wiki_url=url, status="published")
    r["todoist"] = _todoist(a, url, test, args)
    # session.json is written by the closer after we return; index needs it, so write it now too
    (archive / f"{sid}.session.json").write_text(json.dumps({**r, "start": sess[0]["start"],
                                                            "chunks": [c["name"] for c in sess]}, indent=2))
    update_index(archive, test)
    return r


def _finish(result: dict, audio: Path, archive: Path, args) -> dict:
    # Keep raw audio local; deleted after OMI_RAW_RETENTION_DAYS once published (retention.py).
    if args.keep_in_archive and audio.parent.resolve() != archive.resolve():
        dest = archive / audio.name
        if not dest.exists():
            shutil.copy2(audio, dest)
    (archive / f"{audio.stem}.result.json").write_text(json.dumps(result, indent=2))
    return result


def watch(folder: Path, args) -> None:
    """Watch an intake folder and run process() on each new, settled audio file."""
    from .retention import Periodic
    from .watcher import Watcher
    folder.mkdir(parents=True, exist_ok=True)
    archive = Path(args.archive_dir).expanduser()
    archive.mkdir(parents=True, exist_ok=True)
    if not args.dry_run:
        start_index_keepers()   # full rebuild at startup, then on every publish
    retention = Periodic(folder, archive, args.retention_days)
    handler = lambda f: process(f, args)
    tick = retention
    if args.session_pause > 0:
        from .sessions import SessionCloser
        closer = SessionCloser(folder, archive, lambda sess, sid: publish_session(sess, sid, args),
                               pause=args.session_pause, retry=args.defer)
        handler = lambda f: transcribe_chunk(f, args)
        tick = lambda: bool(closer()) | bool(retention())
        print(f"[omi-session] session mode: pause {args.session_pause:g}s closes a session", file=sys.stderr)
    print(f"[omi-retention] raw audio retention: "
          f"{f'{args.retention_days:g} days' if args.retention_days > 0 else 'disabled'}", file=sys.stderr)
    Watcher(folder, handler, settle=args.settle, max_attempts=args.max_attempts,
            defer=args.defer, tick=tick).run(interval=args.interval, once=args.once)


def serve(folder: Path, args) -> None:
    """Container entrypoint: HTTP ingest for ESP32 bridges + intake watcher in one process."""
    from .ingest import serve_background
    folder.mkdir(parents=True, exist_ok=True)
    token = os.environ.get("OMI_INGEST_TOKEN") or None
    srv = serve_background(folder, args.host, args.port, token, args.whisper_url)
    review = None
    if args.review_port > 0:
        from .review import serve_review_background
        review = serve_review_background(folder, Path(args.archive_dir).expanduser(), args.host, args.review_port)
    try:
        watch(folder, args)
    finally:
        srv.shutdown()
        if review:
            review.shutdown()


def main(argv=None) -> int:
    P.load_env()
    ap = argparse.ArgumentParser(prog="omi-pipeline", description=__doc__)
    ap.add_argument("file", nargs="?", help="audio file (m4a/mp3/wav/opus)")
    ap.add_argument("--watch", metavar="DIR", help="watch a drop folder instead")
    ap.add_argument("--serve", metavar="DIR", nargs="?", const=os.environ.get("OMI_INTAKE_DIR", "/data/intake"),
                    help="run HTTP ingest + watcher on DIR (container mode; default $OMI_INTAKE_DIR)")
    ap.add_argument("--host", default=os.environ.get("OMI_INGEST_HOST", "0.0.0.0"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("OMI_INGEST_PORT", "8080")))
    ap.add_argument("--review-port", type=int, default=int(os.environ.get("OMI_REVIEW_PORT", "0")),
                    help="with --serve: read-only transcript review UI port (0=off; needs SSO header)")
    ap.add_argument("--session-pause", type=float, default=float(os.environ.get("OMI_SESSION_PAUSE", "300")),
                    help="with --watch/--serve: aggregate chunks per device into one page per session; "
                         "a session closes after this many seconds without audio or at day end (0 = one page per file)")
    ap.add_argument("--defer", type=float, default=float(os.environ.get("OMI_DEFER_SECONDS", "120")),
                    help="seconds to wait before retrying after a transient (network) failure")
    ap.add_argument("--once", action="store_true", help="with --watch: single scan")
    ap.add_argument("--interval", type=int, default=30, help="with --watch: max seconds between scans")
    ap.add_argument("--settle", type=float, default=10, help="with --watch: min file age before processing")
    ap.add_argument("--max-attempts", type=int, default=3, help="with --watch: retries per failing file")
    ap.add_argument("--dry-run", action="store_true", help="transcribe+LLM only; write nothing external")
    ap.add_argument("--test", action="store_true", help="tag wiki page 'test'")
    ap.add_argument("--llm", default=os.environ.get("OMI_LLM", "ollama"), choices=list(P.PROVIDERS))
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
    ap.add_argument("--retention-days", type=float, default=float(os.environ.get("OMI_RAW_RETENTION_DAYS", "7")),
                    help="with --watch/--serve: delete published raw audio older than N days (0 = keep forever)")
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
