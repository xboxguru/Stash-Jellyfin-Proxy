"""
Tests for api/handy_controller.py (Feature 3 — Interactive Toy / Handy Sync).

NO LIVE NETWORK: every Handy/handyfeeling/Stash call is mocked. The controller's HTTP
primitives (_api_get/_api_put/_upload_csv/_fetch_funscript) and stash_client are patched, so
this suite makes zero real HTTP requests.

Covers:
  - funscript_to_csv conversion (rows, inverted, clamping, CRLF, bad rows)
  - event -> command mapping (play on start, pause/resume, seek inference, steady-state no-op)
  - offset application in setHsspPlay (estimatedServerTimeOffset + scriptOffset)
  - activation success / failure (no key, device not connected) — graceful, no exceptions escape
  - fan-out gating (disabled, non-interactive) + isolation (exceptions never propagate)
  - LAN/direct funscript-serving endpoint (JSON default, ?format=csv, disabled, unavailable)
"""
import asyncio
import json
import time

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from starlette.datastructures import QueryParams

import config
from api import handy_controller, handy_devices
from api.handy_controller import HandyController, HandySessionGroup, funscript_to_csv, funscript_to_points


def make_interactive_scene(scene_id="751", funscript_url="http://stash:9999/scene/751/funscript", interactive=True):
    return {"id": scene_id, "interactive": interactive, "paths": {"funscript": funscript_url}}


@pytest.fixture(autouse=True)
def _clean_registry():
    handy_controller._groups.clear()
    handy_controller._script_url_cache.clear()
    handy_controller._start_pos_by_session.clear()
    handy_devices._devices.clear()
    handy_devices._loaded = True   # treat as loaded/empty; don't touch disk during tests
    config.HANDY_SYNC_MODE = "auto"
    config.HANDY_APPLICATION_ID = ""   # default to v2 unless a test opts into v3
    config.HANDY_HSP_BUFFER_MIN_S = 30
    config.HANDY_HSP_BUFFER_MAX_S = 60
    config.HANDY_HSP_POLL_INTERVAL_S = 15
    orig_debounce = handy_controller.SEEK_DEBOUNCE_S
    handy_controller.SEEK_DEBOUNCE_S = 0.01  # keep debounce tests fast
    yield
    handy_controller.SEEK_DEBOUNCE_S = orig_debounce
    handy_controller._groups.clear()
    handy_controller._script_url_cache.clear()
    handy_devices._devices.clear()


async def _flush_play(c):
    """Await the controller's pending debounced play task, if any."""
    if c._pending_play_task is not None:
        await c._pending_play_task


def _ready_controller(session_id="sess-1"):
    c = HandyController(session_id, make_interactive_scene())
    c.state = "ready"
    c.estimated_offset_ms = 0.0
    c.script_offset_ms = 0
    c._play = AsyncMock()
    c._stop = AsyncMock()
    return c


# ── funscript_to_csv ──────────────────────────────────────────────────────────

class TestFunscriptToCsv:
    def test_basic_rows_crlf(self):
        csv = funscript_to_csv({"actions": [{"at": 0, "pos": 0}, {"at": 100, "pos": 100}]})
        assert csv == "0,0\r\n100,100\r\n"

    def test_inverted_flips_position(self):
        assert funscript_to_csv({"actions": [{"at": 0, "pos": 20}], "inverted": True}) == "0,80\r\n"

    def test_clamps_out_of_range(self):
        csv = funscript_to_csv({"actions": [{"at": 0, "pos": 250}, {"at": 1, "pos": -5}]})
        assert csv == "0,100\r\n1,0\r\n"

    def test_empty_actions(self):
        assert funscript_to_csv({"actions": []}) == "\r\n"

    def test_skips_malformed_rows(self):
        csv = funscript_to_csv({"actions": [{"at": None, "pos": 5}, {"at": 10, "pos": 50}]})
        assert csv == "10,50\r\n"


# ── event -> command mapping (on_progress) ────────────────────────────────────

class TestEventMapping:
    async def test_plays_when_not_yet_playing(self):
        c = _ready_controller()
        c._is_playing = False
        await c.on_progress(10.0, is_paused=False)
        await _flush_play(c)
        c._play.assert_awaited_once_with(10.0)
        c._stop.assert_not_awaited()

    async def test_pause_stops(self):
        c = _ready_controller()
        c._is_playing = True
        await c.on_progress(10.0, is_paused=True)
        c._stop.assert_awaited_once()
        c._play.assert_not_awaited()

    async def test_pause_while_already_stopped_is_noop(self):
        c = _ready_controller()
        c._is_playing = False
        await c.on_progress(10.0, is_paused=True)
        c._stop.assert_not_awaited()
        c._play.assert_not_awaited()

    async def test_seek_while_playing_replays(self):
        c = _ready_controller()
        c._is_playing = True
        c._last_position_s = 10.0
        c._last_event_t = time.monotonic()
        await c.on_progress(400.0, is_paused=False)  # jump 390 s with ~0 wall delta
        await _flush_play(c)
        c._play.assert_awaited_once_with(400.0)

    async def test_scrub_coalesces_to_single_play(self):
        c = _ready_controller()
        c._is_playing = True
        c._last_position_s = 10.0
        c._last_event_t = time.monotonic()
        # Three rapid seeks; debounce should collapse them to one play at the final position.
        await c.on_progress(100.0, is_paused=False)
        await c.on_progress(200.0, is_paused=False)
        await c.on_progress(300.0, is_paused=False)
        await _flush_play(c)
        c._play.assert_awaited_once_with(300.0)

    async def test_steady_state_progress_no_replay(self):
        c = _ready_controller()
        c._is_playing = True
        c._last_position_s = 10.0
        c._last_event_t = time.monotonic() - 5.0  # 5 s ago
        await c.on_progress(15.0, is_paused=False)  # advanced ~5 s over ~5 s wall
        await _flush_play(c)
        c._play.assert_not_awaited()

    async def test_ignores_events_when_not_ready(self):
        c = _ready_controller()
        c.state = "failed"
        c._is_playing = False
        await c.on_progress(10.0, is_paused=False)
        c._play.assert_not_awaited()

    async def test_pause_then_resume_toggles_playing_via_api(self):
        c = HandyController("s", make_interactive_scene())
        c.state = "ready"
        c._is_playing = True
        c._api_put = AsyncMock()
        await c.on_progress(10.0, is_paused=True)
        assert c._is_playing is False
        await c.on_progress(10.0, is_paused=False)
        await _flush_play(c)
        assert c._is_playing is True
        assert c._api_put.await_count == 2  # stop then play


# ── setHsspPlay offset application ────────────────────────────────────────────

class TestPlayOffsets:
    async def test_play_applies_script_and_server_offsets(self):
        c = HandyController("s", make_interactive_scene())
        c.estimated_offset_ms = 1000.0
        c.script_offset_ms = 400
        c._api_put = AsyncMock()
        with patch("api.handy_controller._now_ms", return_value=5000.0):
            await c._play(10.0)
        c._api_put.assert_awaited_once()
        path, body = c._api_put.await_args[0]
        assert path == "hssp/play"
        assert body["startTime"] == 10 * 1000 + 400          # positionSeconds*1000 + scriptOffset
        assert body["serverTime"] == round(1000.0 + 5000.0)  # estimatedServerTimeOffset + now
        assert c._is_playing is True


# ── position extrapolation (report→issue lag compensation) ────────────────────

class TestPositionExtrapolation:
    async def test_play_extrapolates_stale_position(self):
        c = HandyController("s", make_interactive_scene())  # v2 (no app id)
        c.estimated_offset_ms = 0.0
        c.script_offset_ms = 0
        c._api_put = AsyncMock()
        c._last_event_t = time.monotonic() - 0.4  # position was sampled ~0.4 s ago
        await c._play(10.0)
        _, body = c._api_put.await_args[0]
        assert 10350 <= body["startTime"] <= 10600  # advanced ~400 ms (slack for test timing)

    async def test_play_no_extrapolation_without_anchor(self):
        c = HandyController("s", make_interactive_scene())
        c.estimated_offset_ms = 0.0
        c.script_offset_ms = 0
        c._api_put = AsyncMock()
        assert c._last_event_t is None
        await c._play(10.0)
        _, body = c._api_put.await_args[0]
        assert body["startTime"] == 10000  # unchanged

    def test_extrapolation_is_capped(self):
        c = HandyController("s", make_interactive_scene())
        c._last_event_t = time.monotonic() - 100  # absurdly stale
        assert c._extrapolated_pos(5.0) == 5.0 + handy_controller.MAX_EXTRAPOLATION_S

    async def test_hsp_play_receives_extrapolated_pos(self):
        c = _hsp_controller()
        c._hsp_play = AsyncMock()
        c._last_event_t = time.monotonic() - 0.4
        await c._play(10.0)
        assert 10.35 <= c._hsp_play.await_args[0][0] <= 10.6


# ── activation ────────────────────────────────────────────────────────────────

class TestActivation:
    @pytest.fixture(autouse=True)
    def _stub_upload(self):
        # HSSP activation calls the module-level _prepare_upload_url; stub it to a hosted URL.
        with patch("api.handy_controller._prepare_upload_url", new=AsyncMock(return_value="http://script.csv")):
            yield

    def _patch_handy_cfg(self, **over):
        cfg = {"handyKey": "kJVRef7g", "funscriptOffset": 0, "useStashHostedFunscript": False}
        cfg.update(over)
        return patch("core.stash_client.get_stash_interface_config", new=AsyncMock(return_value=cfg))

    def _stub_pipeline(self, c, connected=True, setup=True):
        c._get_connected = AsyncMock(return_value=connected)
        c._estimate_offset = AsyncMock(return_value=0.0)
        c._set_mode = AsyncMock(return_value=True)
        c._hssp_setup = AsyncMock(return_value=setup)
        c._play = AsyncMock()

    async def test_activate_ready_and_plays_when_not_paused(self):
        c = HandyController("s", make_interactive_scene())
        self._stub_pipeline(c)
        with self._patch_handy_cfg():
            await c.begin_playback(5.0, is_paused=False)
        assert c.state == "ready"
        c._play.assert_awaited_once_with(5.0)

    async def test_activate_ready_but_no_play_when_paused(self):
        c = HandyController("s", make_interactive_scene())
        self._stub_pipeline(c)
        with self._patch_handy_cfg():
            await c.begin_playback(5.0, is_paused=True)
        assert c.state == "ready"
        c._play.assert_not_awaited()

    async def test_activate_fails_without_key(self):
        c = HandyController("s", make_interactive_scene())
        self._stub_pipeline(c)
        with self._patch_handy_cfg(handyKey=""):
            await c.begin_playback(5.0, is_paused=False)
        assert c.state == "failed"
        c._get_connected.assert_not_awaited()
        c._play.assert_not_awaited()

    async def test_activate_fails_when_device_not_connected(self):
        c = HandyController("s", make_interactive_scene())
        self._stub_pipeline(c, connected=False)
        with self._patch_handy_cfg():
            await c.begin_playback(5.0, is_paused=False)
        assert c.state == "failed"
        c._play.assert_not_awaited()

    async def test_activate_applies_script_offset_from_stash(self):
        c = HandyController("s", make_interactive_scene())
        self._stub_pipeline(c)
        with self._patch_handy_cfg(funscriptOffset=250):
            await c.begin_playback(5.0, is_paused=True)
        assert c.script_offset_ms == 250

    async def test_activate_anticipates_resume_when_pos_zero(self):
        scene = make_interactive_scene()
        scene["resume_time"] = 800.0
        c = HandyController("s", scene)
        self._stub_pipeline(c)
        with self._patch_handy_cfg():
            await c.begin_playback(0.0, is_paused=False)
        c._play.assert_awaited_once_with(800.0)  # jumped straight to the resume point

    async def test_activate_no_anticipation_when_real_pos_reported(self):
        scene = make_interactive_scene()
        scene["resume_time"] = 800.0
        c = HandyController("s", scene)
        self._stub_pipeline(c)
        with self._patch_handy_cfg():
            await c.begin_playback(50.0, is_paused=False)
        c._play.assert_awaited_once_with(50.0)  # real position wins, no anticipation

    async def test_activate_no_anticipation_without_resume(self):
        c = HandyController("s", make_interactive_scene())  # no resume_time
        self._stub_pipeline(c)
        with self._patch_handy_cfg():
            await c.begin_playback(0.0, is_paused=False)
        c._play.assert_awaited_once_with(0.0)


# ── fan-out gating + isolation ────────────────────────────────────────────────

def _patch_seed_cfg(**over):
    """Patch the Stash interface-config fetch used by group build/seed (no network in tests)."""
    cfg = {"handyKey": "", "funscriptOffset": 0, "useStashHostedFunscript": False}
    cfg.update(over)
    return patch("core.stash_client.get_stash_interface_config", new=AsyncMock(return_value=cfg))


class TestFanOut:
    def test_notify_playing_noop_when_disabled(self):
        config.ENABLE_HANDY_SYNC = False
        handy_controller.notify_playing("s", make_interactive_scene(), 0.0, False)
        assert "s" not in handy_controller._groups

    def test_notify_playing_noop_when_non_interactive(self):
        config.ENABLE_HANDY_SYNC = True
        handy_controller.notify_playing("s", make_interactive_scene(interactive=False), 0.0, False)
        assert "s" not in handy_controller._groups

    def test_notify_playing_noop_when_scene_none(self):
        config.ENABLE_HANDY_SYNC = True
        handy_controller.notify_playing("s", None, 0.0, False)
        assert "s" not in handy_controller._groups

    async def test_notify_playing_schedules_handler_when_enabled(self):
        config.ENABLE_HANDY_SYNC = True
        seen = {}

        async def fake(session_id, scene, pos, paused):
            seen["args"] = (session_id, pos, paused)

        with patch.object(handy_controller, "_safe_handle_playing", new=fake):
            handy_controller.notify_playing("s2", make_interactive_scene(), 3.0, False)
            await asyncio.sleep(0)
        assert seen["args"] == ("s2", 3.0, False)

    async def test_safe_handle_playing_swallows_exceptions(self):
        config.ENABLE_HANDY_SYNC = True
        handy_devices.add_device({"key": "k", "label": "d1"})
        with patch.object(HandyController, "begin_playback", new=AsyncMock(side_effect=RuntimeError("boom"))), \
             _patch_seed_cfg():
            # Must not raise — isolation contract (per-device error is swallowed by the group fan-out).
            await handy_controller._safe_handle_playing("s3", make_interactive_scene(), 0.0, False)

    async def test_first_event_begins_then_progresses(self):
        config.ENABLE_HANDY_SYNC = True
        handy_devices.add_device({"key": "k", "label": "d1"})
        with patch.object(HandyController, "begin_playback", new=AsyncMock()) as m_begin, \
             patch.object(HandyController, "on_progress", new=AsyncMock()) as m_progress, \
             _patch_seed_cfg():
            # First event → begin_playback; second → on_progress (group tracks _playback_started).
            await handy_controller._safe_handle_playing("s4", make_interactive_scene(), 0.0, False)
            await handy_controller._safe_handle_playing("s4", make_interactive_scene(), 1.0, False)
        assert m_begin.await_count == 1
        assert m_progress.await_count == 1

    async def test_notify_stopped_tears_down(self):
        config.ENABLE_HANDY_SYNC = True
        group = HandySessionGroup("s5", make_interactive_scene())
        handy_controller._groups["s5"] = group
        with patch.object(group, "teardown", new=AsyncMock()) as m_teardown:
            handy_controller.notify_stopped("s5")
            await asyncio.sleep(0)
        m_teardown.assert_awaited_once()
        assert "s5" not in handy_controller._groups


# ── LAN/direct funscript-serving endpoint ─────────────────────────────────────

class _FakeRequest:
    def __init__(self, scene_id, query=""):
        self.path_params = {"scene_id": scene_id}
        self.query_params = QueryParams(query)


class TestServeFunscriptEndpoint:
    async def test_serves_raw_json_by_default(self):
        config.ENABLE_HANDY_SYNC = True
        fs = {"actions": [{"at": 0, "pos": 0}, {"at": 100, "pos": 90}]}
        with patch("api.handy_controller._fetch_funscript", new=AsyncMock(return_value=fs)):
            resp = await handy_controller.endpoint_funscript(_FakeRequest("751"))
        assert resp.status_code == 200
        assert json.loads(bytes(resp.body)) == fs

    async def test_serves_csv_when_requested(self):
        config.ENABLE_HANDY_SYNC = True
        fs = {"actions": [{"at": 0, "pos": 0}, {"at": 100, "pos": 90}]}
        with patch("api.handy_controller._fetch_funscript", new=AsyncMock(return_value=fs)):
            resp = await handy_controller.endpoint_funscript(_FakeRequest("751", "format=csv"))
        assert resp.status_code == 200
        assert resp.media_type == "text/csv"
        assert bytes(resp.body).decode() == "0,0\r\n100,90\r\n"

    async def test_404_when_disabled(self):
        config.ENABLE_HANDY_SYNC = False
        resp = await handy_controller.endpoint_funscript(_FakeRequest("751"))
        assert resp.status_code == 404

    async def test_404_when_funscript_unavailable(self):
        config.ENABLE_HANDY_SYNC = True
        with patch("api.handy_controller._fetch_funscript", new=AsyncMock(return_value=None)):
            resp = await handy_controller.endpoint_funscript(_FakeRequest("751"))
        assert resp.status_code == 404


# ── prewarm + upload-URL cache ────────────────────────────────────────────────

class TestPrewarmCache:
    async def test_prepare_upload_url_caches(self):
        fs = {"actions": [{"at": 0, "pos": 0}]}
        with patch("api.handy_controller._fetch_funscript", new=AsyncMock(return_value=fs)) as m_fetch, \
             patch("api.handy_controller._upload_csv", new=AsyncMock(return_value="http://hosted/abc")) as m_up:
            u1 = await handy_controller._prepare_upload_url("751", "http://stash/fs")
            u2 = await handy_controller._prepare_upload_url("751", "http://stash/fs")
        assert u1 == u2 == "http://hosted/abc"
        assert m_up.await_count == 1     # second call served from cache
        assert m_fetch.await_count == 1

    async def test_prepared_url_reused_from_cache(self):
        handy_controller._script_url_cache["751"] = (time.monotonic() + 100, "http://hosted/cached")
        with patch("api.handy_controller._fetch_funscript", new=AsyncMock()) as m_fetch:
            url = await handy_controller._prepare_upload_url("751", "http://stash/fs")
        assert url == "http://hosted/cached"
        m_fetch.assert_not_awaited()     # no fetch/convert/upload — reused cache


# ── prewarm = pre-activation (PlaybackInfo) ───────────────────────────────────

class TestPreactivation:
    async def test_prewarm_noop_when_disabled(self):
        config.ENABLE_HANDY_SYNC = False
        with patch.object(HandySessionGroup, "preactivate", new=AsyncMock()) as m:
            handy_controller.prewarm(make_interactive_scene())
            await asyncio.sleep(0.02)
        m.assert_not_awaited()
        assert "stash_751" not in handy_controller._groups

    async def test_prewarm_noop_when_non_interactive(self):
        config.ENABLE_HANDY_SYNC = True
        with patch.object(HandySessionGroup, "preactivate", new=AsyncMock()) as m:
            handy_controller.prewarm(make_interactive_scene(interactive=False))
            await asyncio.sleep(0.02)
        m.assert_not_awaited()

    async def test_prewarm_preactivates_under_deterministic_session_id(self):
        config.ENABLE_HANDY_SYNC = True
        with patch.object(HandySessionGroup, "preactivate", new=AsyncMock()) as m:
            handy_controller.prewarm(make_interactive_scene("751"))
            await asyncio.sleep(0.02)
        m.assert_awaited_once()
        assert "stash_751" in handy_controller._groups  # keyed like PlaybackInfo's PlaySessionId

    async def test_prewarm_idempotent_if_already_registered(self):
        config.ENABLE_HANDY_SYNC = True
        handy_controller._groups["stash_751"] = HandySessionGroup("stash_751", make_interactive_scene("751"))
        with patch.object(HandySessionGroup, "preactivate", new=AsyncMock()) as m:
            handy_controller.prewarm(make_interactive_scene("751"))
            await asyncio.sleep(0.02)
        m.assert_not_awaited()  # already (pre)active — no second group/preactivate

    async def test_preactivate_prepares_without_playing(self):
        c = HandyController("stash_751", make_interactive_scene("751"))
        c._get_connected = AsyncMock(return_value=True)
        c._estimate_offset = AsyncMock(return_value=0.0)
        c._set_mode = AsyncMock(return_value=True)
        c._hssp_setup = AsyncMock(return_value=True)
        c._play = AsyncMock()
        with patch("api.handy_controller._prepare_upload_url", new=AsyncMock(return_value="http://s.csv")), \
             patch("core.stash_client.get_stash_interface_config",
                   new=AsyncMock(return_value={"handyKey": "k"})):
            await c.preactivate()
        assert c.state == "ready"
        assert c._playback_started is False
        c._play.assert_not_awaited()          # prepared, but no motion until real play
        c._cancel_abandon_timeout()

    async def test_start_position_used_for_first_play(self):
        handy_controller._start_pos_by_session["stash_751"] = 300.0
        c = HandyController("stash_751", make_interactive_scene("751"))
        c.state = "ready"
        c._play = AsyncMock()
        await c.begin_playback(0.0, is_paused=False)   # reported 0, but startTimeTicks said 300
        c._play.assert_awaited_once_with(300.0)

    def test_note_start_position_records_by_session(self):
        config.ENABLE_HANDY_SYNC = True
        handy_controller.note_start_position("751", 42.5)
        assert handy_controller._start_pos_by_session["stash_751"] == 42.5


# ── protocol selection (HANDY_SYNC_MODE override) ─────────────────────────────

class TestResolveUseHsp:
    def test_auto_follows_stash_local(self):
        assert handy_controller._resolve_use_hsp("auto", {"useStashHostedFunscript": True}) is True

    def test_auto_follows_stash_cloud(self):
        assert handy_controller._resolve_use_hsp("auto", {"useStashHostedFunscript": False}) is False

    def test_hosted_forces_hssp_ignoring_stash(self):
        assert handy_controller._resolve_use_hsp("hosted", {"useStashHostedFunscript": True}) is False

    def test_local_forces_hsp_ignoring_stash(self):
        assert handy_controller._resolve_use_hsp("local", {"useStashHostedFunscript": False}) is True


# ── API version gating (HANDY_APPLICATION_ID → v3, else v2) ────────────────────

class TestApiVersionGating:
    def test_defaults_to_v2_without_app_id(self):
        config.HANDY_APPLICATION_ID = ""
        c = HandyController("s", make_interactive_scene())
        assert c.use_v3 is False
        assert c._api_base().endswith("/handy/v2")
        assert "X-Api-Key" not in c._headers()

    def test_uses_v3_with_app_id(self):
        config.HANDY_APPLICATION_ID = "app-123"
        c = HandyController("s", make_interactive_scene())
        assert c.use_v3 is True
        assert c._api_base().endswith("/handy-rest/v3")
        assert c._headers().get("X-Api-Key") == "app-123"

    async def test_v3_play_uses_snake_case(self):
        config.HANDY_APPLICATION_ID = "app-123"
        c = HandyController("s", make_interactive_scene())
        c._api_put = AsyncMock()
        await c._play(5.0)
        _, body = c._api_put.await_args[0]
        assert "start_time" in body and "server_time" in body

    async def test_v2_play_uses_camel_case(self):
        config.HANDY_APPLICATION_ID = ""
        c = HandyController("s", make_interactive_scene())
        c._api_put = AsyncMock()
        await c._play(5.0)
        _, body = c._api_put.await_args[0]
        assert "startTime" in body and "serverTime" in body

    async def test_v3_set_mode_uses_mode2(self):
        config.HANDY_APPLICATION_ID = "app-123"
        c = HandyController("s", make_interactive_scene())
        c._api_put = AsyncMock(return_value={"result": "ok"})
        await c._set_mode(1)
        assert c._api_put.await_args[0][0] == "mode2"

    async def test_v2_set_mode_uses_mode(self):
        config.HANDY_APPLICATION_ID = ""
        c = HandyController("s", make_interactive_scene())
        c._api_put = AsyncMock(return_value={"result": 0})
        await c._set_mode(1)
        assert c._api_put.await_args[0][0] == "mode"


# ── HSP (local streaming) point conversion ────────────────────────────────────

class TestFunscriptToPoints:
    def test_basic_points(self):
        pts = funscript_to_points({"actions": [{"at": 0, "pos": 0}, {"at": 100, "pos": 90}]})
        assert pts == [{"t": 0, "x": 0}, {"t": 100, "x": 90}]

    def test_inverted_flips_position(self):
        assert funscript_to_points({"actions": [{"at": 5, "pos": 20}], "inverted": True}) == [{"t": 5, "x": 80}]

    def test_clamps_out_of_range(self):
        pts = funscript_to_points({"actions": [{"at": 0, "pos": 250}, {"at": 1, "pos": -5}]})
        assert pts == [{"t": 0, "x": 100}, {"t": 1, "x": 0}]

    def test_sorted_by_time(self):
        pts = funscript_to_points({"actions": [{"at": 200, "pos": 5}, {"at": 10, "pos": 6}]})
        assert [p["t"] for p in pts] == [10, 200]

    def test_skips_malformed_and_empty(self):
        assert funscript_to_points({"actions": [{"at": None, "pos": 5}]}) == []
        assert funscript_to_points({"actions": []}) == []


# ── HSP setup / prepare branch ────────────────────────────────────────────────

def _hsp_controller(session_id="hsp-1", points=None):
    """A v3 HSP controller pre-seeded with points and a stubbed offset, ready for _hsp_play.
    Default points are 1/s (sparse), so the 30 s seed fits in the first 100-point batch and play
    issues a single call — dense cases pass their own points."""
    config.HANDY_APPLICATION_ID = "app-xyz"
    c = HandyController(session_id, make_interactive_scene())
    c.state = "ready"
    c.use_hsp = True
    c.estimated_offset_ms = 0.0
    c.script_offset_ms = 0
    c._hsp_points = points if points is not None else [{"t": i * 1000, "x": i % 100} for i in range(500)]
    c._hsp_times = [p["t"] for p in c._hsp_points]
    return c


class TestHspPrepareBranch:
    def _patch_cfg(self, **over):
        cfg = {"handyKey": "kJVRef7g", "funscriptOffset": 0, "useStashHostedFunscript": True}
        cfg.update(over)
        return patch("core.stash_client.get_stash_interface_config", new=AsyncMock(return_value=cfg))

    async def test_prepare_uses_hsp_when_local_and_v3(self):
        config.HANDY_APPLICATION_ID = "app-xyz"
        config.HANDY_SYNC_MODE = "local"
        c = HandyController("s", make_interactive_scene())
        c._get_connected = AsyncMock(return_value=True)
        c._estimate_offset = AsyncMock(return_value=0.0)
        c._set_mode = AsyncMock(return_value=True)
        c._hsp_setup = AsyncMock(return_value=True)
        with patch("api.handy_controller._fetch_funscript",
                   new=AsyncMock(return_value={"actions": [{"at": 0, "pos": 10}, {"at": 50, "pos": 90}]})), \
             self._patch_cfg():
            await c.preactivate()
        assert c.state == "ready"
        assert c.use_hsp is True
        assert c._hsp_points == [{"t": 0, "x": 10}, {"t": 50, "x": 90}]
        c._set_mode.assert_awaited_once_with(handy_controller.MODE_HSP)
        c._hsp_setup.assert_awaited_once()
        c._cancel_abandon_timeout()

    async def test_hsp_fails_loudly_without_v3(self):
        config.HANDY_APPLICATION_ID = ""   # no app id -> v2 only
        config.HANDY_SYNC_MODE = "local"
        c = HandyController("s", make_interactive_scene())
        c._get_connected = AsyncMock(return_value=True)
        with self._patch_cfg():
            await c.preactivate()
        assert c.state == "failed"          # no silent HSSP fallback
        c._get_connected.assert_not_awaited()

    async def test_hsp_setup_captures_max_points_and_stream_id(self):
        config.HANDY_APPLICATION_ID = "app-xyz"
        c = HandyController("s", make_interactive_scene())
        c._api_put = AsyncMock(return_value={"result": {"max_points": 9876, "stream_id": 42}})
        assert await c._hsp_setup() is True
        assert c._hsp_max_points == 9876
        assert c._hsp_stream_id == 42
        assert c._api_put.await_args[0][0] == "hsp/setup"


# ── HSP play / seed / refill ──────────────────────────────────────────────────

def _dense_points(count, step_ms=100):
    """Points every step_ms (default 10/s) — used to exercise multi-chunk seeding/refill."""
    return [{"t": i * step_ms, "x": i % 100} for i in range(count)]


class TestHspPlay:
    async def test_play_seeds_from_position_with_flush(self):
        c = _hsp_controller()  # 1/s points
        c._api_put = AsyncMock(return_value={"result": {}})
        with patch("api.handy_controller._now_ms", return_value=5000.0):
            await c._hsp_play(10.0)  # 10 s -> start_time 10000 ms -> index 10
        # First (and only, sparse) call is the play with the embedded flush seed.
        path, body = c._api_put.await_args_list[0][0]
        assert path == "hsp/play"
        assert body["start_time"] == 10000
        assert body["server_time"] == 5000
        add = body["add"]
        assert add["flush"] is True
        assert len(add["points"]) == handy_controller.HSP_ADD_BATCH  # 100 pts covers >30 s at 1/s
        assert add["points"][0] == {"t": 10000, "x": 10}
        assert add["tail_point_stream_index"] == handy_controller.HSP_ADD_BATCH - 1
        assert c._is_playing is True
        assert c._hsp_next_index == 10 + handy_controller.HSP_ADD_BATCH
        c._cancel_refill()

    async def test_play_seeds_min_seconds_when_dense(self):
        # Dense (10/s): the play's embedded add only covers 10 s, so follow-up adds must fill to the
        # 30 s seed floor before the refill task starts.
        c = _hsp_controller(points=_dense_points(2000))
        c._api_put = AsyncMock(return_value={"result": {}})
        await c._hsp_play(0.0)
        calls = c._api_put.await_args_list
        assert calls[0][0][0] == "hsp/play"
        assert all(call[0][0] == "hsp/add" for call in calls[1:])
        assert calls[1][0][1]["flush"] is False       # follow-up seed adds don't flush
        # 30 s at 10/s = ~300 pts -> the 100-pt play seed + 2 more adds (200 pts) -> covers t<=30000.
        assert c._hsp_next_index >= 300
        assert c._hsp_points[c._hsp_next_index - 1]["t"] <= 30000 + 100
        c._cancel_refill()

    async def test_play_applies_script_offset_to_seed(self):
        pts = [{"t": t, "x": 0} for t in (0, 1000, 2000, 3000)]
        c = _hsp_controller(points=pts)
        c.script_offset_ms = 1000
        c._api_put = AsyncMock(return_value={"result": {}})
        await c._hsp_play(1.0)  # 1000 + offset 1000 = start_time 2000 -> seed at t>=2000
        add = c._api_put.await_args_list[0][0][1]["add"]
        assert add["points"][0]["t"] == 2000
        c._cancel_refill()

    async def test_play_past_end_does_not_command(self):
        pts = [{"t": t, "x": 0} for t in (0, 100, 200)]
        c = _hsp_controller(points=pts)
        c._api_put = AsyncMock()
        await c._hsp_play(999.0)  # far past the last point
        c._api_put.assert_not_awaited()
        assert c._is_playing is False

    async def test_play_not_accepted_leaves_not_playing(self):
        c = _hsp_controller()
        c._api_put = AsyncMock(return_value=None)  # device rejected
        await c._hsp_play(0.0)
        assert c._is_playing is False

    async def test_seek_reseeds_with_flush_and_resets_counter(self):
        c = _hsp_controller()
        c._api_put = AsyncMock(return_value={"result": {}})
        await c._hsp_play(0.0)
        first_sent = c._hsp_points_sent
        await c._hsp_play(20.0)  # seek -> new flush add
        add = c._api_put.await_args_list[-1][0][1]["add"]
        assert add["flush"] is True
        # flush restarts the run, so the counter reflects only this batch, not the sum.
        assert c._hsp_points_sent == len(add["points"]) == first_sent
        c._cancel_refill()

    async def test_dispatch_play_routes_to_hsp(self):
        # _play() must branch to _hsp_play when use_hsp is set.
        c = _hsp_controller()
        c._hsp_play = AsyncMock()
        await c._play(3.0)
        c._hsp_play.assert_awaited_once_with(3.0)

    async def test_stop_uses_hsp_endpoint(self):
        c = _hsp_controller()
        c._api_put = AsyncMock()
        c._is_playing = True
        await c._stop()
        assert c._api_put.await_args[0][0] == "hsp/stop"
        assert c._is_playing is False


class TestHspRefill:
    def test_current_time_parses_result_and_flat(self):
        assert HandyController._hsp_current_time({"result": {"current_time": 1234}}) == 1234
        assert HandyController._hsp_current_time({"current_time": 7}) == 7
        assert HandyController._hsp_current_time(None) is None
        assert HandyController._hsp_current_time({"result": {}}) is None

    async def test_refill_fills_to_max_seconds_ahead(self):
        c = _hsp_controller()  # 1/s points
        c._is_playing = True
        c._hsp_next_index = 0
        c._api_get = AsyncMock(return_value={"result": {"current_time": 0}})
        c._api_put = AsyncMock(return_value={"result": {}})
        await c._hsp_refill_once()  # MAX=60 s -> fill points with t<=60000 (61 points, one add)
        assert c._api_put.await_count == 1
        assert c._hsp_next_index == 61
        assert c._hsp_points[c._hsp_next_index - 1]["t"] <= 60000

    async def test_refill_uses_device_current_time_as_window_base(self):
        c = _hsp_controller()  # 1/s
        c._is_playing = True
        c._hsp_next_index = 100  # already streamed up to t=99000
        c._api_get = AsyncMock(return_value={"result": {"current_time": 100000}})  # 100 s in
        c._api_put = AsyncMock(return_value={"result": {}})
        await c._hsp_refill_once()  # window end = 100000 + 60000 = 160000 -> up to index 161
        assert c._hsp_next_index == 161

    async def test_refill_noop_when_already_buffered_past_window(self):
        c = _hsp_controller()  # 1/s
        c._is_playing = True
        c._hsp_next_index = 200  # already buffered to t=199000, well past a 60 s window from t=0
        c._api_get = AsyncMock(return_value={"result": {"current_time": 0}})
        c._api_put = AsyncMock()
        await c._hsp_refill_once()
        c._api_put.assert_not_awaited()

    async def test_refill_chunks_dense_buffer(self):
        c = _hsp_controller(points=_dense_points(2000))  # 10/s
        c._is_playing = True
        c._hsp_next_index = 0
        c._api_get = AsyncMock(return_value={"result": {"current_time": 0}})
        c._api_put = AsyncMock(return_value={"result": {}})
        await c._hsp_refill_once()  # 60 s at 10/s = 601 pts (incl. t=60000) -> 7 adds
        assert c._api_put.await_count == 7
        assert c._hsp_next_index == 601

    async def test_refill_caps_adds_per_fill(self):
        # Ultra-dense (100/s): a 60 s window is 6000 pts, but a single pass is capped.
        c = _hsp_controller(points=_dense_points(10000, step_ms=10))
        c._is_playing = True
        c._hsp_next_index = 0
        c._api_get = AsyncMock(return_value={"result": {"current_time": 0}})
        c._api_put = AsyncMock(return_value={"result": {}})
        await c._hsp_refill_once()
        assert c._api_put.await_count == handy_controller.HSP_MAX_ADDS_PER_FILL
        assert c._hsp_next_index == handy_controller.HSP_MAX_ADDS_PER_FILL * handy_controller.HSP_ADD_BATCH

    async def test_refill_stops_at_end_of_script(self):
        pts = [{"t": t, "x": 0} for t in range(0, 5000, 1000)]  # 5 points, 1/s
        c = _hsp_controller(points=pts)
        c._is_playing = True
        c._hsp_next_index = 3  # only 2 points left
        c._api_get = AsyncMock(return_value={"result": {"current_time": 0}})
        c._api_put = AsyncMock(return_value={"result": {}})
        await c._hsp_refill_once()
        assert c._api_put.await_count == 1
        assert c._hsp_next_index == 5  # pushed the remaining 2, then stopped

    async def test_refill_noop_when_no_current_time(self):
        c = _hsp_controller()
        c._is_playing = True
        c._api_get = AsyncMock(return_value={"result": {}})  # no current_time
        c._api_put = AsyncMock()
        await c._hsp_refill_once()
        c._api_put.assert_not_awaited()

    async def test_refill_stops_on_add_rejection(self):
        c = _hsp_controller(points=_dense_points(2000))
        c._is_playing = True
        c._hsp_next_index = 0
        c._api_get = AsyncMock(return_value={"result": {"current_time": 0}})
        c._api_put = AsyncMock(return_value=None)  # first add rejected
        await c._hsp_refill_once()
        assert c._api_put.await_count == 1
        assert c._hsp_next_index == 0

    async def test_teardown_cancels_refill_task(self):
        c = _hsp_controller()
        c._api_put = AsyncMock()
        c._start_refill()
        task = c._hsp_refill_task
        assert task is not None and not task.done()
        await c.teardown()
        assert c._hsp_refill_task is None
        # Drain the cancelled task (it may be cancelled before its handler runs, or catch+return).
        try:
            await task
        except asyncio.CancelledError:
            pass
        assert task.done()


# ── multi-device: per-device binding, session group, endpoints ────────────────

class TestDeviceBinding:
    async def test_controller_uses_device_key_and_mode(self):
        config.HANDY_APPLICATION_ID = "app-xyz"
        device = {"label": "Toy2", "key": "devkey", "sync_mode": "hosted"}
        c = HandyController("s", make_interactive_scene(), device)
        c._get_connected = AsyncMock(return_value=True)
        c._estimate_offset = AsyncMock(return_value=0.0)
        c._prepare_hssp = AsyncMock(return_value=True)
        c._prepare_hsp = AsyncMock(return_value=True)
        with patch("core.stash_client.get_stash_interface_config",
                   new=AsyncMock(return_value={"handyKey": "STASHKEY", "useStashHostedFunscript": True})):
            await c.preactivate()
        assert c.key == "devkey"          # device key wins over Stash's
        assert c.use_hsp is False         # sync_mode=hosted forces HSSP even though Stash says local
        c._prepare_hssp.assert_awaited_once()
        c._prepare_hsp.assert_not_awaited()
        c._cancel_abandon_timeout()

    async def test_device_offset_overrides_stash(self):
        device = {"key": "k", "funscript_offset": 500}
        c = HandyController("s", make_interactive_scene(), device)
        c._get_connected = AsyncMock(return_value=True)
        c._estimate_offset = AsyncMock(return_value=0.0)
        c._prepare_hssp = AsyncMock(return_value=True)
        with patch("core.stash_client.get_stash_interface_config",
                   new=AsyncMock(return_value={"funscriptOffset": 999})):
            await c.preactivate()
        assert c.script_offset_ms == 500   # device override wins over Stash's 999
        c._cancel_abandon_timeout()

    def test_device_hsp_knob_override(self):
        c = HandyController("s", make_interactive_scene(), {"key": "k", "hsp_buffer_max_s": 90})
        assert c._cfg_int("HANDY_HSP_BUFFER_MAX_S", 60, c._dev_hsp_max) == 90
        # None override falls back to the global default
        c2 = HandyController("s", make_interactive_scene(), {"key": "k"})
        assert c2._cfg_int("HANDY_HSP_BUFFER_MAX_S", 60, c2._dev_hsp_max) == 60


class TestHandySessionGroup:
    async def test_builds_one_controller_per_enabled_device(self):
        handy_devices.add_device({"key": "k1", "label": "A"})
        handy_devices.add_device({"key": "k2", "label": "B", "enabled": False})
        handy_devices.add_device({"key": "k3", "label": "C"})
        group = HandySessionGroup("sess", make_interactive_scene())
        with patch.object(HandyController, "preactivate", new=AsyncMock()), _patch_seed_cfg():
            await group.preactivate()
        assert [c.label for c in group.controllers] == ["A", "C"]  # disabled B skipped
        group._cancel_abandon_timeout()

    async def test_fan_isolates_per_device_failure(self):
        handy_devices.add_device({"key": "k1", "label": "A"})
        handy_devices.add_device({"key": "k2", "label": "B"})
        driven = []

        async def begin(self, pos, paused):
            driven.append(self.label)
            if self.label == "A":
                raise RuntimeError("device A boom")

        group = HandySessionGroup("sess", make_interactive_scene())
        with patch.object(HandyController, "begin_playback", new=begin), _patch_seed_cfg():
            await group.handle_playing(5.0, False)  # must not raise
        assert set(driven) == {"A", "B"}  # B still driven despite A failing

    async def test_handle_playing_begins_then_progresses(self):
        handy_devices.add_device({"key": "k1", "label": "A"})
        group = HandySessionGroup("sess", make_interactive_scene())
        with patch.object(HandyController, "begin_playback", new=AsyncMock()) as mb, \
             patch.object(HandyController, "on_progress", new=AsyncMock()) as mp, _patch_seed_cfg():
            await group.handle_playing(0.0, False)
            await group.handle_playing(1.0, False)
        assert mb.await_count == 1 and mp.await_count == 1

    async def test_teardown_fans_to_all(self):
        handy_devices.add_device({"key": "k1", "label": "A"})
        handy_devices.add_device({"key": "k2", "label": "B"})
        group = HandySessionGroup("sess", make_interactive_scene())
        with patch.object(HandyController, "preactivate", new=AsyncMock()), _patch_seed_cfg():
            await group.preactivate()
        with patch.object(HandyController, "teardown", new=AsyncMock()) as mt:
            await group.teardown()
        assert mt.await_count == 2

    async def test_seed_adds_stash_device_on_build(self):
        group = HandySessionGroup("sess", make_interactive_scene())
        with patch.object(HandyController, "preactivate", new=AsyncMock()), \
             _patch_seed_cfg(handyKey="stashkey"):
            await group.preactivate()
        assert any(d["key"] == "stashkey" for d in handy_devices.list_devices())
        group._cancel_abandon_timeout()


class TestDeviceEndpoints:
    async def test_status_probes_only_enabled(self):
        handy_devices.add_device({"key": "k1", "label": "A"})
        handy_devices.add_device({"key": "k2", "label": "B", "enabled": False})
        with patch.object(handy_controller, "_probe_connected", new=AsyncMock(return_value=True)):
            resp = await handy_controller.endpoint_devices_status(_FakeRequest(""))
        data = json.loads(bytes(resp.body))
        assert len(data["status"]) == 1
        assert data["status"][0]["connected"] is True

    async def test_list_seeds_stash_device(self):
        with _patch_seed_cfg(handyKey="stashk"):
            resp = await handy_controller.endpoint_devices_list(_FakeRequest(""))
        data = json.loads(bytes(resp.body))
        assert any(d["key"] == "stashk" for d in data["devices"])
