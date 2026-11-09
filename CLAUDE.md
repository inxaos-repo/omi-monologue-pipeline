# CLAUDE.md — agent guide for omi-monologue-pipeline

Plan: [plans/omi-custom-firmware](https://wiki.k8s.inxaos.com/plans/omi-custom-firmware) (this repo is step 3).

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
- `tests/test_core.py` — pytest for `core.py` only (no network).

Key flags:
- `--dry-run` — transcribe + LLM only; writes the `.md`/`.result.json` locally but **nothing to Wiki.js or Todoist**. (Still sends audio to LAN Whisper and the transcript to the LLM.)
- `--fallback-local` — if remote Whisper fails, transcribe on local CPU (`faster-whisper`, needs `uv sync --extra local`; model `--local-model`, default `small.en`). `--transcriber local` skips remote entirely.
- `--watch DIR [--once] [--interval N]` — **stub** (issue #3): polls, skips files modified <10s ago, writes `<file>.done` markers.
- `--test` — adds wiki tag `test`. `--llm anthropic|ollama`, `--llm-model`, `--label` (default `monologue`), `--archive-dir`, `--no-archive-copy`.

Exit code 0 on `published`/`dry-run`, 3 otherwise. Final JSON (minus transcript) printed to stdout; logs to stderr.

## Config / env
No secrets in the repo, ever. `load_env()` reads `~/.openclaw/.env` and `~/.openclaw/workspace/.env` (setdefault; real env wins).
- Secrets: `ANTHROPIC_API_KEY`, `WIKIJS_TOKEN`, `TODOIST_TOKEN`; `WIKIJS_URL` (default `https://wiki.k8s.inxaos.com`).
- Knobs: `OMI_LLM`, `OMI_ANTHROPIC_MODEL` (default `claude-sonnet-4-5`), `OMI_OLLAMA_MODEL` (`qwen2.5:14b`), `OMI_OLLAMA_URL` (`http://192.168.2.25:11434`), `OMI_WHISPER_URL`, `OMI_WHISPER_MODEL` (`Systran/faster-whisper-large-v3`), `OMI_TRANSCRIBER`, `OMI_LOCAL_WHISPER_MODEL`, `OMI_ARCHIVE_DIR` (`~/omi-monologues`).

## External dependencies
- **Whisper on Ix (192.168.2.25)** — default URL is `:8000/v1/audio/transcriptions`, which is currently **broken**. Moving to **speaches on :8001** (issue #1). Until then set `OMI_WHISPER_URL` or use `--fallback-local`.
- **LLM** — Anthropic now; Ix Ollama is the intended later default (issue #4).
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
- **Never delete raw audio** (archive dir or source). Retention policy is undecided (issue #4).
- The work-content guard was **intentionally removed** on 2026-11-09 by the boss. Do not re-add it.
- Update the Wiki plan page after meaningful changes.
