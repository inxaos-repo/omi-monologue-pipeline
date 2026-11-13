import json
import os
import threading
import urllib.error
import urllib.request

import pytest

from omi_pipeline import ingest, metrics, review
from omi_pipeline import omi_protocol as O
from omi_pipeline.watcher import STATE_NAME


@pytest.fixture(autouse=True)
def _reset():
    metrics.reset()
    yield
    metrics.reset()


def get(url, headers=None):
    req = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, r.read(), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read(), dict(e.headers)


@pytest.fixture
def data(tmp_path):
    intake, archive = tmp_path / "intake", tmp_path / "archive"
    intake.mkdir(), archive.mkdir()
    ogg = O.ogg_opus([b"\xfc" + b"\x00" * 40] * 50)  # 50 x 20 ms = 1 s
    (intake / "omi-omi-bridge-20261112-193032.ogg").write_bytes(ogg)
    (intake / "omi-omi-bridge-20261112-200000.ogg").write_bytes(ogg)
    (archive / "omi-omi-bridge-20261112-193032.result.json").write_text(json.dumps({
        "status": "published", "transcript": "buy <milk> tomorrow", "title": "Groceries",
        "wiki_url": "https://wiki.example.lan/journal/x", "todoist": [{"id": "123", "content": "buy milk"}],
        "audio": "/data/archive/omi-omi-bridge-20261112-193032.ogg"}))
    # audio already deleted by retention, result kept
    (archive / "omi-phone-20261101-080000.result.json").write_text(json.dumps({
        "status": "published", "transcript": "old one", "audio": "/data/archive/omi-phone-20261101-080000.m4a"}))
    (intake / STATE_NAME).write_text(json.dumps({
        "omi-omi-bridge-20261112-193032.ogg": {"status": "published"},
        "omi-omi-bridge-20261112-200000.ogg": {"status": "deferred", "error": "conn refused", "deferrals": 2}}))
    return intake, archive


def test_load_records(data):
    recs = review.load_records(*data)
    names = [r["name"] for r in recs]
    assert names[0] == "omi-omi-bridge-20261112-200000.ogg"  # newest first
    r = {x["name"]: x for x in recs}
    pub = r["omi-omi-bridge-20261112-193032.ogg"]
    assert pub["status"] == "published" and pub["transcript"].startswith("buy")
    assert pub["device"] == "omi-bridge" and abs(pub["duration"] - 1.0) < 0.05
    old = r["omi-phone-20261101-080000.m4a"]
    assert old["audio"] is None and old["transcript"] == "old one"
    assert r["omi-omi-bridge-20261112-200000.ogg"]["status"] == "deferred"


def test_render_escapes_and_filters(data):
    recs = review.load_records(*data)
    page = review.render_page(recs)
    assert "buy &lt;milk&gt;" in page and "<milk>" not in page
    assert "audio deleted" in page and "app.todoist.com/app/task/123" in page
    assert "old one" not in review.render_page(recs, q="milk")
    assert "old one" in review.render_page(recs, day="2026-11-01")


def test_review_server_requires_sso(data):
    from http.server import ThreadingHTTPServer
    srv = ThreadingHTTPServer(("127.0.0.1", 0), review.make_review_handler(*data, require_sso=True))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        assert get(base + "/review")[0] == 403
        assert get(base + "/healthz")[0] == 200
        h = {"X-authentik-username": "boss"}
        st, body, _ = get(base + "/review?q=milk", h)
        assert st == 200 and b"Groceries" in body
        st, body, hd = get(base + "/review/audio/omi-omi-bridge-20261112-193032.ogg", {**h, "Range": "bytes=0-3"})
        assert st == 206 and body == b"OggS"
        assert get(base + "/review/audio/..%2F.omi-state.json", h)[0] == 404
        assert get(base + "/review/audio/omi-phone-20261101-080000.m4a", h)[0] == 404
    finally:
        srv.shutdown()


def test_metrics_and_bridge_health(data):
    intake, _ = data
    srv = ingest.make_server(intake, "127.0.0.1", 0, token="s3cret")
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        body = json.dumps({"rssi": -61, "ram_q": 2, "spool": 0, "posts_ok": 10, "posts_fail": 1, "ble": True}).encode()
        req = urllib.request.Request(base + "/v1/bridge-health?device=omi-bridge", data=body, method="POST")
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(req, timeout=5)
        assert e.value.code == 401
        req.add_header("Authorization", "Bearer s3cret")
        with urllib.request.urlopen(req, timeout=5) as r:
            assert r.status == 200
        st, text, _ = get(base + "/metrics")
        text = text.decode()
        assert st == 200
        assert 'omi_bridge_rssi{device="omi-bridge"} -61' in text
        assert 'omi_bridge_ble{device="omi-bridge"} 1' in text
        assert "omi_pending_queue 1" in text  # the deferred one
        assert 'omi_last_segment_timestamp_seconds{device="omi-bridge"}' in text
        assert "omi_segments_stored_total 0" in text
        assert "e+" not in text  # timestamps must keep full precision
    finally:
        srv.shutdown()


def test_watcher_counts(tmp_path):
    from omi_pipeline.watcher import Watcher
    f = tmp_path / "a.ogg"
    f.write_bytes(b"x")
    os.utime(f, (0, 0))
    w = Watcher(tmp_path, lambda p: {"status": "published"}, settle=0)
    w.scan()
    w.scan()  # first scan only records the size/mtime signature
    text = metrics.render()
    assert "omi_segments_published_total 1" in text and "omi_segments_transcribed_total 1" in text
