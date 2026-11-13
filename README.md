# omi-monologue-pipeline

Agents/contributors: read [CLAUDE.md](CLAUDE.md) first.

Step 3 of [plans/omi-custom-firmware](<wiki-url>/plans/omi-custom-firmware).

> **Bridge firmware lives in [inxaos-repo/omi-bridge-firmware](https://github.com/inxaos-repo/omi-bridge-firmware)**
> (ESP32-S3 BLE→WiFi bridge, fw 3.0.24 custody). This repo keeps the receiver/ingest and the shared
> protocol spec, [`docs/omi-ble-protocol.md`](docs/omi-ble-protocol.md).
Solo voice monologue → transcript → cleaned text, summary, action items → Wiki.js page + Todoist tasks.

```
audio (m4a/mp3/wav/opus)
  → speaches (the GPU host :8001, large-v3-turbo)
  → LLM (ollama [default, the GPU host qwen2.5:14b] | anthropic) → JSON {title, cleaned, summary, action_items, tags}
  → Wiki.js journal/monologues/<date>-<slug> (no-clobber: -2, -3 suffix)
  → Todoist Inbox tasks labelled `monologue`, linking the page
```

## Usage
```
uv sync
uv run omi-pipeline recording.m4a --dry-run     # nothing external written
uv run omi-pipeline recording.m4a               # publish
uv run omi-pipeline --watch ~/omi-intake        # intake watcher (see docs/receiver.md)
uv run omi-pipeline rec.m4a --llm ollama --llm-model qwen2.5:14b
```

Secrets come from the environment or files listed in `OMI_ENV_FILES` (colon-separated):
`ANTHROPIC_API_KEY`, `WIKIJS_TOKEN`, `WIKIJS_URL`, `TODOIST_TOKEN`. Wiki publishing shells out to
the script at `OMI_WIKI_PUSH_SCRIPT`.

Required (no built-in defaults): `WIKIJS_URL` (when publishing), `OMI_WHISPER_URL` (remote transcription),
`OMI_OLLAMA_URL` (ollama LLM). Optional: `OMI_PUBLIC_HOST` (review UI hostname, for CSRF + index link).

Env knobs: `OMI_LLM`, `OMI_ANTHROPIC_MODEL`, `OMI_OLLAMA_MODEL`, `OMI_OLLAMA_URL`, `OMI_WHISPER_URL`,
`OMI_WHISPER_MODEL`, `OMI_ARCHIVE_DIR` (default `~/omi-monologues`).

Raw audio stays local: copied into the archive dir with a `.result.json` and the rendered `.md`.
**Raw audio retention: 7 days** (boss decision 2026-11-10, #4). In `--watch`/`--serve` mode, an hourly pass deletes raw audio older than `OMI_RAW_RETENTION_DAYS` (default `7`; `0` = keep forever; also `--retention-days`) **only if it was successfully published** (intake: `.omi-state.json` status `published`; archive: `<stem>.result.json` status `published`). Failed/deferred/dry-run/unprocessed audio, `.part` files, `.spool/` and the `.md`/`.result.json` outputs are never deleted. Each deletion is logged (`[omi-retention] deleted ...`).

 The `--dry-run` mode still sends audio to the LAN whisper
server and the transcript to the LLM, but writes nothing to the wiki or Todoist.

## Home receiver (k8s ingest + ESP32 bridges)
ESP32-S3 BLE bridges POST Opus to the ingest service, which runs in k8s together with the watcher: [docs/ingest.md](docs/ingest.md). BLE protocol and ESP32 porting notes: [docs/omi-ble-protocol.md](docs/omi-ble-protocol.md). The bleak desk tool is documented in [docs/receiver.md](docs/receiver.md).
