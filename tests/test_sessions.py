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
    # index + review see it as one session
    e = S.index_entries(archive, "journal/monologues")
    assert len(e) == 1 and e[0]["chunks"] == 3
    md = S.render_index(e, "Monologues")
    assert S.INDEX_MARKER in md and "## 2026-11-12" in md and "(/journal/monologues/2026-11-12-x)" in md
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
