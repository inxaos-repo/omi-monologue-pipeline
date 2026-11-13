# CLAUDE.md — agent guide for omi-monologue-pipeline

Plan: [plans/omi-custom-firmware](https://wiki.k8s.inxaos.com/plans/omi-custom-firmware) (this repo is step 3).

**No firmware here.** The ESP32-S3 bridge firmware lives only in `inxaos-repo/omi-bridge-firmware`
(a duplicate `firmware/esp32s3-bridge` was removed 2026-11-12). `docs/omi-ble-protocol.md` stays here:
it is the spec for both the receiver and that firmware; when it changes, open a matching PR there.

## Purpose
Turns Damon's **solo monologues** (recorded on the Omi pendant or phone) into:
1. a transcript (Whisper),
2. an LLM pass → JSON `{title, cleaned, summary, action_items, tags}`,
3. a Wiki.js page at `journal/monologues/<YYYY-MM-DD>-<slug>` (date = audio file mtime),
4. one Todoist task per action item in the **Inbox**, labelled `monologue`, linking back to the page.

## Layout / entry points
- `src/omi_pipeline/cli.py` — `omi-pipeline` console script (`main()`), `process()` (one file), `watch()` (drop-folder mode, `_finish()` archives).
- `src/omi_pipeline/core.py` — pure logic: `slugify`, `extract_json`, `parse_analysis`, `render_page`. Unit-tested.
- `src/omi_pipeline/providers.py` — all I/O: `load_env`, Whisper (`transcribe`, `transcribe_local`), LLMs (`Anthropic`, `Ollama`, `PROVIDERS`, `PROMPT`), Wiki.js GraphQL (`wiki_exists`, `free_wiki_path`, `wiki_publish`), Todoist (`todoist_inbox_id`, `todoist_create`).
- `src/omi_pipeline/omi_protocol.py` — pure Omi BLE protocol (UUIDs, audio fragment reassembly, fw 3.0.19 storage cmds/records, Ogg Opus muxer). Spec: `docs/omi-ble-protocol.md`.
- `src/omi_pipeline/receiver.py` — `omi-receiver` (bleak, `--extra ble`): `list|sync|live` into the intake folder. **No delete by default.** Detects fw via DIS 0x2A26: 3.0.19 file protocol (auto-deletes after full read → stops `--keep-tail` early); 3.0.21+ ring protocol (reading frees data → needs `--consume`). Pendant is on 3.0.21. See `docs/receiver.md`.
- `src/omi_pipeline/watcher.py` — intake watcher used by `--watch` (`tick` hook runs retention).
- `src/omi_pipeline/retention.py` — 7-day raw-audio retention (`purge`, `Periodic` hourly).
- `src/omi_pipeline/ingest.py` — stdlib HTTP ingest server.
- `tests/` — pytest for pure code (core, protocol, watcher, ingest); no network/BLE.

Key flags:
- `--dry-run` — transcribe + LLM only; writes the `.md`/`.result.json` locally but **nothing to Wiki.js or Todoist**. (Still sends audio to LAN Whisper and the transcript to the LLM.)
- `--fallback-local` — if remote Whisper fails, transcribe on local CPU (`faster-whisper`, needs `uv sync --extra local`; model `--local-model`, default `small.en`). `--transcriber local` skips remote entirely.
- `--watch DIR [--once] [--interval N] [--settle S] [--max-attempts N]` — intake watcher (`watcher.py`, #3): settled-file detection, `.part` ignored, state in `<dir>/.omi-state.json`, retry/backoff, inotify if `--extra watch`. transient (network/5xx) errors are deferred and retried forever without using attempts.
- `--serve [DIR]` — container mode: HTTP ingest (`ingest.py`, ESP32 bridges POST Opus frames/segments) + watcher. Dockerfile + `.github/workflows/image.yml` → `ghcr.io/inxaos-repo/omi-monologue-pipeline`. See `docs/ingest.md`.
- `--test` — adds wiki tag `test`. `--llm anthropic|ollama`, `--llm-model`, `--label` (default `monologue`), `--archive-dir`, `--no-archive-copy`.

Exit code 0 on `published`/`dry-run`, 3 otherwise. Final JSON (minus transcript) printed to stdout; logs to stderr.

## Config / env
No secrets in the repo, ever. `load_env()` reads `~/.openclaw/.env` and `~/.openclaw/workspace/.env` (setdefault; real env wins).
- Secrets: `ANTHROPIC_API_KEY`, `WIKIJS_TOKEN`, `TODOIST_TOKEN`; `WIKIJS_URL` (default `https://wiki.k8s.inxaos.com`).
- Knobs: `OMI_LLM`, `OMI_ANTHROPIC_MODEL` (default `claude-sonnet-4-5`), `OMI_OLLAMA_MODEL` (`qwen2.5:14b`), `OMI_OLLAMA_URL` (`http://192.168.2.25:11434`), `OMI_WHISPER_URL`, `OMI_WHISPER_MODEL` (`deepdml/faster-whisper-large-v3-turbo-ct2`), `OMI_TRANSCRIBER`, `OMI_LOCAL_WHISPER_MODEL`, `OMI_ARCHIVE_DIR` (`~/omi-monologues`), `OMI_RAW_RETENTION_DAYS` (`7`; 0 = keep forever).

## External dependencies
- **Whisper on Ix (192.168.2.25)** — default URL is speaches at `:8001/v1/audio/transcriptions`, model `deepdml/faster-whisper-large-v3-turbo-ct2` (issue #1). `Systran/faster-whisper-large-v3` is also installed on speaches. The old `:8000` faster-whisper-server is deprecated/broken. Use `--fallback-local` if Ix is down.
- **LLM = local Ix Ollama** (boss, 2026-11-10, #4): default `OMI_LLM=ollama`, model `qwen2.5:14b` (tested vs gemma3:27b: better action items, ~45s vs ~166s). Ix's 3090 is shared with ComfyUI — keep LLM calls sequential (one at a time; the watcher is single-threaded, don't parallelise). `--llm anthropic` stays as opt-in fallback.
- **Wiki.js** — publishing shells out to `~/.openclaw/workspace/skills/wikijs-push/scripts/wiki_push.py`. **No-clobber:** `free_wiki_path()` checks existence and appends `-2`, `-3`, … Never overwrite or delete an existing page; editing existing pages must follow the `wikijs-page-update` skill (fetch → edit → full-field update → verify).
- **Todoist** — API v1; tasks go to the Inbox project with the `monologue` label.

## Test / lint
```
uv sync
uv run pytest -q
uv run omi-pipeline --help      # CI runs these three (.github/workflows/ci.yml)
uvx ruff check .                # optional; no linter is enforced in CI yet
```
Keep network code in `providers.py` and pure logic in `core.py` so it stays testable.

## Rules
- **Branch + PR only** (`bob/*`). Never push to `main`.
- Run `--dry-run` before any real run on new audio/changes.
- Test runs that publish: use `--test` (wiki tag `test`) and **complete** the resulting Todoist tasks afterwards.
- **Raw audio retention = 7 days** (boss, 2026-11-10, #4): only `retention.py` deletes audio, only once it is published and older than `OMI_RAW_RETENTION_DAYS` (default 7). Never delete unprocessed/failed audio, `.spool/`, or outputs; don't add other delete paths.
- The work-content guard was **intentionally removed** on 2026-11-09 by the boss. Do not re-add it.
- Update the Wiki plan page after meaningful changes.

## Deployment (k8s)
- Live as `omi-ingest` in inxaos-flux `apps/omi-ingest/` (app-template, ns `omi-ingest`), `https://omi.k8s.inxaos.com`.
- Image pinned to a full `sha-<40hex>` tag; bump via a flux PR (latest: #1995 -> `sha-a20e140…`, 2026-11-12).
- Secrets in SOPS `apps/omi-ingest/secret.sops.yaml` (`OMI_INGEST_TOKEN`, `WIKIJS_TOKEN`, `TODOIST_TOKEN`); no `ANTHROPIC_API_KEY`, LLM is Ix Ollama.
- Data PVC `omi-ingest-data` (nfs-client, 20Gi) at /data; raw audio retained 7 days (`OMI_RAW_RETENTION_DAYS`).
