import struct

import pytest

from omi_pipeline import omi_protocol as O


def pkt(pid, idx, payload):
    return struct.pack("<HB", pid, idx) + payload


def test_parse_audio_packet():
    p = O.parse_audio_packet(pkt(0x1234, 2, b"abc"))
    assert (p.packet_id, p.index, p.payload) == (0x1234, 2, b"abc")
    with pytest.raises(ValueError):
        O.parse_audio_packet(b"\x00\x01")


def test_assembler_single_and_fragmented():
    a = O.FrameAssembler()
    assert a.feed(pkt(1, 0, b"F1")) == []
    assert a.feed(pkt(2, 0, b"F2a")) == [b"F1"]
    assert a.feed(pkt(3, 1, b"F2b")) == []
    assert a.feed(pkt(4, 0, b"F3")) == [b"F2aF2b"]
    assert a.flush() == [b"F3"]
    assert a.frames_out == 3 and a.dropped == 0


def test_assembler_drops_on_gap_and_wraps():
    a = O.FrameAssembler()
    a.feed(pkt(0xFFFE, 0, b"A1"))
    a.feed(pkt(0xFFFF, 1, b"A2"))          # contiguous
    assert a.feed(pkt(0x0000, 0, b"B")) == [b"A1A2"]  # u16 wrap
    a.feed(pkt(0x0001, 0, b"C1"))
    assert a.feed(pkt(0x0003, 1, b"C2")) == []         # lost id 2 -> drop C
    assert a.dropped == 1
    assert a.flush() == []


def test_storage_commands():
    assert O.cmd_list() == b"\x10"
    assert O.cmd_read(2, 0x01020304) == b"\x11\x02\x01\x02\x03\x04"
    assert O.cmd_delete(1) == b"\x12\x01"
    assert O.cmd_stop() == b"\x03"
    assert O.time_sync_payload(1) == b"\x01\x00\x00\x00"


def test_parse_file_list():
    data = bytes([2]) + struct.pack(">II", 1762700000, 4400) + struct.pack(">II", 1762703600, 880)
    fs = O.parse_file_list(data)
    assert [(f.index, f.timestamp, f.size) for f in fs] == [(0, 1762700000, 4400), (1, 1762703600, 880)]
    assert O.parse_file_list(b"\x00") == []
    with pytest.raises(ValueError):
        O.parse_file_list(b"\x02" + b"\x00" * 8)


def test_classify_storage_notify():
    assert O.classify_storage_notify(b"\x64") == ("status", 100)
    kind, (ts, chunk) = O.classify_storage_notify(struct.pack(">I", 99) + b"xyz")
    assert kind == "data" and ts == 99 and chunk == b"xyz"


def build_blocks(frames):
    """Mimic firmware write_to_storage(): [len][frame] packed in 440-byte blocks."""
    out, blk = bytearray(), bytearray()
    for f in frames:
        n = len(f) + 1
        if len(blk) + n > O.STORAGE_BLOCK - 1:
            blk.append(len(f))                  # stray length byte, then flush
            out += blk + b"\xee" * (O.STORAGE_BLOCK - len(blk))
            blk = bytearray()
        blk += bytes([len(f)]) + f
    out += blk + b"\x00" * (O.STORAGE_BLOCK - len(blk))
    return bytes(out)


def test_iter_stored_frames_roundtrip():
    frames = [bytes([i % 251]) * (60 + i % 90) for i in range(40)]
    raw = build_blocks(frames)
    assert len(raw) % O.STORAGE_BLOCK == 0
    assert list(O.iter_stored_frames(raw)) == frames


def test_align_offset():
    assert O.align_offset(0) == 0
    assert O.align_offset(881) == 880


def test_ogg_opus_structure():
    frames = [b"\xfc" + bytes(40)] * 120
    ogg = O.ogg_opus(frames, frames_per_page=50)
    pages = [i for i in range(len(ogg)) if ogg.startswith(b"OggS", i)]
    assert len(pages) == 2 + 3
    assert ogg[pages[0] + 28:pages[0] + 36] == b"OpusHead"
    assert ogg[pages[1] + 28:pages[1] + 36] == b"OpusTags"
    for a, b in zip(pages, pages[1:] + [len(ogg)]):
        assert O.ogg_crc_ok(ogg[a:b])
    last = pages[-1]
    flags, granule = ogg[last + 5], struct.unpack_from("<q", ogg, last + 6)[0]
    assert flags & 0x04 and granule == 120 * 960
    head = ogg[pages[0] + 28:pages[0] + 47]
    assert struct.unpack_from("<I", head, 12)[0] == 16000


def test_ogg_crc_known_vector():
    # CRC of empty input is 0; of b"OggS" header-only check is self-consistent
    assert O._ogg_crc(b"") == 0
    page = O._ogg_page([b"x"], 0, 1, 0, 2)
    assert O.ogg_crc_ok(page)


# --- 3.0.21 ring protocol ---------------------------------------------------
def test_ring_commands():
    assert O.ring_cmd_info() == b"\x10"
    assert O.ring_cmd_read(5) == b"\x11" + (5).to_bytes(8, "big")
    assert O.ring_cmd_read(5, 7) == b"\x11" + (5).to_bytes(8, "big") + (7).to_bytes(4, "big")
    assert len(O.ring_cmd_read(1)) == 9 and len(O.ring_cmd_read(1, 2)) == 13
    assert O.ring_cmd_advance(9) == b"\x12" + (9).to_bytes(8, "big")
    assert O.ring_cmd_clear() == b"\x13"


def test_parse_ring_notify_all_types():
    info = bytes([2]) + struct.pack(">QQIQH", 100, 160, 20000, 3, 444)
    k, v = O.parse_ring_notify(info)
    assert k == "info" and (v.read_seq, v.write_seq, v.capacity_packets, v.dropped_packets, v.packet_bytes) == (100, 160, 20000, 3, 444)
    assert v.unread == 60
    assert O.parse_ring_notify(b"\x01\x0a") == ("ack", 10)
    assert O.parse_ring_notify(b"\x03abc") == ("data", b"abc")
    assert O.parse_ring_notify(bytes([4, 0]) + struct.pack(">Q", 161)) == ("done", (0, 161))
    assert O.parse_ring_notify(bytes([5]) + struct.pack(">QI", 100, 60)) == ("begin", (100, 60))
    with pytest.raises(ValueError):
        O.parse_ring_notify(b"\x02short")


def test_ring_assembler_across_notification_boundaries():
    frames = [bytes([i]) * (50 + i) for i in range(30)]
    blocks = build_blocks(frames)
    n = len(blocks) // O.STORAGE_BLOCK
    stream = b"".join(struct.pack(">I", 1762700000 + i) + blocks[i * 440:(i + 1) * 440] for i in range(n))
    asm, got, tss = O.RingPacketAssembler(), [], []
    for i in range(0, len(stream), 243):          # MTU 247 - 3 - 1 marker byte
        for ts, fr in asm.feed(stream[i:i + 243]):
            tss.append(ts)
            got += fr
    assert got == frames and tss == [1762700000 + i for i in range(n)] and asm.leftover == 0


def test_fw_version_protocol_selection():
    assert O.parse_fw_version("3.0.21") == (3, 0, 21)
    assert O.parse_fw_version("v3.0.19-omi") == (3, 0, 19)
    assert O.storage_protocol_for("3.0.19") == "file"
    assert O.storage_protocol_for("3.0.21") == "ring"
    assert O.storage_protocol_for("3.1.0") == "ring"
    assert O.storage_protocol_for("") == "unknown"
