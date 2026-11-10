# Ingest service (k8s) — HTTP endpoint + watcher

`omi-pipeline --serve /data/intake` (container default) runs:
- an HTTP ingest on `:8080` for ESP32-S3 BLE bridges on the IoT VLAN, and
- the intake watcher (transcribe on speaches → LLM → Wiki.js + Todoist).

Image: `ghcr.io/inxaos-repo/omi-monologue-pipeline` (GHA `image.yml`; tags `latest`, `sha-<full sha>`, `v*`).

## API
Auth: `Authorization: Bearer $OMI_INGEST_TOKEN` (auth is disabled if the variable is unset; always set it in k8s).
- `GET /healthz` → `ok`
- `POST /v1/frames?device=<id>&start=<epoch>&format=lp16|omi-sd&codec=21`. The body is raw Opus.
  - `lp16` = repeated `[len u16 LE][frame]` (live stream from a bridge).
  - `omi-sd` = block-aligned pendant storage bytes (440-byte blocks of `[len u8][frame]`).
  - The server muxes the frames into Ogg Opus.
- `POST /v1/segments?device=<id>&start=<epoch>&ext=ogg|opus|wav|m4a|mp3|flac|webm`. The body is a complete file.
- Responses:
  - `201 {"file","frames","bytes"}` on success.
  - Errors: 400, 401, 413 (> `OMI_INGEST_MAX_BYTES`, default 64 MiB), 415.
- Writes are atomic (`.part` → rename). `start` becomes the file's mtime, which becomes the wiki page date.

Example:
`curl -H "Authorization: Bearer $T" --data-binary @seg.ogg "http://omi-ingest:8080/v1/segments?device=desk&start=$(date +%s)"`

## Watcher behaviour
- A file is processed once it is settled (unchanged across two scans and older than `--settle`).
- State is kept in `/data/intake/.omi-state.json`. Published raw audio is deleted after `OMI_RAW_RETENTION_DAYS` (default 7, #4); unprocessed audio is never deleted.
- **Transient failures are deferred, not failed.** This covers speaches on Ix unreachable, timeouts, DNS errors, HTTP 5xx and 429. The file is retried every `OMI_DEFER_SECONDS` (default 120) forever, and these retries don't use up attempts.
- Other errors count as attempts (`--max-attempts`, default 3) with backoff.
- Wiki pages are created via GraphQL `pages.create` with a no-clobber check (`OMI_WIKI_MODE=graphql`; the image has no wiki_push.py).

## Environment
| Var | Purpose |
|---|---|
| `ANTHROPIC_API_KEY` | LLM (secret) |
| `WIKIJS_TOKEN`, `WIKIJS_URL` | wiki (secret; URL default `https://wiki.k8s.inxaos.com`) |
| `TODOIST_TOKEN` | tasks (secret) |
| `OMI_INGEST_TOKEN` | bridge auth (secret) |
| `OMI_WHISPER_URL` | default `http://192.168.2.25:8001/v1/audio/transcriptions` |
| `OMI_INTAKE_DIR` / `OMI_ARCHIVE_DIR` | `/data/intake`, `/data/archive` |
| `OMI_DEFER_SECONDS`, `OMI_INGEST_PORT` | 120, 8080 |
| `OMI_WIKI_PREFIX` / `OMI_WIKI_TEST_PREFIX` | `journal/monologues` / `test/omi-monologues`. Files from a device id starting with `test` (`omi-test*`) are TEST: test prefix, `test` tag, Todoist label `omi-test`, titles prefixed TEST. |

## What the inxaos-flux HelmRelease needs (not opened yet)
- **Deployment:** 1 replica, strategy `Recreate`. The state file is single-writer.
  - Image: `ghcr.io/inxaos-repo/omi-monologue-pipeline:sha-…` (digest-pinned).
  - Runs as uid 10001 with `fsGroup: 10001`.
  - Probes: `httpGet /healthz :8080`.
- **Storage:** one NAS-backed PVC (NFS, RWX or RWO) mounted at `/data`, holding `intake/` and `archive/`. Size it for audio at about 14 MB per recorded hour, with 7-day raw retention (#4) — roughly a week of audio plus small transcripts.
- **SOPS secret** with `ANTHROPIC_API_KEY`, `WIKIJS_TOKEN`, `TODOIST_TOKEN` and `OMI_INGEST_TOKEN`, injected through `envFrom`.
- **Network:**
  - A Service on port 8080 that the ESP32 bridges on the IoT VLAN can reach. Options: a LoadBalancer IP (MetalLB) on the LAN, or an ingress with TLS, but a plain-HTTP LB IP is simpler for ESP32. Firewall: IoT VLAN → that IP:8080 only.
  - Egress from the pod to Ix `192.168.2.25:8001` (speaches), `wiki.k8s.inxaos.com`, `api.anthropic.com` and `api.todoist.com`.
- **Ghcr pull:** the package must be public, or the cluster needs an imagePullSecret.
