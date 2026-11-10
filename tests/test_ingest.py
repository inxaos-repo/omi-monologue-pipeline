import json
import struct
import threading
import urllib.error
import urllib.request

import pytest

from omi_pipeline import ingest
from omi_pipeline import omi_protocol as O


@pytest.fixture
def server(tmp_path):
    srv = ingest.make_server(tmp_path, "127.0.0.1", 0, token="s3cret")
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield tmp_path, f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def post(url, body, token="s3cret"):
    h = {"Authorization": f"Bearer {token}"} if token else {}
    req = urllib.request.Request(url, data=body, headers=h, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def lp16(frames):
    return b"".join(struct.pack("<H", len(f)) + f for f in frames)


def test_parse_lp16():
    fr = [b"a" * 40, b"b" * 80]
    assert ingest.parse_lp16(lp16(fr)) == fr
    with pytest.raises(ValueError):
        ingest.parse_lp16(b"\x10\x00abc")


def test_healthz(server):
    _, base = server
    assert urllib.request.urlopen(base + "/healthz").read() == b"ok"


def test_frames_lp16_to_ogg(server):
    d, base = server
    code, r = post(f"{base}/v1/frames?device=esp1&start=1762700000", lp16([b"\xfc" + bytes(30)] * 60))
    assert code == 201 and r["frames"] == 60
    f = d / r["file"]
    assert f.name.startswith("omi-esp1-") and f.suffix == ".ogg"
    assert f.read_bytes()[:4] == b"OggS" and int(f.stat().st_mtime) == 1762700000
    assert not list(d.glob("*.part"))


def test_frames_omi_sd(server):
    d, base = server
    blk = bytearray()
    for _ in range(5):
        blk += bytes([40]) + bytes(40)
    blk += bytes(O.STORAGE_BLOCK - len(blk))
    code, r = post(f"{base}/v1/frames?format=omi-sd", bytes(blk))
    assert code == 201 and r["frames"] == 5


def test_segment_upload_and_collision(server):
    d, base = server
    for _ in range(2):
        code, r = post(f"{base}/v1/segments?device=x&start=1762700000&ext=wav", b"RIFFdata")
        assert code == 201
    assert len(list(d.glob("*.wav"))) == 2


def test_rejects(server):
    _, base = server
    assert post(f"{base}/v1/segments", b"x", token="nope")[0] == 401
    assert post(f"{base}/v1/segments?ext=exe", b"x")[0] == 415
    assert post(f"{base}/v1/frames?codec=0", b"x")[0] == 415
    assert post(f"{base}/v1/frames", b"\xff\xff")[0] == 400
    assert post(f"{base}/v1/nope", b"x")[0] == 404
