# Omi CV1 BLE protocol (firmware 3.0.19 / 3.0.21)

Source: `BasedHardware/omi` tags `Omi_CV1_v3.0.19` (what the pendant runs) and `Omi_CV1_v3.0.21`,
`omi/firmware/omi/src/lib/core/transport.c` and `storage.c`. GATT UUIDs are the same in both versions.
The **storage protocol is different** between them (see below). Implemented in
`src/omi_pipeline/omi_protocol.py` (pure, unit-tested) and `receiver.py` (bleak).

## Services / characteristics

| Service | Characteristic | Props | Meaning |
|---|---|---|---|
| Audio `19B10000-E8F2-537E-4F6C-D104768A1214` | `19B10001-…` | read, notify | audio data (fragmented codec frames) |
| | `19B10002-…` | read | codec id (1 byte): 0 PCM16, 1 PCM8, 20 Opus, **21 Opus FS320** (CV1 default) |
| | `19B10003-…` | write/notify | speaker / haptic audio |
| Settings `19B10010-…` | `19B10011` dim, `19B10012` mic gain, `19B10013` charging (notify) | | |
| Features `19B10020-…` | `19B10021` | read | feature bitmask |
| Time sync `19B10030-…` | `19B10031` | write | uint32 **LE** unix epoch (sets RTC; file names = epoch) |
| | `19B10032` | read | current time |
| Storage `30295780-4301-EABD-2904-2849ADFEAE43` | `30295781-…` | write, notify | commands + status/list/data notifications |
| | `30295782-…` | read | info: `[used u32 LE][file_count u32 LE][free u32 LE][flags u32 LE]` |
| Button `23BA7924-0000-1000-7450-346EAC492E92` | `23BA7925-…` | notify | button events |
| Accel `32403790-0000-1000-7450-BF445E5829A2` | `32403791-…` | notify | IMU |
| Haptic `CAB1AB95-2EA5-4F4D-BB56-874B72CFC984` | `CAB1AB96-…` | write | vibrate |
| Battery `0x180F` | `0x2A19` | read, notify | percent |

## Live audio (19B10001 notify)

Codec: **Opus (CELT), 16 kHz mono, 320-sample = 20 ms frames, 32 kbps VBR, max 160 bytes/frame.**

Each notification: `[packet_id u16 LE][fragment_index u8][payload…]` (3-byte header, payload ≤ MTU-3).
One codec frame is sent per tx buffer; if it exceeds the MTU it is split into fragments with
index 0,1,2… Each notification increments `packet_id` (wraps at 65535). A new frame starts at
index 0. Reassembly: concatenate fragments until the next index 0; a gap in `packet_id`
drops the partial frame (`FrameAssembler`). Frames are raw Opus packets → muxed into Ogg Opus
(`ogg_opus()`, granule in 48 kHz units, 960 per frame) — no decoding needed, ffmpeg/Whisper read it.

## Storage sync — firmware 3.0.19 (installed)

Commands written to `30295781`; responses arrive as notifications on the same characteristic.

| Cmd | Bytes | Response |
|---|---|---|
| LIST_FILES | `10` | `[count u8]` then count × `[timestamp u32 BE][size u32 BE]` |
| READ_FILE | `11 idx offset(u32 BE)` | `[status]` (0 ok) then data notifications, then `[100]` when complete |
| DELETE_FILE | `12 idx` | `[status]` |
| STOP_SYNC | `03` | stops a running read |
| HEARTBEAT | `32` (50) | keep-alive |

Status bytes: 0 ok, 6 invalid, 7 not found, 8 index out of range, 9 not ready, 100 done.
Data notification: `[file timestamp u32 BE][chunk ≤ 440 bytes]`.

**Auto-delete:** on 3.0.19, when a READ reaches the end of a file that is not the one currently
being recorded, the firmware **deletes the file itself**. The receiver therefore defaults to
stopping `--keep-tail` (32 KB ≈ 8 s) before the end and sending STOP_SYNC; the firmware checks the
stop flag per chunk with ~8.9 KB prefetch, so the margin is several times that. `--full-read`
accepts the auto-delete; `--delete` (explicit DELETE) is off by default and needs `--full-read`.

File format on SD / in the stream: 440-byte blocks (`MAX_WRITE_SIZE`) of records
`[len u8][opus frame]`. When the next record would not fit (`offset + len + 1 > 439`) the writer
puts a stray length byte and flushes the block, so the tail of each block is junk: the decoder
moves to the next block on a zero/oversized length or a record that would cross the block end
(`iter_stored_frames`). Offsets must be block-aligned.

## Storage sync — firmware 3.0.21 (ring protocol; **pendant runs this**, corrected 2026-11-09)

Pick the protocol by reading Device Information **firmware revision `0x2A26`**:
- 3.0.21 or later → ring protocol.
- 3.0.19 or earlier → file protocol.
- `omi-receiver --protocol auto|file|ring`.

The storage is one ring of fixed **444-byte packets** (`RAW_AUDIO_PACKET_BYTES`): `[timestamp u32 BE][440-byte record block]`.
The blocks use the same `[len u8][opus frame]` format as above. Sequence numbers are u64. Commands and notifications both use `30295781`.

| Cmd | Bytes | Effect |
|---|---|---|
| RING_INFO | `10` | → INFO notify |
| RING_READ | `11 start_seq(u64 BE) [count(u32 BE)]` (9 or 13 bytes) | count 0/absent = all unread. Bad seq → ACK 10 |
| RING_ADVANCE | `12 seq(u64 BE)` | moves the read pointer (frees data) |
| RING_CLEAR | `13` | frees everything |
| STOP_SYNC | `03` | → ACK 0 |

Notifications (the first byte is the type):
- `01 status` ACK: 0 ok, 6 invalid, 9 not ready, 10 seq out of range.
- `02 read_seq(u64) write_seq(u64) capacity_packets(u32) dropped_packets(u64) packet_bytes(u16)` INFO, 31 bytes, BE.
- `05 start_seq(u64) count(u32)` READ_BEGIN.
- `03 payload…` DATA: a raw slice of the packet stream, ≤ MTU-4 bytes. Packets span notifications, so reassemble by 444 bytes (`RingPacketAssembler`).
- `04 status next_seq(u64)` DONE.

**Auto-delete: yes, and it's stronger than on 3.0.19.**
- The firmware frees ring data *as it is sent*. `bt_gatt_notify_cb` tx-complete callbacks count the confirmed bytes, and `sync_checkpoint_advance()` moves the read pointer over whole packets every ~2 s, plus on DONE and disconnect.
- **Any read consumes the data on the pendant.** The host can't stop early to keep it, unlike 3.0.19's `--keep-tail` workaround.
- Default behaviour of the receiver on 3.0.21:
  - `list` / `sync` only report ring info.
  - Pulling needs `--consume`. Data is spooled with fsync to `<intake>/.spool/ring-<seq>.raw` *before* it's converted to Ogg, so a crash doesn't lose audio. The spool is removed only after the .ogg is written.
  - `--delete` sends RING_CLEAR and is off by default.
- Packet timestamps come from the pendant RTC, which is set via time sync `19B10031`.

## Porting to an ESP32-S3 bridge (NimBLE)

The bridge does no audio decoding. It forwards Opus frames to the ingest endpoint (`docs/ingest.md`).
1. Scan for name prefix `Omi` or service `19B10000-…`. Connect, request MTU 247 and 2M PHY (both
   the nRF5340 and ESP32-S3 support BLE 5 2M PHY) plus data-length extension (251).
2. Optional: write the current epoch (from SNTP) as u32 LE to `19B10031`.
3. **Live:** read `19B10002` (must be 20 or 21), subscribe to `19B10001`, and reassemble frames exactly as
   `FrameAssembler` does (new frame at fragment index 0, drop the partial frame on a packet-id gap). Buffer
   about 10 s of `[len u16 LE][frame]` and POST it to `/v1/frames?device=<id>&start=<epoch of first frame>`.
   Keep the buffer in PSRAM and resend on HTTP failure.
4. **Storage (fw 3.0.21, current):** send `10` to get INFO, then `11 read_seq [count]`. Strip the 1-byte DATA marker and forward the raw 444-byte packets. The server-side format for this is not implemented yet: either strip the 4-byte ts per packet on the bridge and POST `format=omi-sd`, or extend ingest with `format=omi-ring`. Reading frees pendant storage, so the bridge must persist the data (PSRAM/flash) before the link drops.
4b. **Storage (fw 3.0.19):** subscribe to `30295781`, write `10`, then parse the list. For each file, write
   `11 idx off`, strip the 4-byte ts header from each data notification, and append the chunks.
   POST block-aligned batches (multiples of 440 B) to `/v1/frames?format=omi-sd&start=<ts + off/4000>`.
   Stop `keep_tail` bytes before the end with `03` (fw auto-deletes after a full read). Persist
   `{ts: offset}` in NVS so sync resumes.
5. Match `tests/test_omi_protocol.py`: those synthetic vectors are the spec the firmware must reproduce.

### Throughput (storage sync)
- Audio is about 32 kbps ≈ **4 KB/s**, so one hour of recording ≈ 14 MB (the pendant caps files at ~300 KB each, ≈75 s).
- Firmware 3.0.19 sends at most 440 B of payload per notification, using a prefetch ring of about 20 chunks,
  and paces itself from its storage thread. Real BLE throughput is roughly 5–15 KB/s with a 1M PHY / small
  connection interval on a phone. An ESP32-S3 with MTU 247, a 7.5–15 ms connection interval and 2M PHY
  should reach the top of that range, which is about 2–4× real-time. Syncing a full day (~8 h ≈ 115 MB)
  would therefore take **2–6 hours** of connected time. That is fine overnight on the charger and too
  slow for a quick walk past the bridge.
- Prefer a **live stream whenever the pendant is home** (no backlog builds up) and use storage sync
  only for the away-from-home backlog.
- Notifications are unacknowledged. A drop shows up as a gap in the 440-byte block grid, and the
  decoder resynchronises at the next block. Resume by re-reading from the last fully received block.
- Pendant battery: continuous BLE streaming costs battery. Measure it before running live mode
  24/7.
- The RPi 3B onboard adapter (BLE 4.1, no 2M PHY, shared WiFi antenna) is the worst case and is only good for desk tests.
