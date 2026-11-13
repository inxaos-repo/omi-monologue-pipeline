"""3.0.24 custody protocol (RING_ADVANCE_ID after durable save)."""
import asyncio
import struct
import types

import pytest

from omi_pipeline import omi_protocol as O
from omi_pipeline import receiver as R


def info_bytes(read, write, ring_id, caps=0x0F, ver=1, cap=100000, drop=0, pb=444):
    return bytes([2]) + struct.pack(">QQIQHBBQ", read, write, cap, drop, pb, caps, ver, ring_id)


def test_cmd_encoding():
    assert O.ring_cmd_custody() == bytes([0x14, 1, 0x0D])
    assert O.ring_cmd_custody(0x0F) == bytes([0x14, 1, 0x0F])
    b = O.ring_cmd_advance_id(0x1122334455667788, 42)
    assert len(b) == 17 and b[0] == 0x15
    assert struct.unpack(">QQ", b[1:]) == (0x1122334455667788, 42)


def test_parse_info_3024_and_legacy():
    k, i = O.parse_ring_notify(info_bytes(5, 9, 0xABC))
    assert k == "info" and i.ring_id == 0xABC and i.custody and i.unread == 4 and i.caps == 0x0F
    k, i = O.parse_ring_notify(info_bytes(5, 9, 0xABC)[:31])
    assert k == "info" and i.ring_id is None and not i.custody


def test_parse_acks_and_live_mark():
    assert O.parse_ring_notify(bytes([1, 11])) == ("ack", 11)
    assert O.parse_ring_notify(bytes([1, 0, 0x0F])) == ("custody", 0x0F)
    lm = bytes([6]) + struct.pack(">QQH", 7, 1234, 65535)
    assert O.parse_ring_notify(lm) == ("live_mark", (7, 1234, 65535))


def test_advance_target():
    assert O.custody_advance_target(100, 0, 10) is None
    assert O.custody_advance_target(100, 3, 10) == 103
    assert O.custody_advance_target(100, 50, 10) == 110


def test_protocol_for_version():
    assert O.storage_protocol_for("3.0.24") == "custody"
    assert O.storage_protocol_for("v3.0.21") == "ring"
    assert O.storage_protocol_for("3.0.19") == "file"


def packet(ts, frames):
    rec = b"".join(bytes([len(f)]) + f for f in frames)
    return struct.pack(">I", ts) + rec.ljust(440, b"\0")


class FakePendant:
    """Minimal 3.0.24 storage model; records ADVANCE_ID and the intake state at that moment."""

    def __init__(self, packets, ring_id=0xBEEF, intake=None, mismatch=False):
        self.packets, self.ring_id, self.read, self.intake = packets, ring_id, 0, intake
        self.mismatch, self.advances, self.cb = mismatch, [], None

    async def start_notify(self, _u, cb):
        self.cb = cb

    def n(self, b):
        self.cb(None, bytearray(b))

    async def write_gatt_char(self, _u, data, response=True):
        op = data[0]
        if op == 0x10:
            self.n(info_bytes(self.read, len(self.packets), self.ring_id))
        elif op == 0x14:
            self.n(bytes([1, 0, data[2] & 0x0F]))
        elif op == 0x11:
            start, = struct.unpack_from(">Q", data, 1)
            cnt = struct.unpack_from(">I", data, 9)[0] if len(data) == 13 else len(self.packets) - start
            self.n(bytes([5]) + struct.pack(">QI", start, cnt))
            raw = b"".join(self.packets[start:start + cnt])
            for o in range(0, len(raw), 240):
                self.n(bytes([3]) + raw[o:o + 240])
            self.n(bytes([4, 0]) + struct.pack(">Q", self.read))
        elif op == 0x15:
            rid, seq = struct.unpack_from(">QQ", data, 1)
            saved = sorted(p.name for p in self.intake.glob("*.ogg"))
            self.advances.append((rid, seq, saved))
            if self.mismatch or rid != self.ring_id:
                return self.n(bytes([1, 11]))
            self.read = max(self.read, seq)
            self.n(bytes([1, 0]))


def run(fake, tmp_path, **kw):
    args = types.SimpleNamespace(intake=str(tmp_path), max_packets=kw.get("max_packets", 0), idle_timeout=2)
    asyncio.run(R.run_custody(fake, args, list_only=False))


def test_ack_only_after_durable_save(tmp_path):
    pk = [packet(1_700_000_000 + i, [b"\x01" * 40, b"\x02" * 40]) for i in range(5)]
    fake = FakePendant(pk, intake=tmp_path)
    run(fake, tmp_path, max_packets=2)
    assert [(a[0], a[1]) for a in fake.advances] == [(0xBEEF, 2), (0xBEEF, 4), (0xBEEF, 5)]
    for i, (_, _, saved) in enumerate(fake.advances):
        assert len(saved) == i + 1          # file for this batch existed before its ack
    assert fake.read == 5
    assert not list(tmp_path.glob("*.part"))


def test_ring_id_mismatch_frees_nothing_keeps_audio(tmp_path):
    pk = [packet(1_700_000_000, [b"\x03" * 30])]
    fake = FakePendant(pk, intake=tmp_path, mismatch=True)
    run(fake, tmp_path)
    assert fake.read == 0 and len(list(tmp_path.glob("*.ogg"))) == 1


def test_no_custody_caps_refuses_to_read(tmp_path):
    class Legacy(FakePendant):
        async def write_gatt_char(self, u, data, response=True):
            if data[0] == 0x10:
                return self.n(info_bytes(0, 3, 0)[:31])
            raise AssertionError(f"unexpected cmd {data[0]:#x}")
    run(Legacy([packet(1, [b"x"])] * 3, intake=tmp_path), tmp_path)
