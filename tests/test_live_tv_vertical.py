"""
Unit tests for the Vertical TV Live TV channel (Feature 1, Phase 2 — see
docs/Triptych.md § Vertical TV).

Mirrors the existing Live TV / Vertical Multi-View test style: config-driven
gating is tested directly, and the FFmpeg-spawning pieces mock
`asyncio.create_subprocess_exec` (no real ffmpeg or subprocess in tests) the
same way the compositor's own docs describe for `test_stream_routes.py`.

Covers:
  - _vertical_tv_enabled / _live_tv_enabled gating (all three flags required)
  - _get_stash_channels appends the synthetic vertical_tv channel only when
    fully enabled, and never persists it to channels.json
  - _build_stash_channel_playlist short-circuits for vertical_tv (no schedule)
  - _FFmpegChannelManager._feeder dispatches vertical_tv channels to
    _feeder_vertical instead of the scheduled-scene path
  - _feeder_vertical picks a fresh center/sides round and tracks "now playing"
  - _feed_one_vertical_round spawns the composite + audio subs (and the
    silence-filler fallback when the center has no audio), mirroring
    live_tv_engine._feed_one_scene's own safety net
"""
import asyncio
from unittest.mock import AsyncMock

import pytest

import config
import api.live_tv_data as ltd
import api.live_tv_engine as lte


# ── Gating ───────────────────────────────────────────────────────────────────

class TestVerticalTvEnabledGating:
    def test_requires_all_three_flags(self, monkeypatch):
        monkeypatch.setattr(config, "ENABLE_STASH_CHANNELS", True)
        monkeypatch.setattr(config, "ENABLE_VERTICAL_TV_CHANNEL", True)
        monkeypatch.setattr(config, "ENABLE_VERTICAL_MULTI", True)
        assert ltd._vertical_tv_enabled() is True

    @pytest.mark.parametrize("missing", [
        "ENABLE_STASH_CHANNELS", "ENABLE_VERTICAL_TV_CHANNEL", "ENABLE_VERTICAL_MULTI",
    ])
    def test_any_missing_flag_disables_it(self, monkeypatch, missing):
        flags = {
            "ENABLE_STASH_CHANNELS": True,
            "ENABLE_VERTICAL_TV_CHANNEL": True,
            "ENABLE_VERTICAL_MULTI": True,
        }
        flags[missing] = False
        for k, v in flags.items():
            monkeypatch.setattr(config, k, v)
        assert ltd._vertical_tv_enabled() is False

    def test_live_tv_enabled_true_via_vertical_tv_alone(self, monkeypatch):
        # Neither Tunarr nor plain Stash channels enabled — Vertical TV alone
        # should still flip the master _live_tv_enabled() switch on.
        monkeypatch.setattr(config, "ENABLE_LIVE_TV", True)
        monkeypatch.setattr(config, "ENABLE_TUNARR", False)
        monkeypatch.setattr(config, "ENABLE_STASH_CHANNELS", True)
        monkeypatch.setattr(config, "ENABLE_VERTICAL_TV_CHANNEL", True)
        monkeypatch.setattr(config, "ENABLE_VERTICAL_MULTI", True)
        assert ltd._live_tv_enabled() is True

    def test_live_tv_master_switch_still_gates_it(self, monkeypatch):
        monkeypatch.setattr(config, "ENABLE_LIVE_TV", False)
        monkeypatch.setattr(config, "ENABLE_STASH_CHANNELS", True)
        monkeypatch.setattr(config, "ENABLE_VERTICAL_TV_CHANNEL", True)
        monkeypatch.setattr(config, "ENABLE_VERTICAL_MULTI", True)
        assert ltd._live_tv_enabled() is False


# ── Channel list assembly ────────────────────────────────────────────────────

class TestGetStashChannelsVerticalTv:
    async def _get_channels(self, monkeypatch):
        ltd._channels_config.clear()
        monkeypatch.setattr(ltd, "_migrate_from_legacy_config", AsyncMock(return_value=[]))
        ltd._stash_channels_cache["data"] = None
        ltd._stash_channels_cache["ts"] = 0.0
        return await ltd._get_stash_channels()

    async def test_appends_vertical_tv_channel_when_enabled(self, monkeypatch):
        monkeypatch.setattr(config, "ENABLE_STASH_CHANNELS", True)
        monkeypatch.setattr(config, "ENABLE_VERTICAL_TV_CHANNEL", True)
        monkeypatch.setattr(config, "ENABLE_VERTICAL_MULTI", True)
        monkeypatch.setattr(config, "VERTICAL_TV_CHANNEL_NUMBER", 9500)
        channels = await self._get_channels(monkeypatch)
        vtv = [c for c in channels if c.get("stash_type") == "vertical_tv"]
        assert len(vtv) == 1
        assert vtv[0]["tvg_id"] == "vertical_tv"
        assert vtv[0]["number"] == "9500"

    async def test_absent_when_vertical_tv_disabled(self, monkeypatch):
        monkeypatch.setattr(config, "ENABLE_STASH_CHANNELS", True)
        monkeypatch.setattr(config, "ENABLE_VERTICAL_TV_CHANNEL", False)
        monkeypatch.setattr(config, "ENABLE_VERTICAL_MULTI", True)
        channels = await self._get_channels(monkeypatch)
        assert not any(c.get("stash_type") == "vertical_tv" for c in channels)

    async def test_absent_when_vertical_multi_disabled(self, monkeypatch):
        monkeypatch.setattr(config, "ENABLE_STASH_CHANNELS", True)
        monkeypatch.setattr(config, "ENABLE_VERTICAL_TV_CHANNEL", True)
        monkeypatch.setattr(config, "ENABLE_VERTICAL_MULTI", False)
        channels = await self._get_channels(monkeypatch)
        assert not any(c.get("stash_type") == "vertical_tv" for c in channels)

    async def test_not_persisted_to_channels_config(self, monkeypatch):
        # The synthetic channel must never land in _channels_config (channels.json) —
        # it has no lineup for the CRUD/schedule editor to manage.
        monkeypatch.setattr(config, "ENABLE_STASH_CHANNELS", True)
        monkeypatch.setattr(config, "ENABLE_VERTICAL_TV_CHANNEL", True)
        monkeypatch.setattr(config, "ENABLE_VERTICAL_MULTI", True)
        await self._get_channels(monkeypatch)
        assert not any(c.get("stash_type") == "vertical_tv" for c in ltd._channels_config)


# ── Playlist / schedule short-circuit ───────────────────────────────────────

class TestBuildStashChannelPlaylistVerticalTv:
    async def test_returns_empty_playlist_and_zero_seek(self):
        ch = {"tvg_id": "vertical_tv", "stash_type": "vertical_tv", "name": "Vertical TV"}
        result = await ltd._build_stash_channel_playlist(ch)
        assert result == ([], 0.0)


# ── Feeder dispatch ──────────────────────────────────────────────────────────

class TestFeederDispatch:
    async def test_vertical_tv_channel_routes_to_feeder_vertical(self, monkeypatch):
        mgr = lte._FFmpegChannelManager()
        mock_feeder_vertical = AsyncMock()
        monkeypatch.setattr(mgr, "_feeder_vertical", mock_feeder_vertical)
        ch = {"tvg_id": "vertical_tv", "stash_type": "vertical_tv"}
        backend = object()
        await mgr._feeder("cid1", ch, backend, 0.0)
        mock_feeder_vertical.assert_awaited_once_with("cid1", backend)


class TestFeederVertical:
    async def test_picks_round_and_tracks_now_playing(self, monkeypatch):
        mgr = lte._FFmpegChannelManager()
        # Set up a mock master process so health-check passes
        master_proc = _FakeProc(rc=None)
        mgr._procs["cid1"] = master_proc

        center_scene = {"id": "100", "title": "Center", "files": [{"duration": 42.0}]}
        picked = (center_scene, ["101", "102"])
        pick_mock = AsyncMock(side_effect=[picked, asyncio.CancelledError()])
        monkeypatch.setattr("core.vertical_selection.pick_center_and_sides", pick_mock)
        feed_mock = AsyncMock(return_value=True)
        monkeypatch.setattr(mgr, "_feed_one_vertical_round", feed_mock)

        with pytest.raises(asyncio.CancelledError):
            await mgr._feeder_vertical("cid1", backend=object())

        feed_mock.assert_awaited_once()
        args = feed_mock.await_args.args
        assert args[0] == "cid1"
        assert args[1] == "100"
        assert args[2] == ["101", "102"]
        assert mgr._current_scene["cid1"]["scene_id"] == "100"
        assert mgr._current_scene["cid1"]["duration_sec"] == 42.0

    async def test_retries_when_library_has_no_candidates(self, monkeypatch):
        mgr = lte._FFmpegChannelManager()
        # Set up a mock master process so health-check passes
        master_proc = _FakeProc(rc=None)
        mgr._procs["cid1"] = master_proc

        pick_mock = AsyncMock(side_effect=[None, asyncio.CancelledError()])
        monkeypatch.setattr("core.vertical_selection.pick_center_and_sides", pick_mock)
        sleep_mock = AsyncMock()
        monkeypatch.setattr(asyncio, "sleep", sleep_mock)

        with pytest.raises(asyncio.CancelledError):
            await mgr._feeder_vertical("cid1", backend=object())

        sleep_mock.assert_awaited()
        assert "cid1" not in mgr._current_scene


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
        assert create_mock.await_count == 3
