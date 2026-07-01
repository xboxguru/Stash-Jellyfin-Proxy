"""Tests for api/handy_devices.py — the multi-device registry (Feature 3 §9a).

Isolated from disk: LOG_DIR is pointed at a tmp dir per test, so load/save never touch real config.
"""
import pytest

import config
from api import handy_devices


@pytest.fixture(autouse=True)
def _isolate(tmp_path):
    orig_log_dir = getattr(config, "LOG_DIR", None)
    config.LOG_DIR = str(tmp_path)
    handy_devices._devices.clear()
    handy_devices._loaded = False
    yield
    handy_devices._devices.clear()
    handy_devices._loaded = False
    if orig_log_dir is not None:
        config.LOG_DIR = orig_log_dir


class TestCrud:
    def test_add_fills_defaults(self):
        d = handy_devices.add_device({"key": "abc", "label": "Living Room"})
        assert d["key"] == "abc"
        assert d["label"] == "Living Room"
        assert d["sync_mode"] == "local"     # default is HSP
        assert d["enabled"] is True
        assert d["source"] == "manual"
        assert d["funscript_offset"] is None
        assert len(d["id"]) == 12

    def test_add_defaults_label_when_blank(self):
        assert handy_devices.add_device({"key": "k"})["label"] == "Handy"

    def test_list_is_ordered(self):
        handy_devices.add_device({"key": "a"})
        handy_devices.add_device({"key": "b"})
        assert [d["key"] for d in handy_devices.list_devices()] == ["a", "b"]

    def test_update_coerces_and_persists(self):
        d = handy_devices.add_device({"key": "k"})
        handy_devices.update_device(d["id"], {"sync_mode": "HOSTED", "funscript_offset": "250",
                                              "enabled": False, "label": "  X "})
        got = handy_devices.get_device(d["id"])
        assert got["sync_mode"] == "hosted"
        assert got["funscript_offset"] == 250
        assert got["enabled"] is False
        assert got["label"] == "X"

    def test_update_missing_returns_none(self):
        assert handy_devices.update_device("nope", {"label": "x"}) is None

    def test_invalid_sync_mode_falls_back_to_default(self):
        d = handy_devices.add_device({"key": "k", "sync_mode": "bogus"})
        assert d["sync_mode"] == "local"

    def test_blank_int_knob_is_none(self):
        d = handy_devices.add_device({"key": "k", "hsp_buffer_min_s": "", "funscript_offset": "null"})
        assert d["hsp_buffer_min_s"] is None
        assert d["funscript_offset"] is None

    def test_delete_reindexes_order(self):
        a = handy_devices.add_device({"key": "a"})
        handy_devices.add_device({"key": "b"})
        assert handy_devices.delete_device(a["id"]) is True
        remaining = handy_devices.list_devices()
        assert [d["key"] for d in remaining] == ["b"]
        assert remaining[0]["order"] == 0

    def test_delete_missing_returns_false(self):
        assert handy_devices.delete_device("nope") is False


class TestEnabledDevices:
    def test_filters_disabled_and_keyless(self):
        handy_devices.add_device({"key": "a", "enabled": True})
        handy_devices.add_device({"key": "b", "enabled": False})
        handy_devices.add_device({"key": "", "enabled": True})   # no key -> skipped
        assert [d["key"] for d in handy_devices.enabled_devices()] == ["a"]


class TestStashSeed:
    def test_seeds_when_new(self):
        added = handy_devices.ensure_stash_seed("stashkey")
        assert added is not None
        assert added["source"] == "stash"
        assert added["key"] == "stashkey"
        assert added["sync_mode"] == "local"

    def test_noop_when_key_already_present(self):
        handy_devices.add_device({"key": "dupe"})
        assert handy_devices.ensure_stash_seed("dupe") is None
        assert len(handy_devices.list_devices()) == 1

    def test_noop_when_key_blank(self):
        assert handy_devices.ensure_stash_seed("") is None
        assert handy_devices.ensure_stash_seed(None) is None
        assert handy_devices.list_devices() == []

    def test_changed_stash_key_adds_second_device(self):
        handy_devices.ensure_stash_seed("old")
        handy_devices.ensure_stash_seed("new")   # key changed -> add, never delete the old
        keys = [d["key"] for d in handy_devices.list_devices()]
        assert keys == ["old", "new"]


class TestPersistence:
    def test_save_then_load_roundtrip(self):
        handy_devices.add_device({"key": "a", "label": "A"})
        handy_devices.add_device({"key": "b", "label": "B"})
        handy_devices._devices.clear()
        handy_devices._loaded = False
        handy_devices.load_devices()
        assert [d["label"] for d in handy_devices.list_devices()] == ["A", "B"]
