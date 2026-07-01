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
from api import handy_controller
from api.handy_controller import HandyController, funscript_to_csv


def make_interactive_scene(scene_id="751", funscript_url="http://stash:9999/scene/751/funscript", interactive=True):
    return {"id": scene_id, "interactive": interactive, "paths": {"funscript": funscript_url}}


@pytest.fixture(autouse=True)
def _clean_registry():
    handy_controller._controllers.clear()
    handy_controller._script_url_cache.clear()
    handy_controller._start_pos_by_session.clear()
    config.HANDY_SYNC_MODE = "auto"
    config.HANDY_APPLICATION_ID = ""   # default to v2 unless a test opts into v3
    orig_debounce = handy_controller.SEEK_DEBOUNCE_S
    handy_controller.SEEK_DEBOUNCE_S = 0.01  # keep debounce tests fast
    yield
    handy_controller.SEEK_DEBOUNCE_S = orig_debounce
    handy_controller._controllers.clear()
    handy_controller._script_url_cache.clear()


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

class TestFanOut:
    def test_notify_playing_noop_when_disabled(self):
        config.ENABLE_HANDY_SYNC = False
        handy_controller.notify_playing("s", make_interactive_scene(), 0.0, False)
        assert "s" not in handy_controller._controllers

    def test_notify_playing_noop_when_non_interactive(self):
        config.ENABLE_HANDY_SYNC = True
        handy_controller.notify_playing("s", make_interactive_scene(interactive=False), 0.0, False)
        assert "s" not in handy_controller._controllers

    def test_notify_playing_noop_when_scene_none(self):
        config.ENABLE_HANDY_SYNC = True
        handy_controller.notify_playing("s", None, 0.0, False)
        assert "s" not in handy_controller._controllers

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
        with patch.object(HandyController, "begin_playback", new=AsyncMock(side_effect=RuntimeError("boom"))):
            # Must not raise — isolation contract.
            await handy_controller._safe_handle_playing("s3", make_interactive_scene(), 0.0, False)

    async def test_failed_controller_is_not_retried(self):
        config.ENABLE_HANDY_SYNC = True
        with patch.object(HandyController, "begin_playback", new=AsyncMock()) as m_begin, \
             patch.object(HandyController, "on_progress", new=AsyncMock()) as m_progress:
            # First event begins playback; mark started+failed afterwards (as real begin_playback would).
            await handy_controller._safe_handle_playing("s4", make_interactive_scene(), 0.0, False)
            handy_controller._controllers["s4"]._playback_started = True
            handy_controller._controllers["s4"].state = "failed"
            # Second event routes to on_progress (no re-begin), which itself no-ops on failed.
            await handy_controller._safe_handle_playing("s4", make_interactive_scene(), 1.0, False)
        assert m_begin.await_count == 1
        assert m_progress.await_count == 1

    async def test_notify_stopped_tears_down(self):
        config.ENABLE_HANDY_SYNC = True
        c = _ready_controller("s5")
        handy_controller._controllers["s5"] = c
        with patch.object(c, "teardown", new=AsyncMock()) as m_teardown:
            handy_controller.notify_stopped("s5")
            await asyncio.sleep(0)
        m_teardown.assert_awaited_once()
        assert "s5" not in handy_controller._controllers


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
        with patch.object(HandyController, "preactivate", new=AsyncMock()) as m:
            handy_controller.prewarm(make_interactive_scene())
            await asyncio.sleep(0.02)
        m.assert_not_awaited()
        assert "stash_751" not in handy_controller._controllers

    async def test_prewarm_noop_when_non_interactive(self):
        config.ENABLE_HANDY_SYNC = True
        with patch.object(HandyController, "preactivate", new=AsyncMock()) as m:
            handy_controller.prewarm(make_interactive_scene(interactive=False))
            await asyncio.sleep(0.02)
        m.assert_not_awaited()

    async def test_prewarm_preactivates_under_deterministic_session_id(self):
        config.ENABLE_HANDY_SYNC = True
        with patch.object(HandyController, "preactivate", new=AsyncMock()) as m:
            handy_controller.prewarm(make_interactive_scene("751"))
            await asyncio.sleep(0.02)
        m.assert_awaited_once()
        assert "stash_751" in handy_controller._controllers  # keyed like PlaybackInfo's PlaySessionId

    async def test_prewarm_idempotent_if_already_registered(self):
        config.ENABLE_HANDY_SYNC = True
        handy_controller._controllers["stash_751"] = _ready_controller("stash_751")
        with patch.object(HandyController, "preactivate", new=AsyncMock()) as m:
            handy_controller.prewarm(make_interactive_scene("751"))
            await asyncio.sleep(0.02)
        m.assert_not_awaited()  # already (pre)active — no second controller/preactivate

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
        config.HANDY_SYNC_MODE = "auto"
        assert handy_controller._resolve_use_hsp({"useStashHostedFunscript": True}) is True

    def test_auto_follows_stash_cloud(self):
        config.HANDY_SYNC_MODE = "auto"
        assert handy_controller._resolve_use_hsp({"useStashHostedFunscript": False}) is False

    def test_hosted_forces_hssp_ignoring_stash(self):
        config.HANDY_SYNC_MODE = "hosted"
        assert handy_controller._resolve_use_hsp({"useStashHostedFunscript": True}) is False

    def test_local_forces_hsp_ignoring_stash(self):
        config.HANDY_SYNC_MODE = "local"
        assert handy_controller._resolve_use_hsp({"useStashHostedFunscript": False}) is True


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
