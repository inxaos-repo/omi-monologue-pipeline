"""omi-receiver: pull audio from an Omi CV1 pendant over BLE (bleak) into the intake folder.

Modes:
  live  - subscribe to the audio characteristic, write Ogg Opus segments every N seconds.
  sync  - list files stored on the pendant (3.0.19 storage protocol) and download them.
  list  - just print stored files / storage info.

Files are written as `<name>.ogg.part` then atomically renamed, so the watcher never
sees partial files. mtime is set to the recording start so the pipeline dates pages
correctly.

SAFETY: never sends DELETE unless --delete is given. Note firmware 3.0.19 deletes a
file by itself after a *complete* read; by default we therefore stop each read
--keep-tail bytes before the end (CMD_STOP) so the pendant keeps the file. Use
--full-read to accept the firmware's auto-delete and get the last seconds too.
Only record the boss's own monologues (WA two-party consent).
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import os
import sys
import time
from pathlib import Path

from . import omi_protocol as O

DEFAULT_INTAKE = os.environ.get("OMI_INTAKE_DIR", "~/omi-intake")
STATE_FILE = "~/.local/state/omi-receiver.json"


def log(m: str) -> None:
    print(f"[omi-rx] {m}", file=sys.stderr, flush=True)


def write_ogg(intake: Path, name: str, frames: list[bytes], mtime: float) -> Path | None:
    if not frames:
        return None
    intake.mkdir(parents=True, exist_ok=True)
    final = intake / f"{name}.ogg"
    n = 2
    while final.exists():
        final = intake / f"{name}-{n}.ogg"
        n += 1
    part = final.with_name(final.name + ".part")
    part.write_bytes(O.ogg_opus(frames))
    os.utime(part, (mtime, mtime))
    part.rename(final)
    log(f"wrote {final} ({len(frames)} frames, {len(frames) * 0.02:.0f}s)")
    return final


def stamp(ts: float) -> str:
    return dt.datetime.fromtimestamp(ts).strftime("%Y%m%d-%H%M%S")


def _bleak():
    try:
        import bleak
    except ImportError:  # pragma: no cover
        sys.exit("bleak not installed: uv sync --extra ble")
    return bleak


async def find_device(name: str, address: str | None, timeout: float):
    bleak = _bleak()
    if address:
        return await bleak.BleakScanner.find_device_by_address(address, timeout=timeout)
    def match(d, adv):
        uuids = [u.lower() for u in (adv.service_uuids or [])]
        return (d.name or adv.local_name or "").lower().startswith(name.lower()) or O.AUDIO_SERVICE in uuids
    return await bleak.BleakScanner.find_device_by_filter(match, timeout=timeout)


async def connect(args):
    bleak = _bleak()
    dev = await find_device(args.name, args.address, args.scan_timeout)
    if not dev:
        log(f"no Omi found (name~{args.name!r}) within {args.scan_timeout}s; nothing to do")
        raise SystemExit(0)
    log(f"connecting to {dev.name} {dev.address}")
    client = bleak.BleakClient(dev)
    await client.connect()
    if args.time_sync:
        try:
            await client.write_gatt_char(O.TIME_SYNC_WRITE, O.time_sync_payload(int(time.time())), response=True)
            log("time synced")
        except Exception as e:  # older fw may lack it
            log(f"time sync failed: {e}")
    return client


async def run_live(args) -> None:
    intake = Path(args.intake).expanduser()
    client = await connect(args)
    try:
        codec = (await client.read_gatt_char(O.AUDIO_CODEC))[0]
        if codec not in (O.CODEC_OPUS, O.CODEC_OPUS_FS320):
            sys.exit(f"unsupported codec id {codec} (expected Opus 20/21)")
        asm, frames, start = O.FrameAssembler(), [], time.time()

        def on_audio(_c, data: bytearray):
            frames.extend(asm.feed(bytes(data)))

        await client.start_notify(O.AUDIO_DATA, on_audio)
        log(f"streaming (codec {codec}); segment {args.segment}s; Ctrl-C to stop")
        deadline = time.time() + args.duration if args.duration else None
        while client.is_connected and (deadline is None or time.time() < deadline):
            await asyncio.sleep(1)
            if time.time() - start >= args.segment:
                chunk, frames[:] = list(frames), []
                write_ogg(intake, f"omi-live-{stamp(start)}", chunk, start)
                start = time.time()
        frames.extend(asm.flush())
        write_ogg(intake, f"omi-live-{stamp(start)}", list(frames), start)
        if asm.dropped:
            log(f"dropped {asm.dropped} partial frames")
    finally:
        await client.disconnect()


class StorageSession:
    def __init__(self, client):
        self.client = client
        self.q: asyncio.Queue = asyncio.Queue()

    async def start(self):
        await self.client.start_notify(O.STORAGE_DATA, lambda _c, d: self.q.put_nowait(bytes(d)))

    async def send(self, payload: bytes):
        await self.client.write_gatt_char(O.STORAGE_DATA, payload, response=True)

    async def list_files(self) -> list[O.StoredFile]:
        await self.send(O.cmd_list())
        return O.parse_file_list(await asyncio.wait_for(self.q.get(), 10))

    async def read(self, f: O.StoredFile, offset: int, stop_at: int, idle: float) -> bytes:
        offset = O.align_offset(offset)
        await self.send(O.cmd_read(f.index, offset))
        buf = bytearray()
        status_seen = False
        while True:
            try:
                msg = await asyncio.wait_for(self.q.get(), idle)
            except TimeoutError:
                log("read idle timeout")
                break
            kind, val = O.classify_storage_notify(msg)
            if kind == "status":
                if val == 0 and not status_seen:
                    status_seen = True
                    continue
                log(f"status {val} ({O.STATUS_NAMES.get(val, '?')})")
                break
            _ts, chunk = val
            buf += chunk
            if offset + len(buf) >= stop_at:
                await self.send(O.cmd_stop())
                del buf[stop_at - offset:]
                break
        await asyncio.sleep(0.5)
        while not self.q.empty():
            self.q.get_nowait()
        return bytes(buf)


def load_state() -> dict:
    p = Path(STATE_FILE).expanduser()
    return json.loads(p.read_text()) if p.exists() else {}


def save_state(st: dict) -> None:
    p = Path(STATE_FILE).expanduser()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(st, indent=2))


async def detect_protocol(client, override: str) -> str:
    if override != "auto":
        return override
    try:
        ver = (await client.read_gatt_char(O.FW_REVISION)).decode(errors="replace").strip("\x00 ")
    except Exception as e:
        log(f"cannot read firmware revision (0x2A26): {e}")
        ver = ""
    proto = O.storage_protocol_for(ver)
    log(f"firmware revision {ver!r} -> storage protocol {proto}")
    if proto == "unknown":
        sys.exit("unknown firmware version; pass --protocol file|ring")
    return proto


async def run_ring(client, args, list_only: bool) -> None:
    """fw 3.0.21+: ring buffer. The firmware frees whatever it has sent (tx-confirmed),
    so reading == consuming. Requires --consume; spools raw bytes to disk as they arrive."""
    q: asyncio.Queue = asyncio.Queue()
    await client.start_notify(O.STORAGE_DATA, lambda _c, d: q.put_nowait(bytes(d)))
    await client.write_gatt_char(O.STORAGE_DATA, O.ring_cmd_info(), response=True)
    info = None
    while info is None:
        kind, val = O.parse_ring_notify(await asyncio.wait_for(q.get(), 10))
        if kind == "info":
            info = val
        elif kind == "ack":
            sys.exit(f"ring info failed: {O.RING_STATUS.get(val, val)}")
    log(f"ring: read_seq={info.read_seq} write_seq={info.write_seq} unread={info.unread} packets "
        f"(~{info.unread * 440 / 4000:.0f}s audio) capacity={info.capacity_packets} dropped={info.dropped_packets}")
    if list_only or info.unread == 0:
        return
    if not args.consume:
        log("NOT reading: on fw 3.0.21 the pendant frees ring data as it is sent, so any sync deletes it "
            "from the pendant. Re-run with --consume to pull (audio is spooled locally first).")
        return
    intake = Path(args.intake).expanduser()
    spool_dir = intake / ".spool"
    spool_dir.mkdir(parents=True, exist_ok=True)
    count = min(info.unread, args.max_packets) if args.max_packets else None
    spool = spool_dir / f"ring-{info.read_seq}.raw"
    asm, frames, first_ts, got = O.RingPacketAssembler(info.packet_bytes or O.RING_PACKET), [], None, 0
    await client.write_gatt_char(O.STORAGE_DATA, O.ring_cmd_read(info.read_seq, count), response=True)
    with open(spool, "ab") as sp:
        while True:
            try:
                msg = await asyncio.wait_for(q.get(), args.idle_timeout)
            except asyncio.TimeoutError:
                log("ring read idle timeout")
                break
            kind, val = O.parse_ring_notify(msg)
            if kind == "data":
                sp.write(val)
                sp.flush()
                os.fsync(sp.fileno())
                for ts, fr in asm.feed(val):
                    first_ts = first_ts or ts
                    frames += fr
                    got += 1
            elif kind == "begin":
                log(f"read begin seq={val[0]} count={val[1]}")
            elif kind == "done":
                log(f"done status={O.RING_STATUS.get(val[0], val[0])} next_seq={val[1]} packets={got}")
                break
            elif kind == "ack" and val != 0:
                log(f"read refused: {O.RING_STATUS.get(val, val)}")
                break
    start = first_ts if first_ts and first_ts > 1_600_000_000 else time.time()
    if write_ogg(intake, f"omi-sd-{stamp(start)}-{info.read_seq}", frames, start):
        spool.unlink()   # ogg written; keep spool otherwise for manual recovery
    if args.delete:
        await client.write_gatt_char(O.STORAGE_DATA, O.ring_cmd_clear(), response=True)
        log("ring CLEARED on pendant (--delete)")


async def run_sync(args, list_only: bool = False) -> None:
    intake = Path(args.intake).expanduser()
    client = await connect(args)
    try:
        if await detect_protocol(client, args.protocol) == "ring":
            return await run_ring(client, args, list_only)
        try:
            log(f"storage info: {O.parse_storage_info(await client.read_gatt_char(O.STORAGE_INFO))}")
        except Exception as e:
            log(f"storage info unavailable: {e}")
        s = StorageSession(client)
        await s.start()
        files = await s.list_files()
        for f in files:
            log(f"file #{f.index} ts={f.timestamp} ({stamp(f.timestamp)}) size={f.size}")
        if list_only:
            return
        st = load_state()
        for f in files:
            key = f"{f.timestamp}"
            done = st.get(key, 0)
            stop_at = f.size if args.full_read else max(0, f.size - args.keep_tail)
            if stop_at - O.align_offset(done) < O.STORAGE_BLOCK:
                continue
            raw = await s.read(f, done, stop_at, args.idle_timeout)
            start = O.align_offset(done)
            usable = len(raw) - len(raw) % O.STORAGE_BLOCK if not args.full_read else len(raw)
            frames = list(O.iter_stored_frames(raw[:usable]))
            mtime = f.timestamp + start / 4000 if f.timestamp > 1_600_000_000 else time.time()
            write_ogg(intake, f"omi-sd-{stamp(mtime)}-{f.timestamp}", frames, mtime)
            st[key] = start + usable
            save_state(st)
            if args.delete and args.full_read and start + len(raw) >= f.size:
                await s.send(O.cmd_delete(f.index))
                log(f"deleted #{f.index} on pendant (--delete)")
    finally:
        await client.disconnect()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="omi-receiver", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("mode", choices=["live", "sync", "list"])
    ap.add_argument("--intake", default=DEFAULT_INTAKE, help="pipeline watch folder")
    ap.add_argument("--name", default="Omi", help="advertised name prefix")
    ap.add_argument("--address", help="BLE address (skip name scan)")
    ap.add_argument("--scan-timeout", type=float, default=20)
    ap.add_argument("--no-time-sync", dest="time_sync", action="store_false")
    ap.add_argument("--segment", type=int, default=300, help="live: seconds per file")
    ap.add_argument("--duration", type=int, default=0, help="live: stop after N s (0=forever)")
    ap.add_argument("--keep-tail", type=int, default=32768,
                    help="sync: stop N bytes before end so fw 3.0.19 does not auto-delete")
    ap.add_argument("--full-read", action="store_true", help="sync: read to end (fw auto-deletes file!)")
    ap.add_argument("--delete", action="store_true", help="sync: explicitly delete after full read (off)")
    ap.add_argument("--idle-timeout", type=float, default=15)
    ap.add_argument("--protocol", choices=["auto", "file", "ring"], default="auto",
                    help="storage protocol; auto reads Device Information fw revision (3.0.21+ = ring)")
    ap.add_argument("--consume", action="store_true",
                    help="ring (fw 3.0.21+): allow reading, which frees the data on the pendant")
    ap.add_argument("--max-packets", type=int, default=0, help="ring: limit packets per sync (444 B each)")
    args = ap.parse_args(argv)
    if args.delete and not (args.full_read or args.consume):
        ap.error("--delete requires --full-read (fw <=3.0.19) or --consume (fw 3.0.21+)")
    coro = run_live(args) if args.mode == "live" else run_sync(args, list_only=args.mode == "list")
    try:
        asyncio.run(coro)
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
