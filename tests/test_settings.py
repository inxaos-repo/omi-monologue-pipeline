import json
import threading
import urllib.error
import urllib.request
from types import SimpleNamespace

import pytest

from omi_pipeline import metrics, review, settings
from omi_pipeline import sessions as S
from omi_pipeline.watcher import STATE_NAME


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    monkeypatch.delenv("OMI_LLM_ENABLED", raising=False)
    monkeypatch.delenv("OMI_PUBLISH_ENABLED", raising=False)
    intake = tmp_path / "intake"
    intake.mkdir()
    settings.configure(intake)
    metrics.reset()
    yield intake
    settings._dir = None
    metrics.reset()


def test_env_defaults_and_persistence(cfg, monkeypatch):
    assert settings.get("llm_enabled") and settings.get("publish_enabled")
    monkeypatch.setenv("OMI_LLM_ENABLED", "false")
    assert settings.get("llm_enabled") is False              # env default when file has no value
    st = settings.set_values({"llm_enabled": True, "publish_enabled": False, "bogus": True}, "damon")
    assert st["llm_enabled"] is True and st["publish_enabled"] is False  # file beats env
    assert st["updated_by"] == "damon"
    d = json.loads((cfg / settings.NAME).read_text())
    assert "bogus" not in d and d["publish_enabled"] is False
    tok = settings.csrf_token()
    assert tok and settings.csrf_token() == tok               # stable per install
    assert settings.get("publish_enabled") is False           # token write keeps values


def test_metric_gauge(cfg):
    settings.set_values({"llm_enabled": False}, "damon")
    out = metrics.render()
    assert 'omi_setting_enabled{setting="llm_enabled"} 0' in out
    assert 'omi_setting_enabled{setting="publish_enabled"} 1' in out


def test_banner_rendered(cfg):
    page = review.render_page([], user="damon")
    assert 'action="/review/settings"' in page and settings.csrf_token() in page
    assert 'name="llm_enabled" value="false"' in page        # currently on -> button turns it off
    settings.set_values({"publish_enabled": False}, "damon")
    page = review.render_page([], user="damon")
    assert 'name="publish_enabled" value="true"' in page and "OFF" in page and "by damon" in page


def _post(url, body: str, headers: dict):
    req = urllib.request.Request(url, data=body.encode(), method="POST", headers={
        "content-type": "application/x-www-form-urlencoded", **headers})
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(req, timeout=5) as r:
            return r.status, dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


@pytest.fixture
def srv(cfg, tmp_path):
    from http.server import ThreadingHTTPServer
    archive = tmp_path / "archive"
    archive.mkdir()
    s = ThreadingHTTPServer(("127.0.0.1", 0), review.make_review_handler(cfg, archive, require_sso=True))
    threading.Thread(target=s.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{s.server_address[1]}", s.server_address[1]
    s.shutdown()


def test_settings_post(srv, cfg):
    base, port = srv
    url = base + "/review/settings"
    tok = settings.csrf_token()
    sso = {"x-authentik-username": "damon"}
    same = {**sso, "origin": f"http://127.0.0.1:{port}"}
    assert _post(url, f"csrf={tok}&llm_enabled=false", {"origin": f"http://127.0.0.1:{port}"})[0] == 403  # no SSO
    assert _post(url, "llm_enabled=false", same)[0] == 403                       # missing csrf
    assert _post(url, "csrf=nope&llm_enabled=false", same)[0] == 403             # bad csrf
    assert _post(url, f"csrf={tok}&llm_enabled=false", {**sso, "origin": "https://evil.example"})[0] == 403
    assert _post(url, f"csrf={tok}&llm_enabled=false", {**same, "sec-fetch-site": "cross-site"})[0] == 403
    assert _post(url, f"csrf={tok}&llm_enabled=false", sso)[0] == 403            # no origin/referer
    assert settings.get("llm_enabled") is True                                    # nothing changed yet
    # public hostname (behind authentik the Host header is the internal service)
    code, h = _post(url, f"csrf={tok}&llm_enabled=false", {**sso, "origin": "https://omi.k8s.inxaos.com"})
    assert code == 303 and h.get("Location", h.get("location")) == "/review"
    assert settings.get("llm_enabled") is False
    code, _ = _post(url, f"csrf={tok}&publish_enabled=false", {**sso, "referer": f"http://127.0.0.1:{port}/review"})
    assert code == 303 and settings.all_settings()["updated_by"] == "damon"
    assert json.loads((cfg / settings.NAME).read_text())["publish_enabled"] is False
    assert _post(url, f"csrf={tok}", same)[0] == 400                             # nothing to change
    assert _post(base + "/review/other", f"csrf={tok}&llm_enabled=true", same)[0] == 404


T0 = 1_762_990_000  # matches nothing in particular; only used for relative timing


def test_llm_off_holds_session(cfg, tmp_path, monkeypatch):
    from omi_pipeline import cli
    settings.set_values({"llm_enabled": False}, "damon")
    called = []
    monkeypatch.setattr(cli.P, "get_llm", lambda *a: called.append("llm"))
    monkeypatch.setattr(cli.P, "wiki_publish", lambda *a: called.append("wiki"))
    monkeypatch.setattr(cli, "update_index", lambda *a: called.append("index"))
    archive = tmp_path / "archive"
    archive.mkdir()
    sess = [{"name": "omi-omi-bridge-20261112-193032.ogg", "start": T0, "transcript": "hello there this is a test"}]
    args = SimpleNamespace(archive_dir=str(archive), dry_run=False, test=False, llm="x", llm_model="y")
    r = cli.publish_session(sess, "s1", args)
    assert r["status"] == "held" and "LLM" in r["note"] and called == []
    settings.set_values({"llm_enabled": True, "publish_enabled": False}, "damon")
    r = cli.publish_session(sess, "s1", args)
    assert r["status"] == "held" and "publishing paused" in r["note"] and called == []


def test_publish_on_triggers_index_rebuild(cfg, monkeypatch):
    from omi_pipeline import cli
    hits = []
    k = SimpleNamespace(request=lambda: hits.append(1))
    monkeypatch.setattr(cli, "_KEEPERS", {"journal/monologues": k})
    cli.on_settings_changed({"publish_enabled": False})
    assert hits == []
    cli.on_settings_changed({"publish_enabled": True})
    assert hits == [1]


def test_keeper_no_startup_rebuild():
    done = threading.Event()
    k = S.IndexKeeper("journal/monologues", "M", lister=lambda p: [],
                      writer=lambda *a: done.set() or {"action": "updated", "id": 1}).start(rebuild_now=False)
    assert not done.wait(0.3)
    k.request()
    assert done.wait(5)


def test_state_file_untouched(cfg):
    # settings file lives next to the watcher state but must never be treated as audio
    settings.set_values({"llm_enabled": False}, "damon")
    assert settings.NAME != STATE_NAME and settings.NAME.startswith(".")
