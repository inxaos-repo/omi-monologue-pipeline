"""Omi CV1 BLE protocol: pure parsing/encoding helpers (no BLE, no I/O).

Derived from BasedHardware/omi firmware tags Omi_CV1_v3.0.19 (installed on the
pendant) and Omi_CV1_v3.0.21, files omi/firmware/omi/src/lib/core/{transport,storage}.c.
See docs/omi-ble-protocol.md for the full write-up.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass, field

# --- GATT UUIDs -----------------------------------------------------------
AUDIO_SERVICE = "19b10000-e8f2-537e-4f6c-d104768a1214"
AUDIO_DATA = "19b10001-e8f2-537e-4f6c-d104768a1214"      # read/notify, fragmented codec frames
AUDIO_CODEC = "19b10002-e8f2-537e-4f6c-d104768a1214"     # read, 1 byte codec id
SPEAKER = "19b10003-e8f2-537e-4f6c-d104768a1214"
SETTINGS_SERVICE = "19b10010-e8f2-537e-4f6c-d104768a1214"
TIME_SYNC_SERVICE = "19b10030-e8f2-537e-4f6c-d104768a1214"
TIME_SYNC_WRITE = "19b10031-e8f2-537e-4f6c-d104768a1214"  # write uint32 LE unix epoch
STORAGE_SERVICE = "30295780-4301-eabd-2904-2849adfeae43"
STORAGE_DATA = "30295781-4301-eabd-2904-2849adfeae43"    # write cmds + notify (status/list/data)
STORAGE_INFO = "30295782-4301-eabd-2904-2849adfeae43"    # read
BATTERY_LEVEL = "00002a19-0000-1000-8000-00805f9b34fb"

CODEC_PCM16 = 0
CODEC_PCM8 = 1
CODEC_OPUS = 20
CODEC_OPUS_FS320 = 21   # Opus, 16 kHz mono, 320-sample (20 ms) frames - CV1 default
OPUS_SAMPLE_RATE = 16000
OPUS_FRAME_SAMPLES = 320
OPUS_MAX_FRAME = 160    # CODEC_OUTPUT_MAX_BYTES

# --- live audio stream ----------------------------------------------------
AUDIO_HEADER = 3  # [packet id u16 LE][fragment index u8]


@dataclass
class AudioPacket:
    packet_id: int
    index: int
    payload: bytes


def parse_audio_packet(data: bytes) -> AudioPacket:
    if len(data) < AUDIO_HEADER:
        raise ValueError(f"audio packet too short: {len(data)}")
    pid, idx = struct.unpack_from("<HB", data)
    return AudioPacket(pid, idx, bytes(data[AUDIO_HEADER:]))


@dataclass
class FrameAssembler:
    """Reassemble codec frames from notifications.

    Firmware sends one codec frame per tx buffer, split into MTU-sized fragments
    with index 0..n under one packet id (ids increment per fragment). A new frame
    starts at index 0. Gaps (lost notifications) drop the partial frame.
    """
    frames_out: int = 0
    dropped: int = 0
    _buf: bytearray = field(default_factory=bytearray)
    _last_id: int | None = None
    _last_idx: int = -1

    def feed(self, data: bytes) -> list[bytes]:
        p = parse_audio_packet(data)
        out: list[bytes] = []
        contiguous = self._last_id is not None and p.packet_id == (self._last_id + 1) & 0xFFFF
        if p.index == 0:
            if self._buf:
                out.append(bytes(self._buf))
            self._buf = bytearray(p.payload)
        elif contiguous and p.index == self._last_idx + 1 and self._buf is not None:
            self._buf += p.payload
        else:  # lost fragment(s): discard partial frame
            if self._buf:
                self.dropped += 1
            self._buf = bytearray()
        self._last_id, self._last_idx = p.packet_id, p.index
        self.frames_out += len(out)
        return out

    def flush(self) -> list[bytes]:
        out = [bytes(self._buf)] if self._buf else []
        self._buf = bytearray()
        self.frames_out += len(out)
        return out


# --- storage (3.0.19 file protocol) ---------------------------------------
CMD_STOP_SYNC = 0x03
CMD_LIST_FILES = 0x10
CMD_READ_FILE = 0x11
CMD_DELETE_FILE = 0x12
CMD_HEARTBEAT = 50
STATUS_DONE = 100
STATUS_NAMES = {0: "ok", 6: "invalid", 7: "not-found", 8: "index-out-of-range", 9: "not-ready", 100: "done"}
STORAGE_BLOCK = 440       # MAX_WRITE_SIZE: records are packed per 440-byte block
DATA_HEADER = 4           # data notifications: [file ts u32 BE][chunk]


@dataclass
class StoredFile:
    index: int
    timestamp: int
    size: int


def cmd_list() -> bytes:
    return bytes([CMD_LIST_FILES])


def cmd_read(index: int, offset: int = 0) -> bytes:
    return struct.pack(">BBI", CMD_READ_FILE, index, offset)


def cmd_delete(index: int) -> bytes:
    return struct.pack(">BB", CMD_DELETE_FILE, index)


def cmd_stop() -> bytes:
    return bytes([CMD_STOP_SYNC])


def time_sync_payload(epoch: int) -> bytes:
    return struct.pack("<I", epoch)


def parse_file_list(data: bytes) -> list[StoredFile]:
    """LIST_FILES response: [count u8] then count x [ts u32 BE][size u32 BE]."""
    if not data:
        raise ValueError("empty list response")
    n = data[0]
    if len(data) < 1 + 8 * n:
        raise ValueError(f"list response truncated: count={n} len={len(data)}")
    return [StoredFile(i, *struct.unpack_from(">II", data, 1 + 8 * i)) for i in range(n)]


def parse_storage_info(data: bytes) -> dict:
    keys = ("used_bytes", "file_count", "free_bytes", "flags")
    vals = struct.unpack_from(f"<{min(4, len(data) // 4)}I", data)
    return dict(zip(keys, vals))


def classify_storage_notify(data: bytes) -> tuple[str, object]:
    """('status', code) for 1-byte notifications, else ('data', (ts, chunk))."""
    if len(data) == 1:
        return "status", data[0]
    if len(data) < DATA_HEADER:
        raise ValueError(f"storage notify too short: {len(data)}")
    (ts,) = struct.unpack_from(">I", data)
    return "data", (ts, bytes(data[DATA_HEADER:]))


def align_offset(offset: int) -> int:
    return offset - offset % STORAGE_BLOCK


def iter_stored_frames(raw: bytes, block: int = STORAGE_BLOCK):
    """Yield Opus frames from stored bytes ([len u8][frame] records per block).

    The writer flushes a block when the next record would not fit (leaving a stray
    length byte + stale tail), so a record that would cross the block end, or a
    zero length, means "skip to next block". `raw` must start on a block boundary.
    """
    for base in range(0, len(raw), block):
        blk = raw[base:base + block]
        o = 0
        while o < len(blk):
            n = blk[o]
            if n == 0 or n > OPUS_MAX_FRAME or o + 1 + n > len(blk) or o + 1 + n > block - 1:
                break
            yield bytes(blk[o + 1:o + 1 + n])
            o += 1 + n


# --- Ogg Opus muxer (no decoding needed; ffmpeg/whisper read .ogg) --------
def _ogg_crc(data: bytes) -> int:
    crc = 0
    for b in data:
        crc ^= b << 24
        for _ in range(8):
            crc = ((crc << 1) ^ 0x04C11DB7) & 0xFFFFFFFF if crc & 0x80000000 else (crc << 1) & 0xFFFFFFFF
    return crc


def _ogg_page(packets: list[bytes], granule: int, serial: int, seq: int, flags: int) -> bytes:
    lacing = bytearray()
    for p in packets:
        lacing += b"\xff" * (len(p) // 255) + bytes([len(p) % 255])
    hdr = struct.pack("<4sBBqIIIB", b"OggS", 0, flags, granule, serial, seq, 0, len(lacing)) + lacing
    page = bytearray(hdr + b"".join(packets))
    struct.pack_into("<I", page, 22, _ogg_crc(bytes(page)))
    return bytes(page)


def opus_head(sample_rate: int = OPUS_SAMPLE_RATE, pre_skip: int = 0) -> bytes:
    return struct.pack("<8sBBHIhB", b"OpusHead", 1, 1, pre_skip, sample_rate, 0, 0)


def opus_tags(vendor: str = "omi-monologue-pipeline") -> bytes:
    v = vendor.encode()
    return b"OpusTags" + struct.pack("<I", len(v)) + v + struct.pack("<I", 0)


def ogg_opus(frames: list[bytes], serial: int = 0x0E1, frames_per_page: int = 50,
             frame_samples: int = OPUS_FRAME_SAMPLES, sample_rate: int = OPUS_SAMPLE_RATE) -> bytes:
    """Mux raw Opus frames into an Ogg Opus file. Granule is in 48 kHz units."""
    step = frame_samples * 48000 // sample_rate
    out = [_ogg_page([opus_head(sample_rate)], 0, serial, 0, 0x02),
           _ogg_page([opus_tags()], 0, serial, 1, 0)]
    seq, granule = 2, 0
    frames = [f for f in frames if f]
    for i in range(0, len(frames), frames_per_page):
        chunk = frames[i:i + frames_per_page]
        granule += step * len(chunk)
        last = i + frames_per_page >= len(frames)
        out.append(_ogg_page(chunk, granule, serial, seq, 0x04 if last else 0))
        seq += 1
    return b"".join(out)


def ogg_crc_ok(page: bytes) -> bool:
    p = bytearray(page)
    want = struct.unpack_from("<I", p, 22)[0]
    struct.pack_into("<I", p, 22, 0)
    return _ogg_crc(bytes(p)) == want





# --- storage (3.0.21+ ring protocol) --------------------------------------
# Same characteristic (30295781). The pendant keeps one ring of fixed-size packets
# [ts u32 BE][440-byte record block]; seqs are u64. IMPORTANT: the firmware frees ring
# data by itself as soon as notifications are confirmed sent (link-layer tx-complete),
# checkpointed every ~2 s and on DONE/disconnect. A host cannot read without consuming.
RING_CMD_INFO = 0x10
RING_CMD_READ = 0x11
RING_CMD_ADVANCE = 0x12
RING_CMD_CLEAR = 0x13
RING_NOTIFY_ACK = 0x01
RING_NOTIFY_INFO = 0x02
RING_NOTIFY_DATA = 0x03
RING_NOTIFY_DONE = 0x04
RING_NOTIFY_READ_BEGIN = 0x05
RING_STATUS = {0: "ok", 6: "invalid", 9: "not-ready", 10: "seq-out-of-range"}
RING_TS_BYTES = 4
RING_PACKET = RING_TS_BYTES + STORAGE_BLOCK   # RAW_AUDIO_PACKET_BYTES = 444


def ring_cmd_info() -> bytes:
    return bytes([RING_CMD_INFO])


def ring_cmd_read(start_seq: int, count: int | None = None) -> bytes:
    b = struct.pack(">BQ", RING_CMD_READ, start_seq)
    return b + struct.pack(">I", count) if count else b


def ring_cmd_advance(seq: int) -> bytes:
    return struct.pack(">BQ", RING_CMD_ADVANCE, seq)


def ring_cmd_clear() -> bytes:
    return bytes([RING_CMD_CLEAR])


@dataclass
class RingInfo:
    read_seq: int
    write_seq: int
    capacity_packets: int
    dropped_packets: int
    packet_bytes: int

    @property
    def unread(self) -> int:
        return self.write_seq - self.read_seq


def parse_ring_notify(data: bytes) -> tuple[str, object]:
    """Return (kind, value): ack->status, info->RingInfo, data->bytes,
    done->(status, next_seq), begin->(start_seq, count)."""
    if not data:
        raise ValueError("empty ring notify")
    t = data[0]
    if t == RING_NOTIFY_DATA:
        return "data", bytes(data[1:])
    if t == RING_NOTIFY_ACK and len(data) >= 2:
        return "ack", data[1]
    if t == RING_NOTIFY_INFO and len(data) >= 31:
        r, w, cap, drop, pb = struct.unpack_from(">QQIQH", data, 1)
        return "info", RingInfo(r, w, cap, drop, pb)
    if t == RING_NOTIFY_DONE and len(data) >= 10:
        return "done", (data[1], struct.unpack_from(">Q", data, 2)[0])
    if t == RING_NOTIFY_READ_BEGIN and len(data) >= 13:
        return "begin", struct.unpack_from(">QI", data, 1)
    raise ValueError(f"bad ring notify type={t} len={len(data)}")


@dataclass
class RingPacketAssembler:
    """Split the DATA byte stream into (ts, frames) per 444-byte ring packet."""
    packet_bytes: int = RING_PACKET
    _buf: bytearray = field(default_factory=bytearray)

    def feed(self, chunk: bytes) -> list[tuple[int, list[bytes]]]:
        self._buf += chunk
        out = []
        while len(self._buf) >= self.packet_bytes:
            pkt = bytes(self._buf[:self.packet_bytes])
            del self._buf[:self.packet_bytes]
            (ts,) = struct.unpack_from(">I", pkt)
            out.append((ts, list(iter_stored_frames(pkt[RING_TS_BYTES:], self.packet_bytes - RING_TS_BYTES))))
        return out

    @property
    def leftover(self) -> int:
        return len(self._buf)


def parse_fw_version(s: str) -> tuple[int, ...]:
    import re
    m = re.search(r"(\d+)\.(\d+)\.(\d+)", s or "")
    return tuple(int(x) for x in m.groups()) if m else ()


def storage_protocol_for(version: str) -> str:
    """'file' (<=3.0.19 per-file LIST/READ/DELETE) or 'ring' (3.0.21+). Unknown -> 'ring'
    is NOT assumed; callers must pass --protocol explicitly."""
    v = parse_fw_version(version)
    if not v:
        return "unknown"
    return "ring" if v >= (3, 0, 21) else "file"


FW_REVISION = "00002a26-0000-1000-8000-00805f9b34fb"   # Device Information: firmware revision
__all__ = [n for n in dir() if not n.startswith("_") and n not in ("struct", "dataclass", "field")]
