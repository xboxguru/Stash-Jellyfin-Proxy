"""
Unit tests for triptych/shorts Live TV behavior (Feature 1 — see docs/Triptych.md).

Covers:
  - _is_shorts_channel: which channels get the 30-minute block schedule and
    block-based now-playing logic (legacy stash_type=="shorts", the per-channel
    `shorts` flag on any source type, and triptych+shorts combined)
  - _feed_one_vertical_round spawns the composite + audio subs (and the
    silence-filler fallback when the center has no audio), mirroring
    live_tv_engine._feed_one_scene's own safety net

The FFmpeg-spawning pieces mock `asyncio.create_subprocess_exec` (no real ffmpeg
or subprocess in tests), the same way the compositor's own tests do.
"""
import asyncio
from unittest.mock import AsyncMock

import pytest

import api.live_tv_data as ltd
import api.live_tv_engine as lte


# ── _is_shorts_channel ───────────────────────────────────────────────────────

class TestIsShortsChannel:
    """A channel plays as Shorts (30-min blocks + block now-playing) when it's the
    legacy dedicated shorts channel OR carries the per-channel `shorts` flag — the
    flag can ride on any source type and combines with triptych."""

    def test_legacy_stash_type_shorts(self):
        assert ltd._is_shorts_channel({"stash_type": "shorts"}) is True

    def test_per_channel_shorts_flag_on_tag(self):
        assert ltd._is_shorts_channel({"stash_type": "tag", "shorts": True}) is True

    def test_per_channel_shorts_flag_on_performer(self):
        assert ltd._is_shorts_channel({"stash_type": "performer", "shorts": True}) is True

    def test_triptych_plus_shorts_is_shorts(self):
        # triptych+shorts must still get the block schedule (playback compositing
        # is a separate concern handled by the triptych feeder)
        assert ltd._is_shorts_channel(
            {"stash_type": "tag", "shorts": True, "triptych": True}
        ) is True

    def test_plain_tag_channel_is_not_shorts(self):
        assert ltd._is_shorts_channel({"stash_type": "tag"}) is False

    def test_triptych_without_shorts_is_not_shorts(self):
        assert ltd._is_shorts_channel({"stash_type": "tag", "triptych": True}) is False

    def test_shorts_flag_false_is_not_shorts(self):
        assert ltd._is_shorts_channel({"stash_type": "filter", "shorts": False}) is False


class TestGetStashChannelsFlagPropagation:
    """_get_stash_channels() builds the runtime channel dicts that the schedule
    builder, scene fetch, and guide payload all consume — every per-channel flag
    in channels.json must survive the copy. (Regression: `shorts` and
    `shorts_max_minutes` were dropped here, so shorts channels got random
    schedules and no duration filter.)"""

    async def test_propagates_shorts_and_triptych_fields(self, monkeypatch):
        cfg = {
            "tvg_id": "ch_test1", "name": "Verts", "number": "5001",
            "stash_type": "filter", "source_ids": [],
            "triptych": True, "triptych_salt": "abc",
            "shorts": True, "shorts_max_minutes": 3,
            "order": 0,
        }
        monkeypatch.setattr(ltd, "_channels_config", [cfg])
        monkeypatch.setitem(ltd._stash_channels_cache, "data", None)
        monkeypatch.setitem(ltd._stash_channels_cache, "ts", 0.0)

        channels = await ltd._get_stash_channels()

        ch = next(c for c in channels if c["tvg_id"] == "ch_test1")
        assert ch["triptych"] is True
        assert ch["triptych_salt"] == "abc"
        assert ch["shorts"] is True
        assert ch["shorts_max_minutes"] == 3
        assert ltd._is_shorts_channel(ch) is True


# ── _feed_one_vertical_round ─────────────────────────────────────────────────

class _FakeStderr:
    async def readline(self):
        return b""


class _FakeProc:
    def __init__(self, rc=0, fast_exit=False, exit_delay=0.1):
        self.pid = 4242
        self.returncode = None
        self._rc = rc
        self._fast_exit = fast_exit
        self._exit_delay = exit_delay
        self.stderr = _FakeStderr()

    async def wait(self):
        # If fast_exit=True, exit immediately (for testing silence filler).
        # Otherwise, delay before exiting (simulates running process).
        if not self._fast_exit:
            # Delay longer than the 2s timeout so timeout triggers in silent-center check
            await asyncio.sleep(self._exit_delay)
        self.returncode = self._rc
        return self._rc

    def terminate(self):
        pass

    def is_closing(self):
        return False


class _FakeBackend:
    def sub_outputs(self):
        return ("vout", "aout")

    async def wait_master_audio_attached(self, timeout=15.0):
        return True


class TestFeedOneVerticalRound:
    async def test_success_spawns_composite_and_audio_subs(self, monkeypatch):
        mgr = lte._FFmpegChannelManager()
        proc_v = _FakeProc(rc=0, fast_exit=True)
        # exit_delay > 2s so the timeout in silent-center check triggers
        proc_a = _FakeProc(rc=0, fast_exit=False, exit_delay=2.5)
        create_mock = AsyncMock(side_effect=[proc_v, proc_a])
        monkeypatch.setattr(asyncio, "create_subprocess_exec", create_mock)

        ok = await mgr._feed_one_vertical_round("cid1", "100", ["101", "102"], _FakeBackend())

        assert ok is True
        assert create_mock.await_count == 2

    async def test_composite_spawn_failure_returns_false(self, monkeypatch):
        mgr = lte._FFmpegChannelManager()
        create_mock = AsyncMock(side_effect=OSError("boom"))
        monkeypatch.setattr(asyncio, "create_subprocess_exec", create_mock)

        ok = await mgr._feed_one_vertical_round("cid1", "100", ["101", "102"], _FakeBackend())

        assert ok is False

    async def test_audio_attach_timeout_kills_composite_and_returns_false(self, monkeypatch):
        mgr = lte._FFmpegChannelManager()
        proc_v = _FakeProc(rc=0)
        create_mock = AsyncMock(side_effect=[proc_v])
        monkeypatch.setattr(asyncio, "create_subprocess_exec", create_mock)

        class _BadBackend(_FakeBackend):
            async def wait_master_audio_attached(self, timeout=15.0):
                return False

        ok = await mgr._feed_one_vertical_round("cid1", "100", ["101", "102"], _BadBackend())

        assert ok is False
        assert create_mock.await_count == 1

    async def test_silent_center_spawns_silence_filler(self, monkeypatch):
        # Center audio sub exits fast with rc=0 (clean exit, no audio stream) — the
        # silent-center detector spawns a silence filler to prevent the master from
        # stalling. Mirrors live_tv_engine._feed_one_scene's safety net.
        mgr = lte._FFmpegChannelManager()
        proc_v = _FakeProc(rc=0, fast_exit=True)
        proc_a_silent = _FakeProc(rc=0, fast_exit=True)  # Fast exit = no audio stream
        proc_silence = _FakeProc(rc=0, fast_exit=True)
        create_mock = AsyncMock(side_effect=[proc_v, proc_a_silent, proc_silence])
        monkeypatch.setattr(asyncio, "create_subprocess_exec", create_mock)

        ok = await mgr._feed_one_vertical_round("cid1", "100", ["101", "102"], _FakeBackend())

        assert ok is True
