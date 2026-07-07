"""
Unit tests for the Vertical Multi-View compositor rework — full-length seek +
segment cache (api/vertical_engine.py + api/vertical_routes.py).

Covers:
  - synthetic VOD playlist correctness (segment count, uniform + final durations)
  - the segment range tracker (produced indexes, first gap, run head/coverage,
    range summary) and side phasing
  - ensure_segment trigger cases: cache hit, near-head wait, seek-past-head,
    back-seek gap, post-reap resume, cold launch — plus relaunch debounce
  - two-stage teardown (stage-1 reap keeps segments, stage-2 destroy frees the
    slot) and the fetch liveness reset
  - background backfill scheduling + preemption by a live run
  - session-id validation and VERTICAL_DEBUG level gating (unchanged behavior)
"""
import asyncio
import logging
import os
from unittest.mock import AsyncMock

import config
from api.vertical_engine import (
    _VerticalSessionManager, total_segments_for, SEG_DURATION, _FORWARD_WAIT_SEGMENTS,
    build_composite_cmd, build_audio_cmd,
)
from api.vertical_routes import _valid_session_id, _new_session_id, _build_vod_playlist
from core.vertical import vdebug


# ── helpers ────────────────────────────────────────────────────────────────────

def _touch_segs(d: str, indices) -> None:
    for i in indices:
        open(os.path.join(d, f"seg{i:05d}.ts"), "wb").close()


def _session(tmp_path, indices=(), *, center_dur=40.0, alive=False, start_index=0):
    """A manager with one session 's' backed by a real temp dir + seg files."""
    mgr = _VerticalSessionManager()
    d = str(tmp_path)
    mgr._dirs["s"] = d
    mgr._center_dur["s"] = center_dur
    mgr._launch_info["s"] = {"start_index": start_index}
    mgr.is_alive = lambda sid: alive  # type: ignore[assignment]
    _touch_segs(d, indices)
    return mgr, d


async def _quiesce(mgr) -> None:
    tasks = [mgr._watchdog, *list(mgr._backfill.values())]
    for t in tasks:
        if t is not None and not t.done():
            t.cancel()
            try:
                await t
            except asyncio.CancelledError:
                pass


# ── synthetic VOD playlist ──────────────────────────────────────────────────────

class TestSyntheticPlaylist:
    def test_segment_count_and_durations(self):
        # 10 s → 3 segments: 4.000, 4.000, 2.000 (final = true remainder).
        body = _build_vod_playlist("123-abcd1234", "http://p", 10.0)
        lines = body.splitlines()
        extinf = [l for l in lines if l.startswith("#EXTINF")]
        assert extinf == ["#EXTINF:4.000,", "#EXTINF:4.000,", "#EXTINF:2.000,"]
        segs = [l for l in lines if l.startswith("http")]
        assert segs[0].endswith("/vertical/123-abcd1234/seg/seg00000.ts")
        assert segs[-1].endswith("/seg/seg00002.ts")
        assert "#EXT-X-PLAYLIST-TYPE:VOD" in lines
        assert "#EXT-X-TARGETDURATION:4" in lines
        assert lines[-1] == "#EXT-X-ENDLIST"

    def test_exact_multiple_has_full_final_segment(self):
        body = _build_vod_playlist("1-a", "http://p", 8.0)
        extinf = [l for l in body.splitlines() if l.startswith("#EXTINF")]
        assert extinf == ["#EXTINF:4.000,", "#EXTINF:4.000,"]

    def test_short_clip_single_segment(self):
        body = _build_vod_playlist("1-a", "http://p", 2.5)
        extinf = [l for l in body.splitlines() if l.startswith("#EXTINF")]
        assert extinf == ["#EXTINF:2.500,"]

    def test_total_segments_for(self):
        assert total_segments_for(10.0) == 3
        assert total_segments_for(8.0) == 2
        assert total_segments_for(0) is None
        assert total_segments_for(None) is None


# ── segment range tracker ───────────────────────────────────────────────────────

class TestRangeTracker:
    def test_produced_indices(self, tmp_path):
        mgr, _ = _session(tmp_path, [0, 1, 2, 5])
        assert mgr._produced_indices("s") == {0, 1, 2, 5}

    def test_first_gap(self, tmp_path):
        mgr, _ = _session(tmp_path, [0, 1, 2, 5])
        assert mgr._first_gap("s") == 3

    def test_first_gap_none_when_full(self, tmp_path):
        mgr, _ = _session(tmp_path, range(10))  # 40 s / 4 = 10 segments
        assert mgr._first_gap("s") is None

    def test_first_gap_at_zero(self, tmp_path):
        mgr, _ = _session(tmp_path, [3, 4])
        assert mgr._first_gap("s") == 0

    def test_range_summary(self, tmp_path):
        mgr, _ = _session(tmp_path, [0, 1, 2, 5, 6])
        assert mgr._range_summary("s") == "0-2,5-6"

    def test_range_summary_empty(self, tmp_path):
        mgr, _ = _session(tmp_path, [])
        assert mgr._range_summary("s") == "[]"

    def test_run_head(self, tmp_path):
        mgr, _ = _session(tmp_path, [5, 6, 7], start_index=5)
        assert mgr._run_head("s", 5) == 8
        # Old segments below the run start don't move the run head.
        assert mgr._run_head("s", 9) == 9

    def test_run_covers(self, tmp_path):
        # Large clip so the forward-wait window (24) fits inside the timeline.
        mgr, _ = _session(tmp_path, [5, 6, 7], alive=True, start_index=5, center_dur=8000.0)
        head = mgr._run_head("s", 5)  # 8
        assert mgr._run_covers("s", head) is True                    # at the live edge
        assert mgr._run_covers("s", head + _FORWARD_WAIT_SEGMENTS) is True   # read-ahead
        assert mgr._run_covers("s", head + _FORWARD_WAIT_SEGMENTS + 1) is False  # far forward seek
        assert mgr._run_covers("s", 4) is False                      # before the run start

    def test_run_covers_false_when_dead(self, tmp_path):
        mgr, _ = _session(tmp_path, [5], alive=False, start_index=5)
        assert mgr._run_covers("s", 6) is False

    def test_phase(self):
        assert _VerticalSessionManager._phase(0, 10.0) == 0.0
        assert _VerticalSessionManager._phase(3, 10.0) == 2.0   # t=12 → 12 mod 10
        assert _VerticalSessionManager._phase(5, 0) == 0.0      # unknown side duration


# ── scene dedupe ────────────────────────────────────────────────────────────────

class TestSessionForScene:
    def test_none_when_no_session(self, tmp_path):
        mgr = _VerticalSessionManager()
        assert mgr.session_for_scene("35879") is None

    def test_finds_session_for_scene(self, tmp_path):
        mgr, _ = _session(tmp_path, [0], alive=True)
        mgr._center_id["s"] = "35879"
        assert mgr.session_for_scene("35879") == "s"
        assert mgr.session_for_scene("99999") is None

    def test_prefers_live_over_cached(self, tmp_path):
        mgr = _VerticalSessionManager()
        mgr._dirs["cached"] = str(tmp_path / "c"); mgr._center_id["cached"] = "35879"
        mgr._dirs["live"] = str(tmp_path / "l"); mgr._center_id["live"] = "35879"
        mgr.is_alive = lambda sid: sid == "live"  # type: ignore[assignment]
        assert mgr.session_for_scene("35879") == "live"

    def test_skips_stopped_session(self, tmp_path):
        mgr, _ = _session(tmp_path, [0], alive=True)
        mgr._center_id["s"] = "35879"
        mgr._stopped["s"] = True
        assert mgr.session_for_scene("35879") is None


# ── ensure_segment trigger cases + debounce ─────────────────────────────────────

class TestEnsureSegment:
    def _armed(self, mgr):
        mgr._await_segment = AsyncMock(return_value=True)
        mgr._relaunch = AsyncMock(return_value=True)
        mgr._launch = AsyncMock(return_value=True)
        return mgr

    async def test_cache_hit_no_recovery(self, tmp_path):
        mgr, _ = _session(tmp_path, [3], alive=True, start_index=0)
        self._armed(mgr)
        assert await mgr.ensure_segment("s", 3, {"id": "1"}) is True
        mgr._relaunch.assert_not_called()
        mgr._launch.assert_not_called()
        await _quiesce(mgr)

    async def test_near_head_waits_no_relaunch(self, tmp_path):
        mgr, _ = _session(tmp_path, [0, 1], alive=True, start_index=0)  # head=2
        self._armed(mgr)
        assert await mgr.ensure_segment("s", 3, {"id": "1"}) is True
        mgr._relaunch.assert_not_called()
        mgr._await_segment.assert_awaited()
        await _quiesce(mgr)

    async def test_seek_past_head_relaunches(self, tmp_path):
        # Large clip so a genuinely-far forward seek (well past head + the
        # read-ahead window) exists; read-ahead within the window would only wait.
        mgr, _ = _session(tmp_path, [0, 1], alive=True, start_index=0, center_dur=8000.0)  # head=2
        self._armed(mgr)
        far = 2 + _FORWARD_WAIT_SEGMENTS + 20
        assert await mgr.ensure_segment("s", far, {"id": "1"}) is True
        mgr._relaunch.assert_awaited_once_with("s", far)
        await _quiesce(mgr)

    async def test_forward_readahead_does_not_relaunch(self, tmp_path):
        # A read-ahead request within the forward window must WAIT, not relaunch —
        # this is the client-buffering case that previously caused a relaunch storm.
        mgr, _ = _session(tmp_path, [0, 1], alive=True, start_index=0, center_dur=8000.0)  # head=2
        self._armed(mgr)
        assert await mgr.ensure_segment("s", 2 + _FORWARD_WAIT_SEGMENTS, {"id": "1"}) is True
        mgr._relaunch.assert_not_called()
        mgr._await_segment.assert_awaited()
        await _quiesce(mgr)

    async def test_back_seek_gap_relaunches(self, tmp_path):
        mgr, _ = _session(tmp_path, [5, 6, 7], alive=True, start_index=5)  # request below start
        self._armed(mgr)
        assert await mgr.ensure_segment("s", 2, {"id": "1"}) is True
        mgr._relaunch.assert_awaited_once_with("s", 2)
        await _quiesce(mgr)

    async def test_post_reap_resume_relaunches(self, tmp_path):
        mgr, _ = _session(tmp_path, [0, 1, 2], alive=False, start_index=0)  # dead encoder
        self._armed(mgr)
        assert await mgr.ensure_segment("s", 3, {"id": "1"}) is True
        mgr._relaunch.assert_awaited_once_with("s", 3)
        await _quiesce(mgr)

    async def test_cold_session_launches_at_index(self, tmp_path):
        mgr = _VerticalSessionManager()
        mgr.is_alive = lambda sid: False
        self._armed(mgr)
        scene = {"id": "1", "files": [{"duration": 40.0}]}
        assert await mgr.ensure_segment("s", 4, scene) is True
        mgr._launch.assert_awaited_once()
        assert mgr._launch.call_args.args[2] == 4
        await _quiesce(mgr)

    async def test_cold_session_without_scene_fails(self, tmp_path):
        mgr = _VerticalSessionManager()
        self._armed(mgr)
        assert await mgr.ensure_segment("s", 4, None) is False
        mgr._launch.assert_not_called()
        await _quiesce(mgr)

    async def test_debounce_no_second_relaunch_when_covered(self, tmp_path):
        # A relaunch to a far index makes subsequent requests within that run's
        # reach cache-hit or wait — never stacking a second relaunch.
        mgr, d = _session(tmp_path, [], alive=True, start_index=0, center_dur=8000.0)
        mgr._await_segment = AsyncMock(return_value=True)
        calls = []

        async def fake_relaunch(sid, idx):
            calls.append(idx)
            mgr._launch_info[sid]["start_index"] = idx
            _touch_segs(d, [idx])  # run produces the sought segment
            return True

        mgr._relaunch = fake_relaunch
        far = _FORWARD_WAIT_SEGMENTS + 50
        assert await mgr.ensure_segment("s", far, {"id": "1"}) is True      # far → relaunch
        assert await mgr.ensure_segment("s", far, {"id": "1"}) is True      # now cached
        assert await mgr.ensure_segment("s", far + 1, {"id": "1"}) is True  # within run reach
        assert calls == [far]
        await _quiesce(mgr)


# ── two-stage teardown ──────────────────────────────────────────────────────────

class TestTwoStageTeardown:
    async def test_stage1_reap_keeps_segments_when_finished(self, tmp_path):
        mgr, d = _session(tmp_path, [0, 1], alive=False)  # encode already done
        mgr._stopped["s"] = False
        mgr._reaped["s"] = False
        async with mgr._lock:
            await mgr._reap("s", reason="idle")
        assert mgr._reaped["s"] is True
        assert "s" in mgr._dirs                       # slot still held
        assert os.path.exists(os.path.join(d, "seg00000.ts"))  # segments preserved
        # Backfill is skipped once reaped.
        mgr._schedule_backfill("s")
        assert mgr._backfill.get("s") is None
        await _quiesce(mgr)

    async def test_stage2_destroy_frees_slot_and_deletes_dir(self, tmp_path):
        d = str(tmp_path / "sess")
        os.makedirs(d)
        _touch_segs(d, [0])
        mgr = _VerticalSessionManager()
        mgr._dirs["s"] = d
        mgr._center_dur["s"] = 8.0
        mgr._stopped["s"] = False
        async with mgr._lock:
            await mgr._destroy("s", reason="explicit stop")
        assert "s" not in mgr._dirs
        assert mgr.active_count() == 0
        assert not os.path.exists(d)
        await _quiesce(mgr)

    async def test_stage1_then_stage2_sequence(self, tmp_path):
        mgr, d = _session(tmp_path, [0], alive=False)
        mgr._stopped["s"] = False
        mgr._reaped["s"] = False
        async with mgr._lock:
            await mgr._reap("s", reason="idle")
        assert "s" in mgr._dirs
        async with mgr._lock:
            await mgr._destroy("s", reason="ttl")
        assert "s" not in mgr._dirs
        await _quiesce(mgr)

    async def test_touch_resets_liveness_clock(self, tmp_path):
        mgr, _ = _session(tmp_path, [0])
        mgr.touch("s")
        first = mgr._last["s"]
        await asyncio.sleep(0.01)
        mgr.touch("s")
        assert mgr._last["s"] >= first
        await _quiesce(mgr)


# ── background backfill ─────────────────────────────────────────────────────────

class TestBackfill:
    def _fillable(self, tmp_path, indices, *, alive):
        mgr, d = _session(tmp_path, indices, alive=alive, center_dur=40.0)
        mgr._stopped["s"] = False
        mgr._reaped["s"] = False
        return mgr, d

    async def test_backfill_fills_first_gap(self, tmp_path):
        mgr, _ = self._fillable(tmp_path, [0, 1], alive=False)
        calls = []

        async def fake_start(sid, idx, min_ready):
            calls.append(idx)
            return True

        mgr._start_run = fake_start
        mgr._schedule_backfill("s")
        await mgr._backfill["s"]
        assert calls == [2]  # earliest gap
        await _quiesce(mgr)

    async def test_backfill_yields_to_live_run(self, tmp_path):
        mgr, _ = self._fillable(tmp_path, [0, 1], alive=True)  # a run is active
        mgr._start_run = AsyncMock(return_value=True)
        mgr._schedule_backfill("s")
        await mgr._backfill["s"]
        mgr._start_run.assert_not_called()  # real requests win; backfill defers
        await _quiesce(mgr)

    async def test_backfill_skipped_when_stopped(self, tmp_path):
        mgr, _ = self._fillable(tmp_path, [0, 1], alive=False)
        mgr._stopped["s"] = True
        mgr._schedule_backfill("s")
        assert mgr._backfill.get("s") is None
        await _quiesce(mgr)

    async def test_backfill_stall_guard(self, tmp_path):
        mgr, _ = self._fillable(tmp_path, [0, 1], alive=False)
        mgr._backfill_last["s"] = 2  # a prior run at this gap made no progress
        calls = []

        async def fake_start(sid, idx, min_ready):
            calls.append(idx)
            return True

        mgr._start_run = fake_start
        mgr._schedule_backfill("s")
        await mgr._backfill["s"]
        assert calls == []  # doesn't loop on the same stuck gap
        await _quiesce(mgr)


# ── command pacing invariant (Live TV keeps -re; VOD does not) ──────────────────

class TestCommandPacing:
    def test_channel_default_keeps_re(self):
        # The Vertical TV channel calls the builder with defaults → -re on all 3 inputs.
        cmd = build_composite_cmd("ff", "1", "2", "3", 0.0, "out")
        assert cmd.count("-re") == 3
        assert "-readrate" not in cmd

    def test_audio_channel_default_keeps_re(self):
        cmd = build_audio_cmd("ff", "2", 0.0, "out")
        assert "-re" in cmd

    def test_vod_unlimited_drops_re(self, monkeypatch):
        monkeypatch.setattr(config, "VERTICAL_READRATE", 0.0)
        pace = _VerticalSessionManager._pace_args()
        assert pace == ()
        cmd = build_composite_cmd("ff", "1", "2", "3", 0.0, "out", pace_args=pace)
        assert "-re" not in cmd and "-readrate" not in cmd

    def test_vod_readrate_caps_burst(self, monkeypatch):
        monkeypatch.setattr(config, "VERTICAL_READRATE", 1.5)
        pace = _VerticalSessionManager._pace_args()
        assert pace == ("-readrate", "1.5")
        cmd = build_composite_cmd("ff", "1", "2", "3", 0.0, "out", pace_args=pace)
        assert "-readrate" in cmd and "1.5" in cmd and "-re" not in cmd

    def test_side_phasing_applies_input_seeks(self):
        cmd = build_composite_cmd("ff", "1", "2", "3", 40.0, "out",
                                  left_seek=2.0, right_seek=3.0, pace_args=())
        joined = " ".join(cmd)
        assert "-ss 40.000" in joined   # center seek
        assert "-ss 2.000" in joined    # left side phase
        assert "-ss 3.000" in joined    # right side phase


# ── session-id validation + vdebug gating (unchanged) ───────────────────────────

class TestSessionIdValidation:
    def test_minted_ids_validate(self):
        assert _valid_session_id(_new_session_id("123")) is True

    def test_garbage_ids_rejected(self):
        for bad in ("", "123", "123-XYZ", "123-abcd", "../etc-deadbeef",
                    "123-deadbeef/../x", "abc-deadbeef", "123-deadbeef00"):
            assert _valid_session_id(bad) is False, bad


class TestVdebugGating:
    def test_flag_on_logs_at_info(self, monkeypatch, caplog):
        monkeypatch.setattr(config, "VERTICAL_DEBUG", True)
        logger = logging.getLogger("test.vdebug")
        with caplog.at_level(logging.INFO, logger="test.vdebug"):
            vdebug(logger, "hello")
        assert any(r.levelno == logging.INFO and r.message == "hello" for r in caplog.records)

    def test_flag_off_logs_at_debug(self, monkeypatch, caplog):
        monkeypatch.setattr(config, "VERTICAL_DEBUG", False)
        logger = logging.getLogger("test.vdebug")
        with caplog.at_level(logging.DEBUG, logger="test.vdebug"):
            vdebug(logger, "quiet")
        assert any(r.levelno == logging.DEBUG and r.message == "quiet" for r in caplog.records)
