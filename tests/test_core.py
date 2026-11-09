import pytest

from omi_pipeline import core


def test_guard_servicetitan_strong():
    assert core.work_guard("Thinking about the ServiceTitan migration").is_work
    assert core.work_guard("service titan stuff").is_work


def test_guard_two_weak_hits():
    g = core.work_guard("after standup I need to update jira")
    assert g.is_work and len(g.hits) == 2


def test_guard_home_ok():
    g = core.work_guard("I need to fix the garage door and plan the garden. On call? no.")
    assert not g.is_work


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
                            '"action_items":["Buy mulch", " ", "Call Bob"],"tags":["Home Stuff"],"is_work":false}')
    assert a.action_items == ["Buy mulch", "Call Bob"]
    assert a.tags == ["home-stuff"] and not a.is_work


def test_render_page():
    a = core.Analysis("T", "clean", "sum", ["Do x"])
    md = core.render_page(a, "raw", "2026-11-09", "f.m4a")
    assert "- [ ] Do x" in md and "raw" in md and md.startswith("# T")
