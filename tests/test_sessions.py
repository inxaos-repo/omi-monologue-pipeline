import datetime as dt
import json

from omi_pipeline import review, sessions as S
from omi_pipeline.watcher import STATE_NAME

T0 = dt.datetime(2026, 11, 12, 20, 0, 0).timestamp()


def c(name, start, dur=20, dev="omi-bridge", tx="hello"):
    return {"name": name, "device": dev, "start": start, "end": start + dur, "transcript": tx}


def test_group_splits_on_pause_device_and_day():
    cs = [c("a", T0), c("b", T0 + 25), c("c", T0 + 25 + 400),  # pause > 300 -> new session
          c("x", T0 + 10, dev="phone"),
          c("late", dt.datetime(2026, 11, 12, 23, 59, 50).timestamp()),
          c("next", dt.datetime(2026, 11, 13, 0, 0, 15).timestamp())]  # day change
    g = S.group(cs, 300)
    names = sorted([x["name"] for x in s] for s in g)
    assert names == [["a", "b"], ["c"], ["late"], ["next"], ["x"]]


def test_is_closed():
    g = S.group([c("a", T0), c("b", T0 + 25)], 300)
    assert not S.is_closed(g[0], g, 300, T0 + 100)
    assert S.is_closed(g[0], g, 300, T0 + 45 + 300)
    nextday = dt.datetime(2026, 11, 13, 0, 1).timestamp()
    assert S.is_closed(g[0], g, 9999, nextday)


def _setup(tmp_path, n=3):
    intake, archive = tmp_path / "in", tmp_path / "ar"
    intake.mkdir(), archive.mkdir()
    state = {}
    for i in range(n):
        name = f"omi-omi-bridge-20261112-2000{i:02d}.ogg"
        (archive / f"{name[:-4]}.chunk.json").write_text(json.dumps(c(name, T0 + i * 20, tx=f"part {i}")))
        state[name] = {"status": "transcribed"}
    (intake / STATE_NAME).write_text(json.dumps(state))
    return intake, archive


def test_closer_publishes_one_page_per_session(tmp_path):
    intake, archive = _setup(tmp_path)
    calls = []

    def pub(sess, sid):
        calls.append((sid, [x["name"] for x in sess], " ".join(x["transcript"] for x in sess)))
        return {"status": "published", "wiki_url": "https://w/journal/monologues/x",
                "wiki_path": "journal/monologues/2026-11-12-x", "title": "X", "date": "2026-11-12"}

    now = [T0 + 100]
    cl = S.SessionCloser(intake, archive, pub, pause=300, now=lambda: now[0])
    assert cl() is False and not calls          # still open
    now[0] = T0 + 60 + 301
    assert cl() is True
    assert len(calls) == 1 and calls[0][2] == "part 0 part 1 part 2"
    st = json.loads((intake / STATE_NAME).read_text())
    assert all(v["status"] == "published" and v["session"] == calls[0][0] for v in st.values())
    assert cl() is False                        # nothing left
    sj = json.loads((archive / f"{calls[0][0]}.session.json").read_text())
    assert len(sj["chunks"]) == 3
    page = review.render_page(review.load_records(intake, archive), sessions=review.load_sessions(archive))
    assert page.count('class="rec sess"') == 1 and "3 chunks" in page


def test_closer_backs_off_on_failure(tmp_path):
    intake, archive = _setup(tmp_path, 1)
    n = [0]

    def pub(sess, sid):
        n[0] += 1
        raise RuntimeError("llm down")

    now = [T0 + 1000]
    cl = S.SessionCloser(intake, archive, pub, pause=300, retry=120, now=lambda: now[0])
    cl(); cl()
    assert n[0] == 1
    now[0] += 121
    cl()
    assert n[0] == 2
    assert json.loads((intake / STATE_NAME).read_text())[next(iter(json.loads((intake / STATE_NAME).read_text())))]["status"] == "transcribed"


def test_watcher_does_not_redo_transcribed(tmp_path):
    from omi_pipeline.watcher import Watcher
    (tmp_path / STATE_NAME).write_text(json.dumps({"a.ogg": {"status": "transcribed"}}))
    assert not Watcher(tmp_path, lambda p: {}).due("a.ogg")


PAGES = [
    {"id": 1, "path": "journal/monologues/2026-11-12-a", "title": "A", "createdAt": "2026-11-13T03:34:00Z"},
    {"id": 2, "path": "journal/monologues/2026-11-12-b", "title": "B", "createdAt": "2026-11-13T03:40:00Z"},
    {"id": 3, "path": "journal/monologues/2026-11-10-old", "title": "Old", "createdAt": "2026-11-10T03:00:00Z"},
    {"id": 4, "path": "journal/other", "title": "x", "createdAt": ""},
]


def test_index_rebuilt_from_wiki_list():
    md = S.render_index(S.index_from_pages(PAGES, "journal/monologues"), "Monologues")
    assert md.index("## 2026-11-12") < md.index("## 2026-11-10")
    assert md.index("(/journal/monologues/2026-11-12-b)") < md.index("(/journal/monologues/2026-11-12-a)")
    assert "journal/other" not in md and S.INDEX_MARKER in md


def test_index_keeper_retries_until_ok(monkeypatch):
    from omi_pipeline import metrics
    metrics.reset()
    calls, sleeps = [], []

    def writer(path, md, title):
        calls.append(path)
        if len(calls) < 3:
            raise TimeoutError("wiki slow")
        return {"action": "updated", "id": 950}

    k = S.IndexKeeper("journal/monologues", "Monologues", lister=lambda p: PAGES, writer=writer,
                      base=30, cap=60, sleep=sleeps.append)
    assert k.run_until_ok()["id"] == 950
    assert calls == ["journal/monologues"] * 3 and sleeps == [30, 60]
    text = metrics.render()
    assert 'omi_wiki_index_failures_total{prefix="journal/monologues"} 2' in text
    assert "omi_wiki_index_last_success_timestamp_seconds" in text


def test_index_keeper_thread_rebuilds_on_start_and_request():
    import threading
    done = threading.Semaphore(0)
    n = []

    def writer(path, md, title):
        n.append(md)
        done.release()
        return {"action": "updated", "id": 950}

    k = S.IndexKeeper("journal/monologues", "M", lister=lambda p: PAGES, writer=writer).start()
    assert done.acquire(timeout=5)       # startup rebuild
    k.request()
    assert done.acquire(timeout=5)       # publish-triggered rebuild


def test_publish_paused_holds_session(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from omi_pipeline import cli
    monkeypatch.setenv("OMI_PUBLISH_ENABLED", "false")
    called = []
    monkeypatch.setattr(cli.P, "get_llm", lambda *a: called.append("llm"))
    monkeypatch.setattr(cli.P, "wiki_publish", lambda *a: called.append("wiki"))
    monkeypatch.setattr(cli, "update_index", lambda *a: called.append("index"))
    intake, archive = _setup(tmp_path)
    args = SimpleNamespace(archive_dir=str(archive), dry_run=False, test=False, llm="x", llm_model="y")
    cl = S.SessionCloser(intake, archive, lambda s, sid: cli.publish_session(s, sid, args),
                         pause=300, now=lambda: T0 + 10_000)
    assert cl() is True and called == []
    st = json.loads((intake / STATE_NAME).read_text())
    assert {v["status"] for v in st.values()} == {"held"}
    page = review.render_page(review.load_records(intake, archive), sessions=review.load_sessions(archive))
    assert "session held (not published)" in page and "part 1" in page
    from omi_pipeline.watcher import Watcher
    assert not Watcher(intake, lambda p: {}).due(next(iter(st)))
