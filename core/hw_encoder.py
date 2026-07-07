"""Hardware H.264 encoder selection + startup probe (shared FFmpeg helper).

A single, framework-free place to turn a ``*_HWACCEL`` setting
(``none | nvenc | qsv | vaapi | auto``) into a concrete, **verified-working**
FFmpeg video-encoder configuration, falling back to CPU (``libx264``) and logging
the effective choice.  Vertical Multi-View uses it today; Live TV can adopt the
same helper (DRY) when it grows a hardware path — hence this lives in ``core/``
and its log/label wording is engine-agnostic.

Encoder-only, decode stays on CPU
---------------------------------
This phase keeps decode + scaling + ``hstack`` on the CPU (avoids pulling the CUDA
*runtime*; see ``docs/Triptych.md`` → Hardware encoding).  The frames handed to the
encoder are already plain ``yuv420p`` in system memory, so we only need to swap the
final ``-c:v`` encoder — no ``-hwaccel`` decode flags.  The two GPU families that
can't ingest system frames directly (VAAPI, QSV) get a small ``hwupload`` video
filter + device-init so the *upload* happens on the GPU; NVENC and libx264 take
system frames as-is.

The probe is a real one-frame test-encode (``ffmpeg -f lavfi -i color ... -f null``)
rather than a mere ``-encoders`` string match: an encoder can be *compiled in* yet
fail to *initialize* (no GPU, no ``/dev/dri``, missing driver).  Only an encoder that
actually produced a frame is selected; everything else falls back to ``libx264``.
Results are cached per ``(mode, ffmpeg_bin)`` so the probe runs once (at startup).
"""
import logging
import subprocess
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class EncoderConfig:
    """A complete, spliceable FFmpeg H.264 encoder configuration.

    The three arg groups slot into a master command at their natural positions:
    ``input_args`` before the inputs (global/device init), ``vfilter`` as the
    ``-vf`` chain (GPU upload for VAAPI/QSV), and ``output_args`` in place of the
    old hard-coded ``-c:v libx264 ...`` block (codec + rate control).
    """
    codec: str                                    # -c:v value, also the log label
    input_args: tuple[str, ...] = ()              # device/global init, before the inputs
    vfilter: str | None = None                    # -vf chain to upload CPU frames to the GPU
    output_args: tuple[str, ...] = field(default_factory=tuple)  # full -c:v ... rate-control block

    @property
    def is_hardware(self) -> bool:
        return self.codec != "libx264"


# ── Encoder table ──────────────────────────────────────────────────────────────
# Rate control is tuned to roughly match the CPU baseline's CRF 23 / veryfast:
# a quality-targeted mode near visually-lossless-enough for this compositor.

CPU = EncoderConfig(
    codec="libx264",
    output_args=("-c:v", "libx264", "-preset", "veryfast", "-crf", "23"),
)

_NVENC = EncoderConfig(
    codec="h264_nvenc",  # ingests system frames directly — no upload filter needed
    input_args=("-init_hw_device", "cuda=cu:0"),
    output_args=("-c:v", "h264_nvenc", "-preset", "p5", "-rc", "vbr", "-cq", "23"),
)

_QSV = EncoderConfig(
    codec="h264_qsv",
    input_args=("-init_hw_device", "qsv=hw", "-filter_hw_device", "hw"),
    vfilter="hwupload=extra_hw_frames=64,format=qsv",
    output_args=("-c:v", "h264_qsv", "-preset", "veryfast", "-global_quality", "23"),
)

_VAAPI = EncoderConfig(
    codec="h264_vaapi",
    input_args=("-vaapi_device", "/dev/dri/renderD128"),
    vfilter="format=nv12,hwupload",
    output_args=("-c:v", "h264_vaapi", "-qp", "23"),
)

# Explicit-mode lookup, and the preference order for `auto` (NVENC → QSV → VAAPI).
_BY_NAME: dict[str, EncoderConfig] = {"nvenc": _NVENC, "qsv": _QSV, "vaapi": _VAAPI}
_AUTO_ORDER: tuple[EncoderConfig, ...] = (_NVENC, _QSV, _VAAPI)

# Cache: (mode, ffmpeg_bin) → resolved EncoderConfig.  Probing is idempotent, so
# the first resolution (typically the startup probe) is reused by every launch.
_CACHE: dict[tuple[str, str], EncoderConfig] = {}


def probe_encoder(ffmpeg_bin: str, enc: EncoderConfig, timeout: float = 20.0) -> bool:
    """Return True iff ``enc`` can encode one real frame with this ffmpeg binary.

    ``libx264`` is always available (no GPU state), so it short-circuits.  For a
    hardware encoder we run the *same* device-init + upload-filter + codec args the
    real master will use, against a tiny synthetic source, and check for a clean
    exit — the definitive "will it actually initialize here?" test.
    """
    if not enc.is_hardware:
        return True
    cmd = [
        ffmpeg_bin, "-hide_banner", "-nostats", "-loglevel", "error",
        *enc.input_args,
        "-f", "lavfi", "-i", "color=c=black:s=320x240:r=30",
        "-frames:v", "1",
    ]
    if enc.vfilter:
        cmd += ["-vf", enc.vfilter]
    cmd += [*enc.output_args, "-f", "null", "-"]
    try:
        proc = subprocess.run(
            cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=timeout,
        )
        return proc.returncode == 0
    except (OSError, subprocess.SubprocessError) as exc:
        logger.debug(f"H.264 encoder probe for {enc.codec} errored: {exc}")
        return False


def resolve_h264_encoder(mode: str, ffmpeg_bin: str = "ffmpeg",
                         *, use_cache: bool = True) -> EncoderConfig:
    """Resolve a ``*_HWACCEL`` mode to a working encoder, falling back to CPU.

    ``none`` → libx264 with no probe.  ``auto`` → first of NVENC/QSV/VAAPI that
    probes OK, else libx264.  An explicit ``nvenc``/``qsv``/``vaapi`` → that
    encoder if it probes OK, else libx264 (logged as a fallback).  Unknown modes
    → libx264.  The effective choice is logged once and cached per
    ``(mode, ffmpeg_bin)``.
    """
    key = (str(mode).lower(), ffmpeg_bin)
    if use_cache and key in _CACHE:
        return _CACHE[key]
    result = _resolve(key[0], ffmpeg_bin)
    _CACHE[key] = result
    return result


def _resolve(mode: str, ffmpeg_bin: str) -> EncoderConfig:
    if mode in ("none", "cpu", "libx264"):
        logger.info("H.264 encoder: 'none' → libx264 (CPU); no hardware probe")
        return CPU

    if mode == "auto":
        candidates = _AUTO_ORDER
    elif mode in _BY_NAME:
        candidates = (_BY_NAME[mode],)
    else:
        logger.warning(f"H.264 encoder: unknown HWACCEL {mode!r} — using libx264 (CPU)")
        return CPU

    for cand in candidates:
        if probe_encoder(ffmpeg_bin, cand):
            logger.info(f"H.264 encoder: {mode!r} → {cand.codec} (hardware probe OK)")
            return cand
        logger.info(f"H.264 encoder: {cand.codec} unavailable (probe failed)")

    logger.warning(
        f"H.264 encoder: no hardware encoder available for {mode!r} "
        f"— falling back to libx264 (CPU)"
    )
    return CPU


def clear_probe_cache() -> None:
    """Drop cached probe results (e.g. after a config/hardware change, or in tests)."""
    _CACHE.clear()
