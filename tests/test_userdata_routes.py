"""
Tests for api/userdata_routes.py

IMPORTANT — NO LIVE STASH WRITES:
All calls to stash_client (increment_play_count, update_resume_time,
increment_o_counter, update_rating, reset_play_count, reset_activity,
get_scene) are mocked with AsyncMock. This test suite makes zero
real HTTP requests to any Stash instance.

Covers:
  - _evaluate_playback_action: 90 % threshold, unknown runtime, < 1 % guard
  - POST /sessions/playing: stream registration, stat counters
  - POST /sessions/playing/stopped: play/resume update logic
  - POST/DELETE /users/{id}/playeditems/{item_id}: mark played / unplayed
  - POST/DELETE /users/{id}/favoriteitems/{item_id}: mark favorite / unfavorite
  - POST /useritems/{item_id}/userdata: userdata fetch
"""
import pytest
from unittest.mock import AsyncMock, patch, call
from core.jellyfin_mapper import encode_id
from tests.conftest import make_scene
import state
import config

from api.userdata_routes import _evaluate_playback_action


# ── _evaluate_playback_action (pure function) ─────────────────────────────────

class TestEvaluatePlaybackAction:
    # Threshold: played = True when percentage >= 90 %

    def test_exactly_90_percent_marks_played(self):
        played, resume = _evaluate_playback_action(9_000_000, 10_000_000)
        assert played is True
        assert resume == 0.0

    def test_above_90_percent_marks_played(self):
        played, resume = _evaluate_playback_action(9_500_000, 10_000_000)
        assert played is True
        assert resume == 0.0

    def test_below_90_percent_not_played(self):
        played, resume = _evaluate_playback_action(8_900_000, 10_000_000)
        assert played is False

    def test_below_90_saves_resume_time(self):
        played, resume = _evaluate_playback_action(5_000_000, 10_000_000)
        assert played is False
        # 5_000_000 ticks / 10_000_000 ticks-per-second = 0.5 s
        assert pytest.approx(resume, abs=0.01) == 0.5

    def test_less_than_1_percent_saves_zero_resume(self):
        # 0.5 % — too early to bother saving
        played, resume = _evaluate_playback_action(50_000, 10_000_000)
        assert played is False
        assert resume == 0.0

    def test_zero_position_saves_zero_resume(self):
        played, resume = _evaluate_playback_action(0, 10_000_000)
        assert played is False
        assert resume == 0.0

    # Unknown runtime (runtime_ticks <= 0): never auto-mark played

    def test_unknown_runtime_never_marks_played(self):
        played, _ = _evaluate_playback_action(999_999_999, 0)
        assert played is False

    def test_unknown_runtime_saves_resume_when_gt_one_second(self):
        # > 10_000_000 ticks = > 1 second watched
        played, resume = _evaluate_playback_action(30_000_000, 0)  # 3 seconds
        assert played is False
        assert resume == pytest.approx(3.0, abs=0.01)

    def test_unknown_runtime_no_resume_when_under_one_second(self):
        played, resume = _evaluate_playback_action(5_000_000, 0)  # 0.5 s
        assert played is False
        assert resume == 0.0


# ── POST /sessions/playing ────────────────────────────────────────────────────

class TestSessionsPlaying:
    def _scene_payload(self, scene_id="123", ticks=0, runtime=36_000_000_000):
        encoded = encode_id("scene", scene_id)
        return {
            "PlaySessionId": "sess-abc",
            "ItemId": encoded,
            "PlaybackPositionTicks": ticks,
            "RunTimeTicks": runtime,
            "Item": {"Name": "Test Scene", "RunTimeTicks": runtime},
        }

    def test_returns_204(self, client):
        scene = make_scene()
        with patch("core.stash_client.get_scene", new=AsyncMock(return_value=scene)):
            r = client.post("/sessions/playing", json=self._scene_payload())
        assert r.status_code == 204

    def test_registers_stream_in_state(self, client):
        scene = make_scene()
        with patch("core.stash_client.get_scene", new=AsyncMock(return_value=scene)):
            client.post("/sessions/playing", json=self._scene_payload())
        assert len(state.active_streams) == 1

    def test_increments_streams_today(self, client):
        scene = make_scene()
        before = state.stats["streams_today"]
        with patch("core.stash_client.get_scene", new=AsyncMock(return_value=scene)):
            client.post("/sessions/playing", json=self._scene_payload())
        assert state.stats["streams_today"] == before + 1

    def test_increments_total_streams(self, client):
        scene = make_scene()
        before = state.stats["total_streams"]
        with patch("core.stash_client.get_scene", new=AsyncMock(return_value=scene)):
            client.post("/sessions/playing", json=self._scene_payload())
        assert state.stats["total_streams"] == before + 1

    def test_second_report_updates_ticks_not_new_stream(self, client):
        scene = make_scene()
        payload = self._scene_payload(ticks=1_000_000)
        with patch("core.stash_client.get_scene", new=AsyncMock(return_value=scene)):
            client.post("/sessions/playing", json=payload)
            payload["PlaybackPositionTicks"] = 5_000_000
            client.post("/sessions/playing", json=payload)
        # Should still be just 1 stream
        assert len(state.active_streams) == 1
        assert state.active_streams[0]["last_ticks"] == 5_000_000

    def test_ticks_never_go_backward(self, client):
        scene = make_scene()
        payload = self._scene_payload(ticks=5_000_000)
        with patch("core.stash_client.get_scene", new=AsyncMock(return_value=scene)):
            client.post("/sessions/playing", json=payload)
            payload["PlaybackPositionTicks"] = 1_000_000  # seek backward
            client.post("/sessions/playing", json=payload)
        assert state.active_streams[0]["last_ticks"] == 5_000_000


# ── POST /sessions/playing/stopped ────────────────────────────────────────────

class TestSessionsStopped:
    def _stopped_payload(self, scene_id="123", ticks=9_500_000, runtime=10_000_000):
        encoded = encode_id("scene", scene_id)
        return {
            "PlaySessionId": "sess-abc",
            "ItemId": encoded,
            "PlaybackPositionTicks": ticks,
            "RunTimeTicks": runtime,
        }

    def test_returns_204(self, client):
        with patch("core.stash_client.increment_play_count", new=AsyncMock()), \
             patch("core.stash_client.update_resume_time", new=AsyncMock()):
            r = client.post("/sessions/playing/stopped", json=self._stopped_payload())
        assert r.status_code == 204

    def test_play_count_incremented_when_watched_over_90_percent(self, client):
        # Pre-register the stream so stopped endpoint finds it
        state.active_streams = [{
            "id": "sess-abc", "item_id": "scene-123",
            "last_ticks": 9_500_000, "runtime_ticks": 10_000_000,
            "started": 0, "last_ping": 0,
        }]
        mock_inc = AsyncMock()
        with patch("core.stash_client.increment_play_count", new=mock_inc), \
             patch("core.stash_client.update_resume_time", new=AsyncMock()):
            client.post("/sessions/playing/stopped", json=self._stopped_payload(ticks=9_500_000))
        mock_inc.assert_called_once_with("123")

    def test_resume_time_saved_when_under_90_percent(self, client):
        state.active_streams = [{
            "id": "sess-abc", "item_id": "scene-123",
            "last_ticks": 5_000_000, "runtime_ticks": 10_000_000,
            "started": 0, "last_ping": 0,
        }]
        mock_resume = AsyncMock()
        with patch("core.stash_client.increment_play_count", new=AsyncMock()), \
             patch("core.stash_client.update_resume_time", new=mock_resume):
            client.post("/sessions/playing/stopped", json=self._stopped_payload(ticks=5_000_000))
        mock_resume.assert_called_once()
        args = mock_resume.call_args[0]
        assert args[0] == "123"
        assert pytest.approx(args[1], abs=0.1) == 0.5  # 5_000_000 / 10_000_000


# ── POST /users/{id}/playeditems/{item_id} — mark played ─────────────────────

class TestMarkPlayed:
    def test_mark_played_returns_200(self, client):
        scene = make_scene(scene_id="123", play_count=0)
        encoded = encode_id("scene", "123")
        with patch("core.stash_client.get_scene", new=AsyncMock(return_value=scene)), \
             patch("core.stash_client.increment_play_count", new=AsyncMock()), \
             patch("core.stash_client.update_resume_time", new=AsyncMock()):
            r = client.post(f"/users/testuser/playeditems/{encoded}")
        assert r.status_code == 200

    def test_mark_played_response_has_played_true(self, client):
        scene = make_scene(scene_id="123", play_count=1)
        encoded = encode_id("scene", "123")
        with patch("core.stash_client.get_scene", new=AsyncMock(return_value=scene)), \
             patch("core.stash_client.increment_play_count", new=AsyncMock()), \
             patch("core.stash_client.update_resume_time", new=AsyncMock()):
            data = client.post(f"/users/testuser/playeditems/{encoded}").json()
        assert data["Played"] is True


# ── DELETE /users/{id}/playeditems/{item_id} — mark unplayed ─────────────────

class TestMarkUnplayed:
    def test_mark_unplayed_returns_200(self, client):
        scene = make_scene(scene_id="123", play_count=1)
        encoded = encode_id("scene", "123")
        with patch("core.stash_client.get_scene", new=AsyncMock(return_value=scene)), \
             patch("core.stash_client.reset_play_count", new=AsyncMock()), \
             patch("core.stash_client.reset_activity", new=AsyncMock()):
            r = client.delete(f"/users/testuser/playeditems/{encoded}")
        assert r.status_code == 200

    def test_mark_unplayed_response_has_played_false(self, client):
        scene = make_scene(scene_id="123", play_count=1)
        encoded = encode_id("scene", "123")
        with patch("core.stash_client.get_scene", new=AsyncMock(return_value=scene)), \
             patch("core.stash_client.reset_play_count", new=AsyncMock()), \
             patch("core.stash_client.reset_activity", new=AsyncMock()):
            data = client.delete(f"/users/testuser/playeditems/{encoded}").json()
        assert data["Played"] is False


# ── POST /users/{id}/favoriteitems/{item_id} — mark favorite ─────────────────

class TestMarkFavorite:
    def test_mark_favorite_returns_200(self, client):
        scene = make_scene(scene_id="123")
        encoded = encode_id("scene", "123")
        with patch("core.stash_client.get_scene", new=AsyncMock(return_value=scene)), \
             patch("core.stash_client.increment_o_counter", new=AsyncMock()):
            r = client.post(f"/users/testuser/favoriteitems/{encoded}")
        assert r.status_code == 200

    def test_mark_favorite_response_has_is_favorite_true(self, client):
        scene = make_scene(scene_id="123")
        encoded = encode_id("scene", "123")
        with patch("core.stash_client.get_scene", new=AsyncMock(return_value=scene)), \
             patch("core.stash_client.increment_o_counter", new=AsyncMock()):
            data = client.post(f"/users/testuser/favoriteitems/{encoded}").json()
        assert data["IsFavorite"] is True

    def test_unmark_favorite_returns_200(self, client):
        scene = make_scene(scene_id="123")
        encoded = encode_id("scene", "123")
        with patch("core.stash_client.get_scene", new=AsyncMock(return_value=scene)):
            r = client.delete(f"/users/testuser/favoriteitems/{encoded}")
        assert r.status_code == 200


# ── POST /useritems/{item_id}/userdata ────────────────────────────────────────

class TestUpdateUserdata:
    def test_returns_200(self, client):
        scene = make_scene(scene_id="123", play_count=2, resume_time=45.0)
        encoded = encode_id("scene", "123")
        with patch("core.stash_client.get_scene", new=AsyncMock(return_value=scene)):
            r = client.post(f"/useritems/{encoded}/userdata")
        assert r.status_code == 200

    def test_play_count_in_response(self, client):
        scene = make_scene(scene_id="123", play_count=3)
        encoded = encode_id("scene", "123")
        with patch("core.stash_client.get_scene", new=AsyncMock(return_value=scene)):
            data = client.post(f"/useritems/{encoded}/userdata").json()
        assert data["PlayCount"] == 3

    def test_resume_ticks_in_response(self, client):
        scene = make_scene(scene_id="123", resume_time=60.0)  # 60 seconds
        encoded = encode_id("scene", "123")
        with patch("core.stash_client.get_scene", new=AsyncMock(return_value=scene)):
            data = client.post(f"/useritems/{encoded}/userdata").json()
        assert data["PlaybackPositionTicks"] == 60 * 10_000_000

    def test_played_true_when_play_count_nonzero(self, client):
        scene = make_scene(scene_id="123", play_count=1)
        encoded = encode_id("scene", "123")
        with patch("core.stash_client.get_scene", new=AsyncMock(return_value=scene)):
            data = client.post(f"/useritems/{encoded}/userdata").json()
        assert data["Played"] is True

    def test_favorite_via_rating_action(self, client):
        config.FAVORITE_ACTION = "rating"
        scene = make_scene(scene_id="123", rating100=80)
        encoded = encode_id("scene", "123")
        with patch("core.stash_client.get_scene", new=AsyncMock(return_value=scene)):
            data = client.post(f"/useritems/{encoded}/userdata").json()
        assert data["IsFavorite"] is True

    def test_not_favorite_when_rating_zero(self, client):
        config.FAVORITE_ACTION = "rating"
        scene = make_scene(scene_id="123", rating100=0)
        encoded = encode_id("scene", "123")
        with patch("core.stash_client.get_scene", new=AsyncMock(return_value=scene)):
            data = client.post(f"/useritems/{encoded}/userdata").json()
        assert data["IsFavorite"] is False

    def test_favorite_via_o_counter_action(self, client):
        config.FAVORITE_ACTION = "o_counter"
        scene = make_scene(scene_id="123", o_counter=2)
        encoded = encode_id("scene", "123")
        with patch("core.stash_client.get_scene", new=AsyncMock(return_value=scene)):
            data = client.post(f"/useritems/{encoded}/userdata").json()
        assert data["IsFavorite"] is True
