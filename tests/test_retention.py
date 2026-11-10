import json
import os

from omi_pipeline.retention import Periodic, purge
from omi_pipeline.watcher import STATE_NAME, Watcher

DAY = 86400
NOW = 1_800_000_000.0


def mk(d, name, age_days, data=b"x"):
    f = d / name
    f.write_bytes(data)
    t = NOW - age_days * DAY
    os.utime(f, (t, t))
    return f


def test_intake_only_published_and_old(tmp_path):
    intake = tmp_path / "in"
    intake.mkdir()
    old_pub = mk(intake, "a.ogg", 8)
    new_pub = mk(intake, "b.ogg", 2)
    old_fail = mk(intake, "c.ogg", 30)
    old_dry = mk(intake, "d.ogg", 30)
    old_unknown = mk(intake, "e.ogg", 30)
    part = mk(intake, "f.ogg.part", 30)
    (intake / ".spool").mkdir()
    spool = mk(intake / ".spool", "ring-1.raw", 30)
    state = {"a.ogg": {"status": "published"}, "b.ogg": {"status": "published"},
             "c.ogg": {"status": "failed"}, "d.ogg": {"status": "dry-run"}}
    (intake / STATE_NAME).write_text(json.dumps(state))
    deleted = purge(intake, None, 7, now=lambda: NOW)
    assert deleted == [old_pub]
    assert not old_pub.exists()
    for f in (new_pub, old_fail, old_dry, old_unknown, part, spool):
        assert f.exists()
    st = json.loads((intake / STATE_NAME).read_text())
    assert st["a.ogg"]["purged"] == NOW and st["a.ogg"]["status"] == "published"


def test_archive_requires_published_result(tmp_path):
    arc = tmp_path / "arc"
    arc.mkdir()
    a = mk(arc, "a.ogg", 10)
    (arc / "a.result.json").write_text(json.dumps({"status": "published"}))
    b = mk(arc, "b.ogg", 10)
    (arc / "b.result.json").write_text(json.dumps({"status": "failed"}))
    c = mk(arc, "c.ogg", 10)  # no result
    md = mk(arc, "a.md", 10)
    assert purge(None, arc, 7, now=lambda: NOW) == [a]
    assert b.exists() and c.exists() and md.exists() and (arc / "a.result.json").exists()


def test_disabled(tmp_path):
    arc = tmp_path
    mk(arc, "a.ogg", 100)
    (arc / "a.result.json").write_text(json.dumps({"status": "published"}))
    assert purge(None, arc, 0, now=lambda: NOW) == []


def test_periodic_throttles(tmp_path):
    t = [NOW]
    calls = []
    p = Periodic(tmp_path, None, 7, every=3600, now=lambda: t[0])
    import omi_pipeline.retention as R
    orig = R.purge
    R.purge = lambda *a: calls.append(1) or []
    try:
        p(); p()
        t[0] += 3601
        p()
    finally:
        R.purge = orig
    assert len(calls) == 2


def test_watcher_tick_reloads_state(tmp_path):
    f = mk(tmp_path, "a.ogg", 10)
    (tmp_path / STATE_NAME).write_text(json.dumps({"a.ogg": {"status": "published"}}))
    w = Watcher(tmp_path, lambda p: {"status": "published"},
                tick=lambda: purge(tmp_path, None, 7, now=lambda: NOW))
    w._tick()
    assert not f.exists()
    assert w.state["a.ogg"]["purged"] == NOW
