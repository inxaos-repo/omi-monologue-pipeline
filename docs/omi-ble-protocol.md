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

## Storage sync — firmware 3.0.21 (for reference; not implemented)

Ring-buffer protocol: cmds `0x10` INFO, `0x11` READ, `0x12` ADVANCE (ack/consume), `0x13` CLEAR;
notifications typed ACK=1, INFO=2, DATA=3, DONE=4, READ_BEGIN=5 with big-endian sequence numbers.
If the pendant is upgraded, `omi_protocol.py` needs a second storage implementation.
