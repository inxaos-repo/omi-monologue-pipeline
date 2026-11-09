# omi-monologue-pipeline

Agents/contributors: read [CLAUDE.md](CLAUDE.md) first.

Step 3 of [plans/omi-custom-firmware](https://wiki.k8s.inxaos.com/plans/omi-custom-firmware).
Solo voice monologue → transcript → cleaned text, summary, action items → Wiki.js page + Todoist tasks.

```
audio (m4a/mp3/wav/opus)
  → faster-whisper-server (Ix :8000, large-v3)
  → LLM (anthropic | ollama) → JSON {title, cleaned, summary, action_items, tags}
  → Wiki.js journal/monologues/<date>-<slug> (no-clobber: -2, -3 suffix)
  → Todoist Inbox tasks labelled `monologue`, linking the page
```

## Usage
```
uv sync
uv run omi-pipeline recording.m4a --dry-run     # nothing external written
uv run omi-pipeline recording.m4a               # publish
uv run omi-pipeline --watch ~/omi-drop          # drop folder (stub: polls, .done markers)
uv run omi-pipeline rec.m4a --llm ollama --llm-model qwen2.5:14b
```

Secrets come from the environment or `~/.openclaw/.env` / `~/.openclaw/workspace/.env`:
`ANTHROPIC_API_KEY`, `WIKIJS_TOKEN`, `WIKIJS_URL`, `TODOIST_TOKEN`. Wiki publishing shells out to
`~/.openclaw/workspace/skills/wikijs-push/scripts/wiki_push.py`.

Env knobs: `OMI_LLM`, `OMI_ANTHROPIC_MODEL`, `OMI_OLLAMA_MODEL`, `OMI_OLLAMA_URL`, `OMI_WHISPER_URL`,
`OMI_WHISPER_MODEL`, `OMI_ARCHIVE_DIR` (default `~/omi-monologues`).

Raw audio stays local: copied into the archive dir with a `.result.json` and the rendered `.md`.
Nothing is ever deleted (retention undecided). The `--dry-run` mode still sends audio to the LAN whisper
server and the transcript to the LLM, but writes nothing to the wiki or Todoist.
