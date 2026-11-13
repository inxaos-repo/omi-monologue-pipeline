# BLE desk receiver (bleak) — test tool / reference

> **Scope change 2026-11-09:** production receivers are ESP32-S3 BLE bridges on the IoT VLAN that POST
> Opus to the k8s ingest service (`docs/ingest.md`). The systemd units were removed. `omi-receiver` stays as a desk
> test tool and reference implementation for the bridge firmware (see `docs/omi-ble-protocol.md`).


Goal: the pendant's recordings are pulled over BLE by a host at home (not the Omi phone app),
dropped into an intake folder, and processed by the existing pipeline
(speaches Whisper on the GPU host :8001 → LLM → Wiki.js + Todoist).

```
Omi pendant --BLE--> omi-receiver (sync|live) --> ~/omi-intake/*.ogg --> omi-pipeline --watch
```

Scope: only the boss's own monologues (Washington two-party consent).

## Hardware (survey 2026-11-09)
- the agent host: no Bluetooth controller.
- the GPU host: no Bluetooth controller (bluetooth.service inactive).
- devicehub (RPi 3B): onboard BCM43438 `hci0` (UART, BLE 4.1), bluez active, `B8:27:EB:39:DF:B1`.
  Usable but weak (shared WiFi antenna, no 2M PHY). Do not disturb zigbee2mqtt / zwave-js-ui.
- Recommended: a USB BT 5.x dongle (RTL8761B class, e.g. TP-Link UB500 / ASUS USB-BT500) on the agent host.

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
- Storage protocol is auto-detected from firmware revision (`--protocol`). **The pendant is on 3.0.21:** reading the ring frees the data on the pendant, so `sync` only reports until you pass `--consume` (data is spooled to `.spool/` first).
- **No delete by default.** See docs/omi-ble-protocol.md: fw 3.0.19 deletes a file after a full read,
  so default sync stops 32 KB early. `--full-read` gets the tail (pendant then deletes it);
  `--delete` additionally sends DELETE and requires `--full-read`.

## Watcher / deploy
See `docs/ingest.md` (k8s container: HTTP ingest + watcher).
