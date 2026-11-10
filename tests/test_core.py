import pytest

from omi_pipeline import core


def test_slugify():
    assert core.slugify("Garden & Garage: Plans!") == "garden-garage-plans"
    assert core.slugify("!!!") == "monologue"
    assert len(core.slugify("a" * 200)) == 50


def test_extract_json_fenced_and_prose():
    assert core.extract_json('here:\n```json\n{"a": 1}\n```') == {"a": 1}
    assert core.extract_json('Sure! {"a": {"b": 2}} thanks') == {"a": {"b": 2}}
    with pytest.raises(ValueError):
        core.extract_json("nothing")


def test_parse_analysis():
    a = core.parse_analysis('{"title":"Weekend","cleaned":"x","summary":"s",'
                            '"action_items":["Buy mulch", " ", "Call Bob"],"tags":["Home Stuff"]}')
    assert a.action_items == ["Buy mulch", "Call Bob"]
    assert a.tags == ["home-stuff"]


def test_render_page():
    a = core.Analysis("T", "clean", "sum", ["Do x"])
    md = core.render_page(a, "raw", "2026-11-09", "f.m4a")
    assert "- [ ] Do x" in md and "raw" in md and md.startswith("# T")


def test_test_device_routing(monkeypatch):
    from types import SimpleNamespace
    from pathlib import Path
    from omi_pipeline import cli
    monkeypatch.delenv("OMI_WIKI_PREFIX", raising=False)
    monkeypatch.delenv("OMI_WIKI_TEST_PREFIX", raising=False)
    a = SimpleNamespace(test=False)
    assert cli.is_test(Path("omi-test-20261110.wav"), a)
    assert not cli.is_test(Path("omi-desk-20261110.ogg"), a)
    assert cli.is_test(Path("x.wav"), SimpleNamespace(test=True))
    assert cli.wiki_prefix(True) == "test/omi-monologues"
    assert cli.wiki_prefix(False) == "journal/monologues"
