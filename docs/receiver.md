# Home receiver + intake watcher (Route 2)

Goal: the pendant's recordings are pulled over BLE by a host at home (not the Omi phone app),
dropped into an intake folder, and processed by the existing pipeline
(speaches Whisper on Ix :8001 → LLM → Wiki.js + Todoist).

```
Omi pendant --BLE--> omi-receiver (sync|live) --> ~/omi-intake/*.ogg --> omi-pipeline --watch
```

Scope: only the boss's own monologues (Washington two-party consent).

## Hardware (survey 2026-11-09)
- ginaz: no Bluetooth controller.
- Ix: no Bluetooth controller (bluetooth.service inactive).
- devicehub (RPi 3B): onboard BCM43438 `hci0` (UART, BLE 4.1), bluez active, `B8:27:EB:39:DF:B1`.
  Usable but weak (shared WiFi antenna, no 2M PHY). Do not disturb zigbee2mqtt / zwave-js-ui.
- Recommended: a USB BT 5.x dongle (RTL8761B class, e.g. TP-Link UB500 / ASUS USB-BT500) on ginaz.

## Receiver
```
uv sync --extra ble
uv run omi-receiver list                      # storage info + files on the pendant
uv run omi-receiver sync --intake ~/omi-intake  # download stored files (no-delete default)
uv run omi-receiver live --segment 300        # stream live audio into 5-min Ogg segments
```
- Scans for name prefix `Omi` or the audio service UUID (`--address` to pin).
- Writes the pendant clock on connect (`--no-time-sync` to skip) so new file names are real epochs.
- Output: `omi-sd-<YYYYmmdd-HHMMSS>-<ts>.ogg` / `omi-live-<…>.ogg`, written as `.part` then renamed;
  mtime = recording start (pipeline uses it for the page date).
- Resume state: `~/.local/state/omi-receiver.json` (bytes already pulled per file).
- **No delete by default.** See docs/omi-ble-protocol.md: fw 3.0.19 deletes a file after a full read,
  so default sync stops 32 KB early. `--full-read` gets the tail (pendant then deletes it);
  `--delete` additionally sends DELETE and requires `--full-read`.

## Watcher
`omi-pipeline --watch ~/omi-intake [--interval 60] [--settle 15] [--max-attempts 3] [--once]`
- Only audio extensions; ignores `.part`/dotfiles. A file must be unchanged across two scans and older than `--settle`.
- State in `<intake>/.omi-state.json`; published files never re-run; failures retry with backoff (5 min × attempt) up to `--max-attempts`.
- inotify wake-ups if `watchdog` is installed (`uv sync --extra watch`), else polling.
- Never deletes or moves audio (retention: #4).

## Deploy (NOT done — needs the boss)
1. Pick the BLE host (devicehub now, or ginaz after buying a dongle). Clone the repo to `~/omi-monologue-pipeline`, `uv sync --extra ble --extra watch`.
2. With the pendant nearby and the boss present: `uv run omi-receiver list`, then `uv run omi-receiver sync --intake /tmp/omi-test` and check the .ogg plays (`ffprobe`).
3. Dry-run one file: `uv run omi-pipeline --dry-run /tmp/omi-test/<file>.ogg`.
4. Install units from `deploy/systemd/` as user units, `loginctl enable-linger`, enable `omi-pipeline-watch.service` (on the host with the secrets, usually ginaz) and `omi-receiver-sync.timer` (on the BLE host). If those are different hosts, share the intake folder (NFS/rsync) or run both on ginaz with a dongle.
5. Close the Omi phone app's background sync, otherwise it races the receiver for the files.
