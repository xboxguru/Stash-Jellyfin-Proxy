"""
Unit tests for core/vertical.py (Feature 1 — Vertical Multi-View, Phase 0)

Covers:
  - is_vertical_scene: the orientation predicate (height > width AND
    height/width >= VERTICAL_ASPECT_MIN)
  - threshold boundaries, config-driven vs explicit aspect_min
  - missing / zero / malformed file dimensions
  - filter_vertical_scenes: list refinement helper
"""
import pytest
import config
from core.vertical import is_vertical_scene, filter_vertical_scenes
from tests.conftest import make_scene


class TestIsVerticalScene:
    """Predicate: portrait AND tall enough (default aspect_min = 1.3)."""

    @pytest.mark.parametrize("width,height", [
        (1080, 1920),   # 9:16 phone portrait (~1.78)
        (1440, 1920),   # 3:4 portrait (~1.33)
        (608, 1080),
    ])
    def test_vertical_scenes_pass(self, width, height):
        assert is_vertical_scene(make_scene(width=width, height=height)) is True

    @pytest.mark.parametrize("width,height", [
        (1920, 1080),   # 16:9 landscape
        (1080, 1080),   # square
        (1000, 1200),   # portrait but only 1.2 — below the 1.3 threshold
        (1080, 1079),   # near-square, landscape by a pixel
    ])
    def test_non_vertical_scenes_fail(self, width, height):
        assert is_vertical_scene(make_scene(width=width, height=height)) is False

    def test_exact_threshold_passes(self):
        # >= comparison: exactly aspect_min qualifies
        assert is_vertical_scene(make_scene(width=1000, height=1300)) is True

    def test_uses_config_threshold_by_default(self, monkeypatch):
        scene = make_scene(width=1000, height=1400)  # aspect 1.4
        monkeypatch.setattr(config, "VERTICAL_ASPECT_MIN", 1.5)
        assert is_vertical_scene(scene) is False
        monkeypatch.setattr(config, "VERTICAL_ASPECT_MIN", 1.3)
        assert is_vertical_scene(scene) is True

    def test_explicit_aspect_min_overrides_config(self, monkeypatch):
        monkeypatch.setattr(config, "VERTICAL_ASPECT_MIN", 1.3)
        scene = make_scene(width=1000, height=1400)
        assert is_vertical_scene(scene, aspect_min=1.5) is False

    def test_judges_primary_file_only(self):
        # Orientation follows files[0] (the primary file, used for playback)
        scene = make_scene(width=1080, height=1920)
        scene["files"].append({"width": 1920, "height": 1080})
        assert is_vertical_scene(scene) is True

    # Scenes whose dimensions can't be proven vertical are excluded
    def test_no_files_fails(self):
        scene = make_scene()
        scene["files"] = []
        assert is_vertical_scene(scene) is False

    def test_files_none_fails(self):
        scene = make_scene()
        scene["files"] = None
        assert is_vertical_scene(scene) is False

    @pytest.mark.parametrize("width,height", [
        (0, 1920),
        (1080, 0),
        (None, 1920),
        (1080, None),
    ])
    def test_missing_or_zero_dimensions_fail(self, width, height):
        assert is_vertical_scene(make_scene(width=width, height=height)) is False


class TestFilterVerticalScenes:
    def test_keeps_only_vertical(self):
        vertical = make_scene(scene_id="1", width=1080, height=1920)
        landscape = make_scene(scene_id="2", width=1920, height=1080)
        near_square = make_scene(scene_id="3", width=1000, height=1200)
        result = filter_vertical_scenes([vertical, landscape, near_square])
        assert result == [vertical]

    def test_empty_input(self):
        assert filter_vertical_scenes([]) == []
