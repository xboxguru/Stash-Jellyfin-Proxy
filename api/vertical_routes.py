"""HTTP surface for the Vertical Multi-View ("Triptych") compositor — Feature 1, Phase 1b.

Wires the play-session lifecycle to the compositor engine:
  • vertical_playback_info() — PlaybackInfo for a vertical-context item: selects
    sides, pre-warms the session, advertises a session-scoped HLS TranscodingUrl
    (direct play disabled).  Falls back to normal single-video playback when the
    concurrency cap is hit or the library is too small for sides.
  • redirect_to_composite() — guard for clients that build a /Videos/{id}/stream
    URL straight from the item id, bypassing PlaybackInfo (mirrors Live TV).
  • endpoint_vertical_manifest / _segment / _seek / _stop — the composite HLS
    manifest + segment proxy and explicit session seek/stop control.

Session ids are `{scene_id}-{nonce}`: the nonce makes sides fresh per play; the
scene id is recovered from the session id so a manifest request can rebuild the
session cold (e.g. after an idle teardown mid-play).
"""
import logging
import os
import re
import secrets

import config
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response, StreamingResponse

from core import stash_client, jellyfin_mapper
from core.jellyfin_mapper import decode_id
from core.vertical import vdebug
from api.vertical_engine import _vertical_manager

logger = logging.getLogger(__name__)


# ── helpers ──────────────────────────────────────────────────────────────────

# `{numeric scene id}-{8-hex nonce}` — everything a session endpoint accepts.
# Session ids come straight off the URL and end up in a log-file name and a
# temp-dir prefix, so reject anything that doesn't match what we mint.
_SESSION_ID_RE = re.compile(r"^\d+-[0-9a-f]{8}$")


def _new_session_id(raw_scene_id: str) -> str:
    """`{scene_id}-{nonce}` — fresh sides per play, stable across seeks within it."""
    return f"{raw_scene_id}-{secrets.token_hex(4)}"


def _valid_session_id(session_id: str) -> bool:
    return bool(_SESSION_ID_RE.match(session_id))


def _scene_id_from_session(session_id: str) -> str:
    """Recover the raw (numeric) scene id from a session id."""
    return session_id.split("-", 1)[0]


def _seek_seconds(request: Request, *, default: float | None = None) -> float | None:
    """Seek position (seconds) from a request's query params.

    Honors Jellyfin's `StartTimeTicks` (100 ns units) and the explicit-seek
    endpoint's `ticks` / `pos` params, case-insensitively.  Returns `default`
    (None unless overridden) when the request carries no position at all —
    callers must distinguish "no position given" from "seek to 0", otherwise a
    steady-state manifest poll would relaunch an explicitly-sought session
    back to 0 (see _VerticalSessionManager.ensure).
    """
    qp = request.query_params
    def _get(*names):
        for k, v in qp.items():
            if k.lower() in names:
                return v
        return None
    ticks = _get("starttimeticks", "ticks")
    if ticks is not None:
        try:
            return max(0.0, float(ticks) / 10_000_000.0)
        except ValueError:
            pass
    pos = _get("pos", "position", "seconds")
    if pos is not None:
        try:
            return max(0.0, float(pos))
        except ValueError:
            pass
    return default


def _composite_media_source(session_id: str, scene: dict) -> dict:
    """MediaSource advertising the triptych compositor as a finite HLS VOD.

    Direct play is disabled and an HLS TranscodingUrl is advertised so the client
    uses hls.js / ExoPlayer's HlsMediaSource pointed at our composite manifest —
    the same wiring proven for Live TV, but finite (RunTimeTicks = center clip
    length) so the client shows a scrubbable timeline for center seeking.
    """
    files = scene.get("files") or []
    duration = float(files[0].get("duration") or 0) if files else 0.0
    runtime_ticks = int(duration * 10_000_000)
    transcode_url = f"/vertical/{session_id}/master.m3u8"
    name = scene.get("title") or f"Scene {scene.get('id')}"
    return {
        "Protocol": "Http",
        "Id": session_id,
        "Path": transcode_url,
        "Type": "Default",
        "Name": name,
        "IsRemote": False,
        "RunTimeTicks": runtime_ticks,
        "ReadAtNativeFramerate": True,
        "IgnoreDts": False,
        "IgnoreIndex": False,
        "GenPtsInput": False,
        "SupportsTranscoding": True,
        "SupportsDirectStream": False,
        "SupportsDirectPlay": False,
        "IsInfiniteStream": False,
        "IsLive": False,
        "UseMostCompatibleTranscodingProfile": True,
        "RequiresOpening": False,
        "RequiresClosing": False,
        "RequiresLooping": False,
        "SupportsProbing": False,
        "TranscodingUrl": transcode_url,
        "TranscodingSubProtocol": "hls",
        "TranscodingContainer": "ts",
        "MediaStreams": [
            {"VideoRange": "SDR", "VideoRangeType": "SDR", "AudioSpatialFormat": "None",
             "DisplayTitle": "SDR", "IsInterlaced": False, "IsDefault": True,
             "IsForced": False, "IsHearingImpaired": False, "Type": "Video", "Index": 0,
             "IsExternal": False, "IsTextSubtitleStream": False, "SupportsExternalStream": False},
            {"VideoRange": "Unknown", "VideoRangeType": "Unknown", "AudioSpatialFormat": "None",
             "DisplayTitle": "", "IsInterlaced": False, "IsDefault": False,
             "IsForced": False, "IsHearingImpaired": False, "Type": "Audio", "Index": 1,
             "IsExternal": False, "IsTextSubtitleStream": False, "SupportsExternalStream": False},
        ],
        "MediaAttachments": [],
        "Formats": [],
        "RequiredHttpHeaders": {},
        "HasSegments": False,
    }


# ── PlaybackInfo (called from stream_routes.endpoint_playback_info) ───────────

async def vertical_playback_info(scene: dict, raw_id: str, request: Request) -> JSONResponse:
    """Advertise the triptych compositor for a vertical-context item.

    Pre-warms an FFmpeg session (so segments are ready by the manifest fetch) and
    advertises the session-scoped HLS URL.  If the session can't start (cap hit,
    or the library has no other vertical clips for sides), falls back to normal
    single-video playback and logs why.
    """
    session_id = _new_session_id(raw_id)
    vdebug(logger, f"Vertical: PlaybackInfo pre-warming session {session_id!r} for scene {raw_id}")
    ok = await _vertical_manager.ensure(session_id, scene, 0.0)
    if not ok:
        logger.info(
            f"Vertical: PlaybackInfo for scene {raw_id} — compositor unavailable "
            f"(cap or no sides); falling back to single-video playback"
        )
        item = jellyfin_mapper.format_jellyfin_item(scene)
        return JSONResponse({
            "MediaSources": item.get("MediaSources", []),
            "PlaySessionId": f"stash_{raw_id}",
        })

    _vertical_manager.touch(session_id)
    logger.info(f"Vertical: PlaybackInfo scene {raw_id} → composite session {session_id!r}")
    return JSONResponse({
        "MediaSources": [_composite_media_source(session_id, scene)],
        "PlaySessionId": session_id,
    })


async def redirect_to_composite(raw_item_id: str, request: Request) -> Response:
    """Guard for /Videos/{vscene-id}/stream|master.m3u8 built straight from the id.

    Mints a session and 302-redirects to its composite manifest, mirroring the
    Live TV channel guard in endpoint_stream.  PlaybackInfo is the normal path;
    this only fires for clients that skip it.
    """
    raw_id = decode_id(raw_item_id).replace("scene-", "")
    scene = await stash_client.get_scene(raw_id)
    if not scene:
        return Response(status_code=404)
    session_id = _new_session_id(raw_id)
    seek = _seek_seconds(request)
    vdebug(logger, f"Vertical: stream guard for scene {raw_id} — minting session {session_id!r} seek={seek}")
    ok = await _vertical_manager.ensure(session_id, scene, seek)
    if not ok:
        # Compositor unavailable — fall through to a normal scene stream so the
        # client still plays something (single-video fallback).
        stash_base = config.get_stash_base()
        apikey = getattr(config, "STASH_API_KEY", "")
        url = f"{stash_base}/scene/{raw_id}/stream" + (f"?apikey={apikey}" if apikey else "")
        logger.info(f"Vertical: stream guard for scene {raw_id} — compositor unavailable, redirecting to raw stream")
        return RedirectResponse(url=url, status_code=302)
    _vertical_manager.touch(session_id)
    target = f"/vertical/{session_id}/master.m3u8"
    logger.info(f"Vertical: stream guard scene {raw_id} → redirect {target}")
    return RedirectResponse(url=target, status_code=302)


# ── composite HLS manifest + segments ─────────────────────────────────────────

async def endpoint_vertical_manifest(request: Request) -> Response:
    """Serve the composite HLS manifest, creating/relaunching the session as needed.

    A `StartTimeTicks` on the request that differs from the session's current
    center position triggers a full relaunch with the new `-ss` (§1.5 center seek).
    """
    session_id = request.path_params.get("session_id", "")
    if not _valid_session_id(session_id):
        logger.warning(f"Vertical: manifest request with malformed session id {session_id!r}")
        return Response(status_code=404)
    raw_scene_id = _scene_id_from_session(session_id)
    scene = await stash_client.get_scene(raw_scene_id)
    if not scene:
        logger.warning(f"Vertical: manifest for unknown scene in session {session_id!r}")
        return Response(status_code=404)

    seek = _seek_seconds(request)  # None = no position given → never resets the session
    was_alive = _vertical_manager.is_alive(session_id)
    ok = await _vertical_manager.ensure(session_id, scene, seek)
    if not ok:
        logger.warning(f"Vertical: manifest for session {session_id!r} — compositor unavailable (503)")
        return Response(status_code=503, content="Vertical compositor unavailable")
    if not was_alive:
        # Cold (re)build from a manifest request — e.g. first fetch after the
        # stream-guard redirect, or a client resuming after an idle teardown.
        vdebug(logger, f"Vertical: manifest request (re)built session {session_id!r} seek={seek}")

    _vertical_manager.touch(session_id)
    manifest_path = _vertical_manager.manifest_path(session_id)
    if not manifest_path:
        logger.warning(f"Vertical: session {session_id!r} alive but manifest file missing (502)")
        return Response(status_code=502)
    try:
        with open(manifest_path, "r", encoding="utf-8") as fh:
            raw = fh.read()
    except OSError as exc:
        logger.warning(f"Vertical: could not read manifest for session {session_id!r}: {exc} (502)")
        return Response(status_code=502)

    # Rewrite relative segment filenames → absolute URLs through our proxy.
    base = f"{request.url.scheme}://{request.url.netloc}"
    out_lines = []
    for line in raw.splitlines():
        s = line.strip()
        if s and not s.startswith("#"):
            out_lines.append(f"{base}/vertical/{session_id}/seg/{s}")
        else:
            out_lines.append(line)

    logger.trace(f"Vertical: served composite manifest for session {session_id!r} seek={seek}")
    return Response(
        "\n".join(out_lines),
        media_type="application/vnd.apple.mpegurl",
        headers={"Cache-Control": "no-cache, no-store", "Access-Control-Allow-Origin": "*"},
    )


async def endpoint_vertical_segment(request: Request) -> Response:
    """Serve one composite HLS segment from the session's temp dir."""
    session_id = request.path_params.get("session_id", "")
    seg_name = request.path_params.get("seg_name", "")
    if not re.match(r"^seg\d+\.ts$", seg_name):
        return Response(status_code=400)

    seg_dir = _vertical_manager.seg_dir(session_id)
    if not seg_dir:
        # Session already torn down (or never existed) — the client is fetching
        # from a stale manifest.  Worth a warning: mid-play this means the idle
        # watchdog or an explicit stop beat the client to it.
        logger.warning(f"Vertical: segment {seg_name} requested for unknown/stopped session {session_id!r}")
        return Response(status_code=404)
    seg_path = os.path.join(seg_dir, seg_name)
    if not os.path.exists(seg_path):
        logger.warning(
            f"Vertical: segment {seg_name} not on disk for session {session_id!r} "
            f"(alive={_vertical_manager.is_alive(session_id)}) — client ahead of encoder or stale manifest"
        )
        return Response(status_code=404)

    _vertical_manager.touch(session_id)

    async def _iter():
        with open(seg_path, "rb") as fh:
            while chunk := fh.read(65536):
                yield chunk

    return StreamingResponse(
        _iter(), media_type="video/mp2t",
        headers={"Access-Control-Allow-Origin": "*"},
    )


async def endpoint_vertical_seek(request: Request) -> Response:
    """Explicit center seek — relaunch the composite at a new position (sides loop on)."""
    session_id = request.path_params.get("session_id", "")
    if not _valid_session_id(session_id):
        logger.warning(f"Vertical: seek request with malformed session id {session_id!r}")
        return Response(status_code=404)
    raw_scene_id = _scene_id_from_session(session_id)
    scene = await stash_client.get_scene(raw_scene_id)
    if not scene:
        return Response(status_code=404)
    position = _seek_seconds(request, default=0.0)  # explicit endpoint: no position = restart at 0
    ok = await _vertical_manager.seek(session_id, scene, position)
    if not ok:
        logger.warning(f"Vertical: explicit seek to {position:.1f}s failed for session {session_id!r} (503)")
        return Response(status_code=503)
    _vertical_manager.touch(session_id)
    return Response(status_code=204)


async def endpoint_vertical_stop(request: Request) -> Response:
    """Explicit teardown of a composite session."""
    session_id = request.path_params.get("session_id", "")
    logger.info(f"Vertical: explicit stop for session {session_id!r}")
    await _vertical_manager.stop(session_id)
    return Response(status_code=204)
