import os
import time

from omi_pipeline.watcher import STATE_NAME, Watcher


def mk(tmp_path, name, data=b"abc", age=100):
    f = tmp_path / name
    f.write_bytes(data)
    t = os.path.getmtime(f) - age
    os.utime(f, (t, t))
    return f


def test_requires_two_stable_scans_and_skips_part(tmp_path):
    mk(tmp_path, "a.ogg")
    mk(tmp_path, "b.ogg.part")
    mk(tmp_path, "notes.txt")
    seen = []
    w = Watcher(tmp_path, lambda f: seen.append(f.name) or {"status": "published"}, settle=1)
    assert w.scan() == []          # first sighting
    assert w.scan() == ["a.ogg"]   # stable
    assert w.scan() == []          # never twice
    assert seen == ["a.ogg"]
    assert (tmp_path / STATE_NAME).exists()
    # state persists across restarts
    w2 = Watcher(tmp_path, lambda f: seen.append(f.name) or {"status": "published"}, settle=1)
    w2.scan(); w2.scan()
    assert seen == ["a.ogg"]


def test_growing_file_waits(tmp_path):
    f = mk(tmp_path, "c.wav")
    w = Watcher(tmp_path, lambda f: {"status": "published"}, settle=0)
    w.scan()
    with open(f, "ab") as fh:
        fh.write(b"more")
    assert w.scan() == []
    assert w.scan() == ["c.wav"]


def test_retry_with_backoff_and_cap(tmp_path):
    mk(tmp_path, "d.m4a")
    clock = [time.time()]
    calls = []

    def boom(f):
        calls.append(1)
        raise RuntimeError("whisper down")

    w = Watcher(tmp_path, boom, settle=1, max_attempts=2, backoff=60, now=lambda: clock[0])
    w.scan(); w.scan()
    assert len(calls) == 1 and w.state["d.m4a"]["status"] == "failed"
    w.scan()
    assert len(calls) == 1         # backoff not elapsed
    clock[0] += 61
    w.scan()
    assert len(calls) == 2
    clock[0] += 10_000
    w.scan()
    assert len(calls) == 2         # capped
    assert (tmp_path / "d.m4a").exists()  # file never deleted


def test_transient_errors_defer_without_using_attempts(tmp_path):
    import urllib.error
    mk(tmp_path, "e.ogg")
    clock = [time.time()]
    calls = []

    def down(f):
        calls.append(1)
        raise urllib.error.URLError(ConnectionRefusedError(111, "refused"))

    w = Watcher(tmp_path, down, settle=1, max_attempts=1, defer=60, now=lambda: clock[0])
    w.scan(); w.scan()
    assert w.state["e.ogg"]["status"] == "deferred" and w.state["e.ogg"].get("attempts", 0) == 0
    for _ in range(5):              # speaches down for a long time: keeps retrying, never gives up
        clock[0] += 61
        w.scan()
    assert len(calls) == 6 and w.state["e.ogg"]["status"] == "deferred"
    w.handler = lambda f: {"status": "published"}
    clock[0] += 61
    w.scan()
    assert w.state["e.ogg"]["status"] == "published"


def test_is_transient():
    import urllib.error
    from omi_pipeline.watcher import is_transient
    assert is_transient(TimeoutError())
    assert is_transient(urllib.error.HTTPError("u", 503, "x", {}, None))
    assert not is_transient(urllib.error.HTTPError("u", 400, "x", {}, None))
    assert not is_transient(ValueError("bad json"))
