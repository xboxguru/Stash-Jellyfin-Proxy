"""Unit tests for core.hw_encoder — encoder selection + probe-fallback logic.

Covers:
  - resolve_h264_encoder: none/unknown → CPU (no probe), auto preference order,
    explicit-mode success + CPU fallback, and per-(mode, bin) caching.
  - probe_encoder: libx264 short-circuit, and command construction / exit-code
    handling for a hardware encoder (subprocess.run mocked — no real ffmpeg).
"""
import subprocess

import pytest

from core import hw_encoder
from core.hw_encoder import (
    CPU, EncoderConfig, _NVENC, _QSV, _VAAPI,
    probe_encoder, resolve_h264_encoder,
)


@pytest.fixture(autouse=True)
def _clear_cache():
    """Every test starts with an empty probe cache."""
    hw_encoder.clear_probe_cache()
    yield
    hw_encoder.clear_probe_cache()


def _fake_probe(available):
    """Build a probe stub that returns True only for encoders in `available`."""
    available = set(available)
    calls = []

    def probe(ffmpeg_bin, enc, timeout=20.0):
        calls.append(enc.codec)
        return enc.codec in available or not enc.is_hardware

    probe.calls = calls
    return probe


class TestResolveNoProbe:
    """Modes that resolve without touching the probe."""

    def test_none_is_cpu(self, monkeypatch):
        probe = _fake_probe([])
        monkeypatch.setattr(hw_encoder, "probe_encoder", probe)
        assert resolve_h264_encoder("none") is CPU
        assert probe.calls == []  # 'none' never probes

    def test_unknown_mode_is_cpu(self, monkeypatch):
        probe = _fake_probe(["h264_nvenc"])
        monkeypatch.setattr(hw_encoder, "probe_encoder", probe)
        assert resolve_h264_encoder("garbage") is CPU
        assert probe.calls == []

    def test_mode_is_case_insensitive(self, monkeypatch):
        monkeypatch.setattr(hw_encoder, "probe_encoder", _fake_probe([]))
        assert resolve_h264_encoder("NONE") is CPU


class TestResolveAuto:
    """`auto` walks NVENC → QSV → VAAPI and takes the first that probes OK."""

    def test_auto_prefers_nvenc(self, monkeypatch):
        probe = _fake_probe(["h264_nvenc", "h264_qsv", "h264_vaapi"])
        monkeypatch.setattr(hw_encoder, "probe_encoder", probe)
        assert resolve_h264_encoder("auto") is _NVENC
        assert probe.calls == ["h264_nvenc"]  # stops at the first success

    def test_auto_falls_through_to_qsv(self, monkeypatch):
        probe = _fake_probe(["h264_qsv", "h264_vaapi"])  # nvenc unavailable
        monkeypatch.setattr(hw_encoder, "probe_encoder", probe)
        assert resolve_h264_encoder("auto") is _QSV
        assert probe.calls == ["h264_nvenc", "h264_qsv"]

    def test_auto_falls_through_to_vaapi(self, monkeypatch):
        probe = _fake_probe(["h264_vaapi"])
        monkeypatch.setattr(hw_encoder, "probe_encoder", probe)
        assert resolve_h264_encoder("auto") is _VAAPI
        assert probe.calls == ["h264_nvenc", "h264_qsv", "h264_vaapi"]

    def test_auto_all_fail_falls_back_to_cpu(self, monkeypatch):
        probe = _fake_probe([])  # no hardware initializes
        monkeypatch.setattr(hw_encoder, "probe_encoder", probe)
        assert resolve_h264_encoder("auto") is CPU
        assert probe.calls == ["h264_nvenc", "h264_qsv", "h264_vaapi"]


class TestResolveExplicit:
    """An explicit encoder is used if it probes OK, else a CPU fallback."""

    @pytest.mark.parametrize("mode,expected", [
        ("nvenc", _NVENC), ("qsv", _QSV), ("vaapi", _VAAPI),
    ])
    def test_explicit_available(self, monkeypatch, mode, expected):
        monkeypatch.setattr(hw_encoder, "probe_encoder", _fake_probe([expected.codec]))
        assert resolve_h264_encoder(mode) is expected

    def test_explicit_unavailable_falls_back_to_cpu(self, monkeypatch):
        probe = _fake_probe([])  # nvenc requested but won't initialize
        monkeypatch.setattr(hw_encoder, "probe_encoder", probe)
        assert resolve_h264_encoder("nvenc") is CPU
        assert probe.calls == ["h264_nvenc"]  # only the requested encoder is tried

    def test_explicit_does_not_try_other_encoders(self, monkeypatch):
        # qsv available but nvenc requested → CPU, without ever probing qsv/vaapi.
        probe = _fake_probe(["h264_qsv", "h264_vaapi"])
        monkeypatch.setattr(hw_encoder, "probe_encoder", probe)
        assert resolve_h264_encoder("nvenc") is CPU
        assert probe.calls == ["h264_nvenc"]


class TestCaching:
    """Resolution is cached per (mode, ffmpeg_bin) so the probe runs once."""

    def test_second_call_hits_cache(self, monkeypatch):
        probe = _fake_probe(["h264_nvenc"])
        monkeypatch.setattr(hw_encoder, "probe_encoder", probe)
        first = resolve_h264_encoder("auto", "ffmpeg")
        second = resolve_h264_encoder("auto", "ffmpeg")
        assert first is second is _NVENC
        assert probe.calls == ["h264_nvenc"]  # not re-probed

    def test_different_binary_reprobes(self, monkeypatch):
        probe = _fake_probe(["h264_nvenc"])
        monkeypatch.setattr(hw_encoder, "probe_encoder", probe)
        resolve_h264_encoder("auto", "/usr/bin/ffmpeg")
        resolve_h264_encoder("auto", "/usr/lib/jellyfin-ffmpeg/ffmpeg")
        assert probe.calls == ["h264_nvenc", "h264_nvenc"]  # distinct cache keys

    def test_use_cache_false_reprobes(self, monkeypatch):
        probe = _fake_probe(["h264_nvenc"])
        monkeypatch.setattr(hw_encoder, "probe_encoder", probe)
        resolve_h264_encoder("auto")
        resolve_h264_encoder("auto", use_cache=False)
        assert probe.calls == ["h264_nvenc", "h264_nvenc"]


class TestProbeEncoder:
    """probe_encoder's own behavior (subprocess mocked)."""

    def test_libx264_never_shells_out(self, monkeypatch):
        def boom(*a, **k):
            raise AssertionError("subprocess.run should not be called for libx264")
        monkeypatch.setattr(subprocess, "run", boom)
        assert probe_encoder("ffmpeg", CPU) is True

    def test_hardware_probe_success(self, monkeypatch):
        captured = {}

        def fake_run(cmd, **kwargs):
            captured["cmd"] = cmd
            return subprocess.CompletedProcess(cmd, 0)
        monkeypatch.setattr(subprocess, "run", fake_run)

        assert probe_encoder("ffmpeg", _VAAPI) is True
        cmd = captured["cmd"]
        # device-init, the hwupload -vf, and the codec all appear in the probe cmd.
        assert "-vaapi_device" in cmd
        assert "-vf" in cmd and _VAAPI.vfilter in cmd
        assert "h264_vaapi" in cmd
        assert cmd[-2:] == ["-f", "null"] or cmd[-3:] == ["-f", "null", "-"]

    def test_hardware_probe_nonzero_exit_is_false(self, monkeypatch):
        monkeypatch.setattr(
            subprocess, "run",
            lambda cmd, **k: subprocess.CompletedProcess(cmd, 1),
        )
        assert probe_encoder("ffmpeg", _NVENC) is False

    def test_probe_swallows_oserror(self, monkeypatch):
        def raise_oserror(*a, **k):
            raise FileNotFoundError("no ffmpeg here")
        monkeypatch.setattr(subprocess, "run", raise_oserror)
        assert probe_encoder("ffmpeg", _NVENC) is False

    def test_probe_swallows_timeout(self, monkeypatch):
        def raise_timeout(*a, **k):
            raise subprocess.TimeoutExpired(cmd="ffmpeg", timeout=20.0)
        monkeypatch.setattr(subprocess, "run", raise_timeout)
        assert probe_encoder("ffmpeg", _QSV) is False


class TestEncoderConfig:
    """The dataclass invariants the master command relies on."""

    def test_cpu_is_not_hardware(self):
        assert CPU.is_hardware is False

    @pytest.mark.parametrize("enc", [_NVENC, _QSV, _VAAPI])
    def test_hardware_encoders_flagged(self, enc):
        assert enc.is_hardware is True

    def test_output_args_carry_codec(self):
        # output_args splice in place of `-c:v libx264 ...`, so they must set -c:v.
        for enc in (CPU, _NVENC, _QSV, _VAAPI):
            assert "-c:v" in enc.output_args
            assert enc.codec in enc.output_args

    def test_only_upload_encoders_have_a_filter(self):
        assert CPU.vfilter is None
        assert _NVENC.vfilter is None       # NVENC ingests system frames directly
        assert _QSV.vfilter is not None
        assert _VAAPI.vfilter is not None

    def test_frozen(self):
        with pytest.raises(Exception):
            CPU.codec = "x265"  # type: ignore[misc]
