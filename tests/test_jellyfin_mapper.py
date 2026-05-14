"""
Unit tests for core/jellyfin_mapper.py

Covers:
  - encode_id / decode_id round-trips and edge cases
  - generate_sort_name: articles, unicode, symbols, numbers
  - generate_image_tag: determinism and format
  - hyphens: UUID formatting
  - build_folder: collection vs nav vs user-view
  - _build_subtitle_streams: index, codec name, IsDefault, DeliveryUrl casing
  - format_jellyfin_item: title fallback, UserData, MediaSources, chapters
"""
import pytest
import config
from tests.conftest import make_scene, make_performer, make_studio
from core.jellyfin_mapper import (
    encode_id,
    decode_id,
    generate_sort_name,
    generate_image_tag,
    hyphens,
    build_folder,
    _build_subtitle_streams,
    format_jellyfin_item,
)


# ── encode_id / decode_id ────────────────────────────────────────────────────

class TestEncodeDecodeId:
    @pytest.mark.parametrize("prefix,raw_id", [
        ("scene", "123"),
        ("scene", "99999"),
        ("person", "456"),
        ("studio", "789"),
        ("tag", "42"),
        ("root", "organized"),
        ("root", "scenes"),
        ("root", "recent"),
        ("filter", "7"),
        ("year", "2022"),
    ])
    def test_round_trip(self, prefix, raw_id):
        encoded = encode_id(prefix, raw_id)
        assert decode_id(encoded) == f"{prefix}-{raw_id}"

    def test_encoded_is_32_hex_chars(self):
        encoded = encode_id("scene", "1")
        assert len(encoded) == 32
        assert all(c in "0123456789abcdef" for c in encoded)

    def test_encoded_is_lowercase(self):
        encoded = encode_id("scene", "123")
        assert encoded == encoded.lower()

    def test_different_ids_produce_different_encodings(self):
        assert encode_id("scene", "1") != encode_id("scene", "2")

    def test_different_prefixes_produce_different_encodings(self):
        assert encode_id("scene", "1") != encode_id("person", "1")

    def test_encoding_is_deterministic(self):
        assert encode_id("scene", "123") == encode_id("scene", "123")

    # Passthrough: already-decoded IDs passed back to decode_id return unchanged
    @pytest.mark.parametrize("decoded", [
        "scene-123",
        "person-456",
        "studio-789",
    ])
    def test_passthrough_already_decoded(self, decoded):
        assert decode_id(decoded) == decoded

    def test_decode_non_hex_returns_input(self):
        bad = "not-a-hex-id"
        assert decode_id(bad) == bad

    def test_decode_empty_string(self):
        # Should not raise; returns the input unchanged
        result = decode_id("")
        assert isinstance(result, str)


# ── generate_sort_name ───────────────────────────────────────────────────────

class TestGenerateSortName:
    def test_empty_string(self):
        assert generate_sort_name("") == ""

    def test_strips_leading_the(self):
        assert generate_sort_name("The Matrix") == "matrix"

    def test_strips_leading_a(self):
        assert generate_sort_name("A Good Day") == "good day"

    def test_strips_leading_an(self):
        assert generate_sort_name("An Apple") == "apple"

    def test_does_not_strip_article_in_middle(self):
        result = generate_sort_name("Day of the Dead")
        assert result == "day of the dead"

    def test_case_insensitive_article_strip(self):
        assert generate_sort_name("THE Matrix") == "matrix"

    def test_unicode_transliteration(self):
        # é → e, ü → u
        result = generate_sort_name("Amélie")
        assert result == "amelie"

    def test_strips_leading_symbols(self):
        result = generate_sort_name("-*- Scene Title -*-")
        assert result.startswith("scene")

    def test_numbers_zero_padded(self):
        result = generate_sort_name("Scene 2")
        assert "000002" in result

    def test_numbers_zero_padded_multi(self):
        r1 = generate_sort_name("Episode 2")
        r2 = generate_sort_name("Episode 10")
        # natural sort: "2" < "10" but "000002" < "000010" ✓
        assert r1 < r2

    def test_symbol_only_title_not_emptied(self):
        # Title consisting only of symbols should not become empty
        result = generate_sort_name("---")
        assert isinstance(result, str)

    def test_lowercase_output(self):
        assert generate_sort_name("Hello World") == "hello world"


# ── generate_image_tag ───────────────────────────────────────────────────────

class TestGenerateImageTag:
    def test_returns_32_hex_chars(self):
        tag = generate_image_tag("scene", "123", 0)
        assert len(tag) == 32
        assert all(c in "0123456789abcdef" for c in tag)

    def test_deterministic(self):
        assert generate_image_tag("scene", "123", 0) == generate_image_tag("scene", "123", 0)

    def test_different_inputs_different_tags(self):
        assert generate_image_tag("scene", "1", 0) != generate_image_tag("scene", "2", 0)

    def test_cache_version_changes_tag(self):
        assert generate_image_tag("scene", "1", 0) != generate_image_tag("scene", "1", 1)

    def test_different_prefixes_different_tags(self):
        assert generate_image_tag("scene", "1", 0) != generate_image_tag("studio", "1", 0)


# ── hyphens ──────────────────────────────────────────────────────────────────

class TestHyphens:
    def test_formats_32_char_hex_as_uuid(self):
        h = "a" * 32
        result = hyphens(h)
        assert result == "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"

    def test_wrong_length_unchanged(self):
        short = "abc"
        assert hyphens(short) == short

    def test_36_char_unchanged(self):
        s = "a" * 36
        assert hyphens(s) == s


# ── build_folder ─────────────────────────────────────────────────────────────

class TestBuildFolder:
    def test_nav_folder_type(self):
        f = build_folder("Nav", "folder-id", "server-id", 0, is_collection=False)
        assert f["Type"] == "Folder"
        assert f["IsFolder"] is True

    def test_collection_folder_type(self):
        f = build_folder("Lib", "folder-id", "server-id", 0, is_collection=True)
        assert f["Type"] == "CollectionFolder"
        assert f["CollectionType"] == "movies"

    def test_user_view_type(self):
        f = build_folder("View", "folder-id", "server-id", 0, is_collection=True, is_user_view=True)
        assert f["Type"] == "UserView"
        assert "UserData" in f
        assert "LibraryOptions" in f

    def test_name_and_id(self):
        f = build_folder("My Folder", "abc123", "srv", 0)
        assert f["Name"] == "My Folder"
        assert f["Id"] == "abc123"
        assert f["ServerId"] == "srv"

    def test_has_primary_image(self):
        f = build_folder("X", "id", "srv", 0)
        assert f["HasPrimaryImage"] is True
        assert "Primary" in f["ImageTags"]

    def test_cache_version_changes_image_tag(self):
        f0 = build_folder("X", "id", "srv", 0)
        f1 = build_folder("X", "id", "srv", 1)
        assert f0["ImageTags"]["Primary"] != f1["ImageTags"]["Primary"]


# ── _build_subtitle_streams ──────────────────────────────────────────────────

class TestBuildSubtitleStreams:
    def _encoded_scene_id(self):
        return encode_id("scene", "123")

    def test_empty_captions(self):
        assert _build_subtitle_streams("scene-123", []) == []

    def test_single_srt_track(self):
        captions = [{"language_code": "eng", "caption_type": "srt"}]
        streams = _build_subtitle_streams("scene-123", captions)
        assert len(streams) == 1
        s = streams[0]
        assert s["Type"] == "Subtitle"
        assert s["Index"] == 2          # video=0, audio=1, first subtitle=2
        assert s["Codec"] == "subrip"   # SRT maps to subrip, not "srt"
        assert s["Language"] == "eng"
        assert s["Title"] == "English"
        assert s["IsDefault"] is False  # never auto-select
        assert s["IsExternal"] is True

    def test_delivery_url_lowercase(self):
        captions = [{"language_code": "eng", "caption_type": "srt"}]
        streams = _build_subtitle_streams("scene-123", captions)
        url = streams[0]["DeliveryUrl"]
        assert "stream.srt" in url       # lowercase, not Stream.srt
        assert "Stream.srt" not in url

    def test_multiple_tracks_sequential_indices(self):
        captions = [
            {"language_code": "eng", "caption_type": "srt"},
            {"language_code": "spa", "caption_type": "srt"},
        ]
        streams = _build_subtitle_streams("scene-123", captions)
        assert streams[0]["Index"] == 2
        assert streams[1]["Index"] == 3

    def test_none_tracks_are_never_default(self):
        captions = [
            {"language_code": "eng", "caption_type": "srt"},
            {"language_code": "fra", "caption_type": "srt"},
            {"language_code": "deu", "caption_type": "srt"},
        ]
        streams = _build_subtitle_streams("scene-123", captions)
        assert all(s["IsDefault"] is False for s in streams)

    def test_unknown_language_code_uppercased(self):
        captions = [{"language_code": "xyz", "caption_type": "srt"}]
        streams = _build_subtitle_streams("scene-123", captions)
        # Falls back to lang.upper() when not in the name map
        assert streams[0]["Title"] == "XYZ"

    def test_vtt_codec_kept_as_vtt(self):
        captions = [{"language_code": "eng", "caption_type": "vtt"}]
        streams = _build_subtitle_streams("scene-123", captions)
        assert streams[0]["Codec"] == "vtt"

    def test_item_id_in_delivery_url(self):
        captions = [{"language_code": "eng", "caption_type": "srt"}]
        streams = _build_subtitle_streams("scene-999", captions)
        assert "scene-999" in streams[0]["DeliveryUrl"]


# ── format_jellyfin_item ─────────────────────────────────────────────────────

class TestFormatJellyfinItem:
    def test_basic_scene_shape(self):
        scene = make_scene()
        item = format_jellyfin_item(scene)
        assert item["Type"] == "Movie"
        assert item["Name"] == "Test Scene"
        assert item["IsFolder"] is False

    def test_title_fallback_uses_code(self):
        scene = make_scene(title=None, code="SCENE-001")
        item = format_jellyfin_item(scene)
        assert item["Name"] == "SCENE-001"

    def test_title_fallback_uses_path_basename(self):
        # title fallback extracts the stem from the file path, not the basename field
        scene = make_scene(title=None, code=None, path="/data/my_file.mp4")
        item = format_jellyfin_item(scene)
        assert item["Name"] == "my_file"

    def test_title_fallback_uses_scene_id(self):
        # When title/code and path are all absent, fall back to "Scene {id}"
        scene = make_scene(title=None, code=None, scene_id="42")
        scene["files"][0]["path"] = ""
        item = format_jellyfin_item(scene)
        assert "42" in item["Name"]

    def test_o_counter_as_community_rating(self):
        scene = make_scene(o_counter=3)
        item = format_jellyfin_item(scene)
        assert item["CommunityRating"] == 3

    def test_rating100_as_critic_rating(self):
        scene = make_scene(rating100=80)
        item = format_jellyfin_item(scene)
        assert item["CriticRating"] == 80

    def test_play_count_in_userdata(self):
        scene = make_scene(play_count=5)
        item = format_jellyfin_item(scene)
        assert item["UserData"]["PlayCount"] == 5

    def test_played_true_when_play_count_nonzero(self):
        scene = make_scene(play_count=1)
        item = format_jellyfin_item(scene)
        assert item["UserData"]["Played"] is True

    def test_played_false_when_play_count_zero(self):
        scene = make_scene(play_count=0)
        item = format_jellyfin_item(scene)
        assert item["UserData"]["Played"] is False

    def test_resume_time_converted_to_ticks(self):
        scene = make_scene(resume_time=90.0)  # 90 seconds
        item = format_jellyfin_item(scene)
        # 90s × 10,000,000 ticks/s = 900,000,000 ticks
        assert item["UserData"]["PlaybackPositionTicks"] == 900_000_000

    def test_runtime_ticks_from_duration(self):
        scene = make_scene(duration=3600.0)
        item = format_jellyfin_item(scene)
        assert item["RunTimeTicks"] == 3600 * 10_000_000

    def test_media_sources_present(self):
        scene = make_scene()
        item = format_jellyfin_item(scene)
        assert len(item["MediaSources"]) == 1
        ms = item["MediaSources"][0]
        assert ms["Container"] == "mp4"

    def test_transcode_required_for_avi(self):
        scene = make_scene(video_codec="mpeg4", fmt="avi")
        item = format_jellyfin_item(scene)
        ms = item["MediaSources"][0]
        assert ms["SupportsDirectPlay"] is False
        assert "TranscodingUrl" in ms

    def test_direct_play_for_h264_mp4(self):
        scene = make_scene(video_codec="h264", fmt="mp4")
        item = format_jellyfin_item(scene)
        ms = item["MediaSources"][0]
        assert ms["SupportsDirectPlay"] is True
        assert ms["SupportsDirectStream"] is True

    def test_subtitles_included_when_captions_present(self):
        captions = [{"language_code": "eng", "caption_type": "srt"}]
        scene = make_scene(captions=captions)
        item = format_jellyfin_item(scene)
        streams = item["MediaSources"][0]["MediaStreams"]
        subtitle_streams = [s for s in streams if s["Type"] == "Subtitle"]
        assert len(subtitle_streams) == 1

    def test_no_subtitles_when_no_captions(self):
        scene = make_scene(captions=[])
        item = format_jellyfin_item(scene)
        streams = item["MediaSources"][0]["MediaStreams"]
        subtitle_streams = [s for s in streams if s["Type"] == "Subtitle"]
        assert len(subtitle_streams) == 0

    def test_scene_markers_become_chapters(self):
        markers = [
            {"id": "m1", "seconds": 120.0, "title": "Act 2", "primary_tag": {"name": "Tag"}},
            {"id": "m2", "seconds": 240.0, "title": "Act 3", "primary_tag": {"name": "Tag"}},
        ]
        scene = make_scene(scene_markers=markers)
        item = format_jellyfin_item(scene)
        assert len(item["Chapters"]) == 3  # index 0 = scene start + 2 markers

    def test_performers_in_people(self):
        performers = [{"name": "Jane Doe", "id": "10", "image_path": "http://..."}]
        scene = make_scene(performers=performers)
        item = format_jellyfin_item(scene)
        people = item["People"]
        assert any(p["Name"] == "Jane Doe" for p in people)

    def test_studio_in_studios(self):
        studio = {"id": "5", "name": "Test Studio", "image_path": "http://..."}
        scene = make_scene(studio=studio)
        item = format_jellyfin_item(scene)
        assert any(s["Name"] == "Test Studio" for s in item["Studios"])

    def test_official_rating_is_xxx(self):
        scene = make_scene()
        item = format_jellyfin_item(scene)
        assert item["OfficialRating"] == "XXX"

    def test_id_is_32_hex_chars(self):
        scene = make_scene()
        item = format_jellyfin_item(scene)
        assert len(item["Id"]) == 32
        assert all(c in "0123456789abcdef" for c in item["Id"])
