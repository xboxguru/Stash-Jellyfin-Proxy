"""
Unit tests for api/vertical_engine.py + api/vertical_routes.py session semantics
(Feature 1 — Vertical Multi-View, pre-test hardening pass).

Covers:
  - ensure(): seek=None (request carried no position) never relaunches a live
    session; a genuinely new seek relaunches with the cached sides; a dead
    session launches at 0.0 when no position is given
  - _valid_session_id: only `{numeric}-{8-hex}` ids reach the engine
  - vdebug: VERTICAL_DEBUG promotes diagnostics from DEBUG to INFO
"""
import logging
from unittest.mock import AsyncMock

import config
from api.vertical_engine import _VerticalSessionManager
from api.vertical_routes import _valid_session_id, _new_session_id
from core.vertical import vdebug


class TestEnsureSeekSemantics:
    def _live_manager(self, cur_seek: float):
        """Manager faked into 'session alive and serving' state."""
        mgr = _VerticalSessionManager()
        mgr.is_alive = lambda sid: True
        mgr.manifest_path = lambda sid: "/tmp/fake/stream.m3u8"
        mgr._launch_info["s1"] = {"seek": cur_seek}
        mgr._sides["s1"] = ["101", "102"]
        mgr._launch = AsyncMock(return_value=True)
        mgr._stop_locked = AsyncMock()
        return mgr

    async def test_seek_none_is_steady_state_no_relaunch(self):
        # A manifest poll without StartTimeTicks must NOT reset a sought session to 0.
        mgr = self._live_manager(cur_seek=120.0)
        assert await mgr.ensure("s1", {"id": "1"}, None) is True
        mgr._stop_locked.assert_not_called()
        mgr._launch.assert_not_called()

    async def test_same_seek_is_noop(self):
        mgr = self._live_manager(cur_seek=120.0)
        assert await mgr.ensure("s1", {"id": "1"}, 120.2) is True
        mgr._launch.assert_not_called()

    async def test_new_seek_relaunches_with_cached_sides(self):
        mgr = self._live_manager(cur_seek=0.0)
        assert await mgr.ensure("s1", {"id": "1"}, 300.0) is True
        mgr._stop_locked.assert_called_once()
        assert mgr._stop_locked.call_args.kwargs.get("keep_sides") is True
        mgr._launch.assert_called_once()
        assert mgr._launch.call_args.args[2] == 300.0          # new seek
        assert mgr._launch.call_args.kwargs["sides"] == ["101", "102"]  # sides stable

    async def test_dead_session_with_no_position_launches_at_zero(self):
        mgr = _VerticalSessionManager()
        mgr._launch = AsyncMock(return_value=True)
        mgr._stop_locked = AsyncMock()
        assert await mgr.ensure("s2", {"id": "2"}, None) is True
        assert mgr._launch.call_args.args[2] == 0.0


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
