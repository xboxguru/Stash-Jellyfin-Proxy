"""Vertical Multi-View ("Triptych") helpers — Feature 1.

Phase 0 ships the orientation predicate that decides which scenes belong in the
"Vertical Multi-View" library. Stash's scene filter can express the portrait half
of the predicate server-side (orientation: PORTRAIT == height > width), but has no
aspect-ratio criterion, so the "tall enough" half (height/width >= VERTICAL_ASPECT_MIN)
is applied here, client-side, on the scenes Stash returns.

Also home to `vdebug()`, the feature's verbose-diagnostics logger used by the
selection algorithm, the VOD compositor, and the Vertical TV channel feeder.
"""
import logging
import re

import config

_APIKEY_RE = re.compile(r"(apikey=)[^&\s\"']+", re.IGNORECASE)


def redact_apikey(text: str) -> str:
    """Masks a Stash ``apikey=...`` query param, e.g. in a logged FFmpeg command.

    FFmpeg command lines carry the Stash apikey verbatim in HTTP input URLs
    (``/scene/{id}/stream?apikey=...``); both the VOD compositor and the Live TV
    feeder log full command lines for diagnostics, so the key must be scrubbed
    before it hits the log file.
    """
    return _APIKEY_RE.sub(r"\1***REDACTED***", text)


def vdebug(logger: logging.Logger, msg: str) -> None:
    """Log a Vertical Multi-View diagnostic line, gated by ``VERTICAL_DEBUG``.

    With the flag on, the line is emitted at INFO so it shows up under the
    default ``LOG_LEVEL=INFO``; with it off, at DEBUG (visible only under a
    global ``LOG_LEVEL=DEBUG``).  Choosing the level per call is deliberate:
    hypercorn's serve() runs logging.config.dictConfig() at startup, which
    resets any per-logger setLevel() — so a "vertical loggers at DEBUG"
    approach would silently stop working (see main.py
    _SuppressLibraryDebugFilter for the same constraint).
    """
    level = logging.INFO if getattr(config, "VERTICAL_DEBUG", False) else logging.DEBUG
    logger.log(level, msg)


def _primary_dimensions(scene: dict) -> tuple[int, int]:
    """Returns (width, height) of the scene's primary file, or (0, 0) when unknown.

    Stash returns one entry per physical file; the first is the primary file, which
    is also what playback uses — so orientation is judged against it.
    """
    files = scene.get("files") or []
    if not files:
        return 0, 0
    width = files[0].get("width") or 0
    height = files[0].get("height") or 0
    return width, height


def is_vertical_scene(scene: dict, aspect_min: float = None, debug_log=None) -> bool:
    """True when the scene qualifies for the Vertical library.

    A scene is vertical when its primary file is portrait (height > width) AND
    tall enough (height/width >= aspect_min). The default threshold of 1.3 keeps
    9:16 phone portrait (~1.78) and 4:3 portrait (~1.33) while excluding square
    and near-square files. Scenes with missing or zero dimensions are excluded —
    we can't prove they're vertical.
    """
    if aspect_min is None:
        aspect_min = getattr(config, "VERTICAL_ASPECT_MIN", 1.3)
    width, height = _primary_dimensions(scene)
    scene_id = scene.get('id', '?')
    scene_title = scene.get('title', '?')[:40] if scene.get('title') else '?'
    if debug_log:
        debug_log(f"  checking {scene_id} {scene_title}: w={width} h={height}")
    if width <= 0 or height <= 0:
        if debug_log:
            debug_log(f"    → no dimensions")
        return False
    if height <= width:
        if debug_log:
            debug_log(f"    → not portrait")
        return False
    aspect = height / width
    passes = aspect >= aspect_min
    if debug_log:
        debug_log(f"    → aspect={aspect:.2f} (min={aspect_min}) {'✓ PASS' if passes else '✗ FAIL'}")
    return passes


def filter_vertical_scenes(scenes: list, debug=False) -> list:
    """Keeps only scenes passing is_vertical_scene(); refines Stash's PORTRAIT filter."""
    debug_log = None
    if debug:
        logger = logging.getLogger(__name__)
        debug_log = lambda msg: logger.debug(msg)
        debug_log(f"filter_vertical_scenes: checking {len(scenes)} scenes")
    result = [s for s in scenes if is_vertical_scene(s, debug_log=debug_log)]
    if debug:
        debug_log(f"filter_vertical_scenes: {len(scenes)} → {len(result)} scenes pass vertical filter")
    return result
