# ESP32-S3 Omi bridge (fw 3.0.24 custody)

Always-on desk bridge: connects to the Omi CV1 pendant over BLE (NimBLE central), uploads
live Opus audio and the stored ring backlog to the receiver (`POST /v1/frames`, bearer
`OMI_INGEST_TOKEN`), and frees pendant storage **only after the receiver answered 2xx**.
Protocol: [`docs/omi-ble-protocol.md`](../../docs/omi-ble-protocol.md) (3.0.24 section).

**Status: builds in CI, never run on hardware.** Default board: **ESP32-S3 SuperMini FH4R2**
(4 MB flash, 2 MB quad PSRAM, WS2812 on GPIO21). Generic devkit is an alternate overlay.

## Boards

| Board | Config | Flash / partitions | PSRAM | Buffers | LED |
|---|---|---|---|---|---|
| SuperMini FH4R2 (default) | `sdkconfig.defaults` | 4 MB QIO, `partitions_4mb.csv`: otadata + 2× 1.875 MB OTA slots | 2 MB quad @80 MHz | live 1 MiB, backlog 512×440 B | WS2812 GPIO21 |
| Generic devkit | `+ sdkconfig.board.generic` | `partitions_generic.csv`: 1.875 MB factory, no OTA | octal (N8R8/N16R8), optional | live 2 MiB | none |

No PSRAM found → live buffer falls back to 64 KiB internal RAM. LED (WS2812): red = no WiFi,
blue = scanning, green = connected + uploads OK, amber = upload retrying. Console is USB-Serial-JTAG
on the SuperMini (native USB-C). OTA slots are reserved only; there is no OTA client yet.

## Flow

1. Scan (name prefix `Omi` or a fixed address) → connect → MTU 517 → discover audio
   `19B10001` + storage `30295781` → subscribe both.
2. `RING_INFO`. No `ring_id` in INFO (fw < 3.0.24) → **live only, ring never touched**.
3. `CUSTODY_ENABLE` (caps APP_ACK_RECLAIM|ADVANCE_IDEMPOTENT|RING_ID, + LIVE_PERSIST if
   enabled) → `RING_INFO` again; `write_seq` is the backlog end snapshot.
4. Backlog: `RING_READ(read_seq, ≤batch)` → buffer 440-byte records (PSRAM) → POST
   `format=omi-sd` (retry with backoff) → on 2xx `RING_ADVANCE_ID(ring_id, start+packets)` → repeat.
   Status 11 (ring_id changed) frees nothing → re-INFO and re-sync.
5. Live: frames reassembled from audio notifies → lp16 into a PSRAM FIFO → POSTed in
   segments (`format=lp16`, default 250 frames = 5 s). With LIVE_PERSIST the pendant also
   stores live audio and sends `LIVE_MARK(ring_id, ring_seq, live_index)`; the bridge acks
   `ring_seq` only when (a) backlog is synced, (b) every live frame up to `live_index` got
   2xx, (c) no gap this connection (BLE fragment loss, queue/FIFO overflow, rejected POST).
   Any gap ⇒ no live acks until reconnect ⇒ the audio is re-read as backlog. At-least-once:
   duplicates are possible, loss is not.

**Multiple bridges:** pendant fw has `CONFIG_BT_MAX_CONN=1` and stops advertising while
connected, so only one central (phone or one bridge) holds it; bridges never force a
disconnect. Custody acks are idempotent and ring_id-checked, so a hand-over cannot free
un-uploaded audio.

## Config (no secrets in the repo)

`idf.py menuconfig` → *Omi S3 bridge* gives defaults; leave token/WiFi empty there and put
them in NVS namespace `omi` instead:

```csv
key,type,encoding,value
omi,namespace,,
wifi_ssid,data,string,MySSID
wifi_pass,data,string,...
ingest_url,data,string,http://omi-ingest.example:8080
token,data,string,...
device,data,string,omi-pendant
pendant_addr,data,string,
bridge_id,data,string,desk-bridge
```

```sh
python $IDF_PATH/components/nvs_flash/nvs_partition_generator/nvs_partition_gen.py generate nvs.csv nvs.bin 0x6000
esptool.py write_flash 0x9000 nvs.bin  # same offset on both tables   # nvs.csv / nvs.bin are gitignored
```

Tunables (menuconfig): backlog batch packets, live buffer KiB, segment frames, backoff, HTTP
timeout, LIVE_PERSIST on/off, LED GPIO (unused yet).

## Build / test

```sh
make -C components/omi_proto/test           # host unit tests (pure protocol logic, ASan/UBSan)
# SuperMini (default)
docker run --rm -v $PWD:/p -w /p espressif/idf:v5.3.2 idf.py -B build-supermini -D SDKCONFIG=build-supermini/sdkconfig build
# generic devkit
docker run --rm -v $PWD:/p -w /p espressif/idf:v5.3.2 idf.py -B build-generic -D SDKCONFIG=build-generic/sdkconfig \
  -D SDKCONFIG_DEFAULTS="sdkconfig.defaults;sdkconfig.board.generic" build
```

## Known limits / TODO

- PSRAM only, no flash spool: on overflow we stop acking (pendant keeps the data), we don't lose it.
- One backlog POST uses the first packet timestamp as `start`; ts discontinuities inside a batch
  (pendant was off) are not split yet. Uptime-based timestamps (RTC invalid) fall back to now.
- Receiver has no dedup; re-sent batches after a crash-before-ack produce duplicate audio.
- `live_index` → frame mapping assumes LIVE_MARK's index is the audio packet id of the last
  persisted frame (per transport.c); verify on hardware.
