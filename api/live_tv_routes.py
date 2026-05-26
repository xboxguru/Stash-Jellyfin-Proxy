import asyncio
import glob
import hashlib
import json
import logging
import mimetypes
import os
import platform
import random
import re
import shutil
import socket
import tempfile
import time
import xml.etree.ElementTree as ET
try:
    import fcntl  # Linux only — used to bump FIFO buffer size in playout
except ImportError:
    fcntl = None  # type: ignore

_IS_WINDOWS = platform.system().lower().startswith("win")
_HAS_MKFIFO = hasattr(os, "mkfifo")
from datetime import date as _date_cls
from datetime import datetime, timedelta, timezone

import httpx
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from starlette.background import BackgroundTask

import config
from core.jellyfin_mapper import encode_id

logger = logging.getLogger(__name__)

_live_client = httpx.AsyncClient(timeout=15.0, follow_redirects=True)
CACHE_TTL = 300           # 5-min TTL for M3U/XMLTV/channel-list caches
_SCHEDULE_TTL = 86400.0   # rebuild Stash schedules every 24 h

_m3u_cache: dict = {"data": None, "ts": 0.0}
_xmltv_cache: dict = {"data": None, "ts": 0.0}

_channel_stream_map: dict[str, str] = {}   # encoded_id -> stream_url
_channel_info_map: dict[str, dict] = {}    # encoded_id -> raw channel dict
_channel_tvgid_map: dict[str, str] = {}    # encoded_id (dashless) -> tvg_id (reverse lookup for MD5 hashes)
_program_info_map: dict[str, dict] = {}    # encoded_id -> raw program dict (Tunarr/XMLTV; cleared on every XMLTV refresh)
_program_tvgkey_map: dict[str, tuple] = {} # encoded_id (dashless) -> (channel_id, start) for MD5 hash reversal
_stash_program_map: dict[str, dict] = {}   # encoded_id -> raw program dict (Stash only; never cleared by XMLTV fetch)

# Stash dynamic-channel state
_stash_channels_cache: dict = {"data": None, "ts": 0.0}
_stash_channel_map: dict[str, dict] = {}   # encoded_id -> stash channel dict
_stash_schedule: dict[str, list] = {}      # tvg_id -> sorted list of schedule entries
_stash_schedule_built_at: float = 0.0
_rebuild_lock: asyncio.Lock = asyncio.Lock()

# Persistent channel configuration (channels.json)
_channels_config: list[dict] = []         # ordered list of channel config dicts

class _FFmpegChannelManager:
    """One FFmpeg HLS process per active channel, started on first play request.

    FFmpeg reads each scene as its own input with its own decoder, normalizes
    everything to a uniform format via the concat filter (so there are no
    decoder/filter-graph reconfigures at scene transitions), and writes HLS
    segments to a per-channel temp directory.  An idle watchdog shuts down
    the process and deletes the temp dir after LIVE_TV_IDLE_TIMEOUT seconds
    of no manifest/segment requests.
    """

    def __init__(self):
        self._procs:  dict[str, asyncio.subprocess.Process] = {}
        self._dirs:   dict[str, str]        = {}
        self._last:   dict[str, float]      = {}   # channel_id → last request timestamp
        self._stderr: dict[str, list[str]]  = {}   # channel_id → rolling stderr lines (last 60)
        self._stderr_fh: dict[str, object]  = {}   # channel_id → per-channel session log file handle
        self._launch_info: dict[str, dict]  = {}   # channel_id → {start_ts, seek, playlist}
        self._feeders: dict[str, asyncio.Task] = {}  # channel_id → feeder task
        self._backends: dict[str, "_PipeBackend"] = {}  # channel_id → pipe backend (FIFO on Linux, TCP on Windows)
        self._consumed_until: dict[str, float] = {}  # channel_id → wall-clock fed up to
        self._current_scene: dict[str, dict] = {}    # channel_id → live "what's being fed now"
        self._lock    = asyncio.Lock()
        self._watchdog: asyncio.Task | None = None

    # ── log file management ────────────────────────────────────────────────

    @staticmethod
    def _ffmpeg_log_path(cid: str) -> str:
        log_dir = getattr(config, "LOG_DIR", "/config")
        d = os.path.join(log_dir, "livetv_ffmpeg")
        os.makedirs(d, exist_ok=True)
        return os.path.join(d, f"{cid}.log")

    def _open_stderr_file(self, cid: str):
        """Open the per-channel FFmpeg log file in append mode.

        Rotates to {cid}.log.old once the active log exceeds 10 MB so a single
        long-running channel can't fill the disk.  Returns None on failure.
        """
        try:
            path = self._ffmpeg_log_path(cid)
            try:
                if os.path.exists(path) and os.path.getsize(path) > 10 * 1024 * 1024:
                    old = path + ".old"
                    if os.path.exists(old):
                        os.remove(old)
                    os.rename(path, old)
            except OSError:
                pass
            return open(path, "a", encoding="utf-8", errors="replace")
        except Exception as exc:
            logger.warning(f"LiveTV FFmpeg: could not open log file for {cid!r}: {exc}")
            return None

    # ── public API ─────────────────────────────────────────────────────────

    def touch(self, cid: str) -> None:
        """Record activity; restarts the idle watchdog if needed."""
        self._last[cid] = time.time()
        if self._watchdog is None or self._watchdog.done():
            self._watchdog = asyncio.create_task(self._idle_loop())

    def manifest_path(self, cid: str) -> str | None:
        d = self._dirs.get(cid)
        if not d:
            return None
        p = os.path.join(d, "stream.m3u8")
        return p if os.path.exists(p) else None

    def seg_dir(self, cid: str) -> str | None:
        return self._dirs.get(cid)

    def is_alive(self, cid: str) -> bool:
        p = self._procs.get(cid)
        return p is not None and p.returncode is None

    def get_scene_at(self, cid: str, ch: dict | None = None) -> dict:
        """Return the segment currently being fed into the master encoder, plus
        a small lookahead from the schedule.  Source of truth is the feeder's
        `_current_scene` entry (set when it spawns each sub-FFmpeg).
        """
        cur = self._current_scene.get(cid)
        if not cur:
            return {}
        elapsed_since_start = max(0.0, time.time() - cur["started_at"])
        elapsed_in_scene = float(cur.get("scene_seek") or 0) + elapsed_since_start
        dur = float(cur.get("duration_sec") or 0)
        result: dict = {
            "scene_id":     cur["scene_id"],
            "title":        cur.get("title", ""),
            "duration_sec": dur,
            "elapsed_sec":  min(elapsed_in_scene, dur) if dur else elapsed_in_scene,
            "remaining_sec": max(0.0, dur - elapsed_in_scene) if dur else 0.0,
        }
        if ch is not None:
            consumed = self._consumed_until.get(cid, time.time())
            up = _upcoming_scheduled_segments(ch, consumed, count=5)
            result["upcoming"] = up
        return result

    async def ensure(self, cid: str, ch: dict, seek: float) -> bool:
        """Start the pipe-based playout pipeline for the channel if it isn't
        already running.  The feeder task takes it from there and keeps the
        master encoder fed indefinitely from the live schedule.
        """
        async with self._lock:
            if self.is_alive(cid) and self.manifest_path(cid):
                return True
            await self._stop_locked(cid)
            return await self._launch(cid, ch, seek)

    async def stop(self, cid: str) -> None:
        async with self._lock:
            await self._stop_locked(cid)

    async def cleanup_all(self) -> None:
        for cid in list(self._procs.keys()):
            await self.stop(cid)

    # ── internals ──────────────────────────────────────────────────────────

    async def _launch(self, cid: str, ch: dict, seek: float) -> bool:
        """Set up the pipe-based playout pipeline.

        Architecture
        ─────────────
            ┌────────────────────┐   raw YUV    ┌─────────────────┐
            │ per-scene sub-     │── /tmp/v.fifo ─▶│                 │
            │ FFmpeg (decode +   │   raw PCM   │  master FFmpeg  │── HLS segments
            │ normalize)         │── /tmp/a.fifo ─▶│  (-c copy-style │
            └────────┬───────────┘              │   encode loop)  │
                     │                          └─────────────────┘
                     │ spawned + waited by the                ▲
                     │ feeder task, one at a time            │
                     ▼                                       │
                  Feeder task (loops through live schedule)──┘

        The parent process holds writer FDs on both FIFOs for the entire
        session, so the master never sees EOF when an individual sub exits
        between scenes.  Sub-FFmpegs write raw frames directly to the FIFOs
        (a sub's writer end closes when it exits; the parent's writer end
        keeps the FIFO alive; the next sub opens its own writer and the
        stream continues).  The master, reading raw input with implicit
        timestamps, has no decoder or filter graph to reconfigure at scene
        boundaries — it just encodes the contiguous raw stream and segments
        it into HLS.  That's what makes the playout truly seamless.
        """
        if not _HAS_MKFIFO and not _IS_WINDOWS:
            logger.error(
                "LiveTV FFmpeg: pipe-based playout requires either os.mkfifo "
                "(Linux/macOS) or Windows TCP-relay fallback.  Channel cannot "
                "start on this platform."
            )
            return False

        hls_base = getattr(config, "HLS_TEMP_DIR", None) or None
        d = tempfile.mkdtemp(prefix=f"sjp_{cid[:8]}_", dir=hls_base)
        self._dirs[cid] = d

        # Pick the backend.  FIFO is preferred wherever supported (kernel does
        # the byte forwarding); Windows falls back to a Python TCP relay.
        if _HAS_MKFIFO:
            backend: _PipeBackend = _FifoPipeBackend(d)
        else:
            backend = _TcpRelayPipeBackend(d)
        try:
            await backend.start()
        except Exception as exc:
            logger.error(f"LiveTV FFmpeg: pipe backend setup failed ({backend.kind}) — {exc}")
            shutil.rmtree(d, ignore_errors=True)
            self._dirs.pop(cid, None)
            return False
        self._backends[cid] = backend
        master_in_v, master_in_a = backend.master_inputs()
        logger.info(
            f"LiveTV FFmpeg: channel {cid!r} pipe backend={backend.kind} "
            f"video={master_in_v} audio={master_in_a}"
        )

        ffmpeg_bin = getattr(config, "FFMPEG_PATH", "ffmpeg")
        seg_tmpl = os.path.join(d, "seg%05d.ts")
        manifest = os.path.join(d, "stream.m3u8")

        master_cmd = [
            ffmpeg_bin, "-y", "-hide_banner",
            # Raw video input — implicit 30 fps timing.
            # -probesize 32 / -analyzeduration 0: every codec param is
            # already specified on the cmdline, so we skip avformat's
            # stream-info probe.  This is critical for the two-input pipe
            # design: probing on input #0 reads from ONLY the video socket
            # while the sub-FFmpeg is producing interleaved video + audio.
            # With probing on, the audio side-buffer fills, sub blocks
            # writing audio, sub (one process) can't write more video,
            # master probe stalls forever — deadlock.
            "-f", "rawvideo",
            "-pix_fmt", "yuv420p",
            "-s", "1920x1080",
            "-r", "30",
            "-probesize", "32",
            "-analyzeduration", "0",
            "-thread_queue_size", "1024",
            "-i", master_in_v,
            # Raw audio input — implicit 48 kHz / stereo timing.  Same
            # probesize/analyzeduration treatment as the video input.
            "-f", "s16le",
            "-ar", "48000",
            "-ac", "2",
            "-probesize", "32",
            "-analyzeduration", "0",
            "-thread_queue_size", "1024",
            "-i", master_in_a,
            # Explicit mapping so the master never silently drops a stream
            "-map", "0:v:0",
            "-map", "1:a:0",
            # Encode once and forever — uniform input means no reconfigures
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
            "-force_key_frames", "expr:gte(t,n_forced*4)",
            "-c:a", "aac", "-b:a", "192k",
            # HLS
            "-hls_time", "4",
            "-hls_list_size", "450",
            "-hls_flags",
            "delete_segments+append_list+omit_endlist+program_date_time+independent_segments",
            # Non-zero starting media-sequence keeps ExoPlayer's live-edge
            # calc above zero (see prior commit for context).
            "-start_number", str(max(1, int(seek) // 4)),
            "-hls_segment_filename", seg_tmpl,
            manifest,
        ]

        logger.info(
            f"LiveTV FFmpeg: launching channel {cid!r} — pipe-based playout, "
            f"initial seek={seek:.1f}s"
        )
        logger.debug(f"LiveTV FFmpeg master cmd: {' '.join(master_cmd)}")

        # Per-channel session log.  Opened BEFORE the master so we can also
        # route sub-FFmpeg stderr through the same file (sub stderr lines get
        # a "[scene NNNN] " prefix).
        fh = self._open_stderr_file(cid)
        self._stderr_fh[cid] = fh
        if fh is not None:
            try:
                ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                fh.write(
                    f"\n===== FFmpeg session start {ts} | channel={cid} "
                    f"backend={backend.kind} seek={seek:.1f}s =====\n"
                )
                fh.write(f"master cmd: {' '.join(master_cmd)}\n")
                fh.flush()
            except Exception:
                pass

        try:
            proc = await asyncio.create_subprocess_exec(
                *master_cmd,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
        except Exception as exc:
            logger.error(f"LiveTV FFmpeg: launch failed — {exc}")
            await backend.close()
            self._backends.pop(cid, None)
            if fh is not None:
                try: fh.close()
                except Exception: pass
            self._stderr_fh.pop(cid, None)
            shutil.rmtree(d, ignore_errors=True)
            self._dirs.pop(cid, None)
            return False

        self._procs[cid] = proc
        self._stderr[cid] = []
        self._launch_info[cid] = {"start_ts": time.time(), "seek": seek, "pid": proc.pid}
        self._consumed_until[cid] = time.time()
        logger.info(f"LiveTV FFmpeg: channel {cid!r} master pid={proc.pid}")

        asyncio.create_task(self._drain_stderr(proc, cid))

        # CRITICAL ordering on TCP backend: master must claim the
        # "first connection = consumer" slot on the video relay port BEFORE
        # sub_v connects, or the relay will mis-route data.  We only wait
        # for the video side here because master can only connect to the
        # audio port AFTER its avformat_find_stream_info() on input #0
        # finishes — and that requires sub_v to be writing data, which we
        # haven't started yet.  The audio-side handshake happens inside
        # _feed_one_scene between spawning sub_v and sub_a.  No-op on FIFO.
        v_ready = await backend.wait_master_video_attached(timeout=10.0)
        if not v_ready:
            logger.error(
                f"LiveTV FFmpeg: master never connected to video endpoint "
                f"for channel {cid!r} — aborting launch"
            )
            await self._stop_locked(cid)
            return False
        logger.info(f"LiveTV FFmpeg: channel {cid!r} master attached to video endpoint")

        # Spawn the feeder.  It iterates the live schedule and pipes each
        # scene's decoded frames into the master via fresh sub-FFmpegs,
        # advancing _consumed_until after each scene.  Runs until cancelled
        # by _stop_locked.
        feeder = asyncio.create_task(self._feeder(cid, ch, backend, seek))
        self._feeders[cid] = feeder

        # Readiness gate — wait for ≥3 segments in the manifest before
        # returning success.  Same logic as before; the master's pipeline
        # still needs the feeder to actually be writing for segments to
        # appear, but that happens immediately after the feeder task starts.
        _MIN_READY_SEGMENTS = 3
        for _ in range(60):
            if proc.returncode is not None:
                logger.error(
                    f"LiveTV FFmpeg: exited prematurely (rc={proc.returncode})"
                )
                buf = self._stderr.get(cid, [])
                error_lines = [l for l in buf if not l.startswith(("ffmpeg version", "  built", "  config", "  lib"))]
                if error_lines:
                    logger.error("LiveTV FFmpeg stderr (errors):\n" + "\n".join(error_lines[-30:]))
                return False
            if os.path.exists(manifest):
                try:
                    with open(manifest, "r", encoding="utf-8") as mh:
                        seg_count = sum(
                            1 for ln in mh
                            if ln.strip() and not ln.startswith("#")
                        )
                except OSError:
                    seg_count = 0
                if seg_count >= _MIN_READY_SEGMENTS:
                    logger.info(
                        f"LiveTV FFmpeg: channel {cid!r} ready "
                        f"({seg_count} segments)"
                    )
                    return True
            await asyncio.sleep(0.5)

        logger.error("LiveTV FFmpeg: timed out waiting for segments")
        return False

    async def _feeder(self, cid: str, ch: dict, backend: "_PipeBackend",
                       initial_seek: float) -> None:
        """Iterate the channel's live schedule, decoding one scene at a time
        into the master via the backend's sub-output endpoints.  Persists
        until the master process is stopped (this task is cancelled by
        _stop_locked).
        """
        stash_base = config.get_stash_base()
        api_key    = getattr(config, "STASH_API_KEY", "")
        ffmpeg_bin = getattr(config, "FFMPEG_PATH", "ffmpeg")

        # consumed_until tracks how far into the wall-clock schedule we've
        # already fed.  Start at "now" so the very first iteration picks the
        # segment currently airing and computes scene_seek = now - seg.start_ts
        # (which equals the `initial_seek` the caller derived from the same
        # arithmetic).
        consumed_until = time.time()
        self._consumed_until[cid] = consumed_until
        logger.info(
            f"LiveTV feeder: started for channel {cid!r} "
            f"(initial_seek={initial_seek:.1f}s)"
        )

        scenes_played = 0
        try:
            while True:
                # Re-read the live schedule on every iteration so a maintenance
                # rebuild or a user-initiated edit takes effect immediately.
                seg = _next_scheduled_segment_after(ch, consumed_until)
                if seg is None:
                    logger.warning(
                        f"LiveTV feeder: no scheduled segment after "
                        f"{consumed_until:.0f} for channel {cid!r}; sleeping 2s"
                    )
                    await asyncio.sleep(2)
                    continue
                scene_id = seg.get("scene_id") or seg.get("id")
                if not scene_id:
                    logger.warning(f"LiveTV feeder: scheduled segment missing scene_id, skipping")
                    consumed_until = float(seg.get("stop_ts", consumed_until + 1))
                    self._consumed_until[cid] = consumed_until
                    continue
                scene_dur = float(seg.get("duration_sec") or 0)
                scene_seek = max(0.0, consumed_until - float(seg.get("start_ts", consumed_until)))
                if scene_dur <= 0 or scene_seek >= scene_dur - 0.5:
                    # Already past end of this segment — advance pointer.
                    consumed_until = float(seg.get("stop_ts", consumed_until + max(0.0, scene_dur)))
                    self._consumed_until[cid] = consumed_until
                    continue

                self._current_scene[cid] = {
                    "scene_id":     scene_id,
                    "title":        seg.get("title", ""),
                    "start_ts":     seg.get("start_ts"),
                    "stop_ts":      seg.get("stop_ts"),
                    "duration_sec": scene_dur,
                    "scene_seek":   scene_seek,
                    "started_at":   time.time(),
                }
                t0 = time.time()
                logger.info(
                    f"LiveTV feeder: channel {cid!r} scene #{scenes_played+1} "
                    f"id={scene_id} title={seg.get('title','')!r} "
                    f"seek={scene_seek:.1f}s dur={scene_dur:.1f}s"
                )
                ok = await self._feed_one_scene(
                    cid, scene_id, backend,
                    stash_base, api_key, ffmpeg_bin, scene_seek,
                )
                logger.info(
                    f"LiveTV feeder: channel {cid!r} scene id={scene_id} "
                    f"finished ok={ok} elapsed={time.time()-t0:.1f}s"
                )
                scenes_played += 1
                # Advance the pointer regardless of success — a failed sub
                # shouldn't lock us into an infinite retry on the same scene.
                consumed_until = float(seg.get("stop_ts", consumed_until + scene_dur))
                self._consumed_until[cid] = consumed_until
        except asyncio.CancelledError:
            logger.info(f"LiveTV feeder: cancelled for {cid!r} after {scenes_played} scene(s)")
            raise
        except Exception:
            logger.error(f"LiveTV feeder: crashed for {cid!r}", exc_info=True)

    async def _feed_one_scene(self, cid: str, scene_id: str,
                               backend: "_PipeBackend",
                               stash_base: str, api_key: str,
                               ffmpeg_bin: str, scene_seek: float) -> bool:
        """Spawn TWO sub-FFmpegs per scene — one for raw video, one for raw
        audio — each writing to its own endpoint on the backend.

        We deliberately do NOT use a single sub with two outputs.  Raw video
        (~746 Mbps for 1080p30) and raw PCM (~1.5 Mbps) have a 500× bandwidth
        gap.  A single-process sub interleaves both outputs; the moment the
        master's video TCP buffer fills, that one process blocks on the
        video write and can no longer write the next audio packet either,
        starving the master's AAC encoder and creating a permanent deadlock
        (verified empirically — master shows "Press [q]" + libx264 init but
        never produces a single frame).  Splitting into two processes gives
        each output an independent backpressure path, so the audio side
        keeps flowing even when video temporarily blocks.

        Sub processes both read the same Stash HTTP source (small extra
        bandwidth cost on a LAN; Stash itself serves seekable mp4).  Both
        sub processes are waited on; the function returns when both exit.
        """
        url = f"{stash_base}/scene/{scene_id}/stream"
        if api_key:
            url += f"?apikey={api_key}"
        sub_out_v, sub_out_a = backend.sub_outputs()

        common_pre = [
            ffmpeg_bin, "-y", "-hide_banner",
            # info logs the input/output summary and (with -stats) the
            # periodic frame=… progress line — invaluable for diagnosing
            # pipe stalls.  -stats is forced so it shows even when stderr
            # isn't a TTY (ffmpeg suppresses progress by default on pipes).
            "-loglevel", "info", "-stats",
            "-reconnect", "1", "-reconnect_streamed", "1",
            "-reconnect_at_eof", "1", "-reconnect_delay_max", "5",
        ]
        seek_args: list[str] = []
        if scene_seek > 0.1:
            seek_args = ["-ss", f"{scene_seek:.3f}"]

        video_cmd = common_pre + seek_args + [
            "-i", url,
            "-map", "0:v:0",
            "-vn", "-sn",  # drop audio+subs at demux level for this process
            "-an",  # belt and braces — no audio output stream
            # ↑ -an conflicts with -vn semantically; remove -vn here.
        ]
        # Build the video sub cleanly to avoid the silly flag combo above.
        video_cmd = common_pre + seek_args + [
            "-i", url,
            "-map", "0:v:0",
            "-an", "-sn",
            "-vf",
            "format=yuv420p,setsar=1,"
            "scale=1920:1080:force_original_aspect_ratio=decrease:force_divisible_by=2,"
            "pad=1920:1080:(ow-iw)/2:(oh-ih)/2:color=black,"
            "setsar=1,fps=30",
            "-pix_fmt", "yuv420p",
            "-f", "rawvideo",
            sub_out_v,
        ]
        audio_cmd = common_pre + seek_args + [
            "-i", url,
            # '?' makes audio optional so a silent source doesn't fail —
            # if there's no audio stream this sub will exit immediately
            # and the master's audio input will simply see nothing this
            # scene (the master keeps reading; next scene's audio sub will
            # resume).
            "-map", "0:a:0?",
            "-vn", "-sn",
            "-af",
            "aresample=async=1000:first_pts=0,"
            "aformat=sample_rates=48000:channel_layouts=stereo",
            "-ar", "48000",
            "-ac", "2",
            "-c:a", "pcm_s16le",
            "-f", "s16le",
            sub_out_a,
        ]

        logger.debug(f"LiveTV feeder: video sub cmd for scene {scene_id}: {' '.join(video_cmd)}")
        logger.debug(f"LiveTV feeder: audio sub cmd for scene {scene_id}: {' '.join(audio_cmd)}")

        # ── 1. Spawn the video sub first ──
        # By this point in _launch we've already waited for the master to
        # attach to the video endpoint, so sub_v will arrive at the relay
        # second and be correctly classified as the producer.
        try:
            sub_v = await asyncio.create_subprocess_exec(
                *video_cmd,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
        except Exception as exc:
            logger.error(f"LiveTV feeder: could not spawn video sub for scene {scene_id}: {exc}")
            return False
        logger.info(f"LiveTV feeder: scene {scene_id} video pid={sub_v.pid}")
        asyncio.create_task(self._drain_sub_stderr(sub_v, cid, f"{scene_id}/v"))

        # ── 2. Wait for master to attach to the audio endpoint ──
        # Master can only do this AFTER its find_stream_info() on input #0
        # finishes, which requires sub_v to have written enough video data
        # for the rawvideo demuxer to satisfy a packet read.  This step is
        # a no-op on the FIFO backend (no race), and on the TCP backend
        # after the first scene (master is already attached and the event
        # stays set).
        a_ready = await backend.wait_master_audio_attached(timeout=15.0)
        if not a_ready:
            logger.error(
                f"LiveTV feeder: master never attached to audio endpoint "
                f"for scene {scene_id} — killing video sub and skipping"
            )
            try: sub_v.terminate()
            except Exception: pass
            await sub_v.wait()
            return False

        # ── 3. Now safe to spawn the audio sub ──
        try:
            sub_a = await asyncio.create_subprocess_exec(
                *audio_cmd,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
        except Exception as exc:
            logger.error(f"LiveTV feeder: could not spawn audio sub for scene {scene_id}: {exc}")
            try: sub_v.terminate()
            except Exception: pass
            await sub_v.wait()
            return False
        logger.info(f"LiveTV feeder: scene {scene_id} audio pid={sub_a.pid}")
        asyncio.create_task(self._drain_sub_stderr(sub_a, cid, f"{scene_id}/a"))

        rc_v, rc_a = await asyncio.gather(sub_v.wait(), sub_a.wait())
        if rc_v != 0:
            logger.warning(f"LiveTV feeder: video sub for scene {scene_id} exited rc={rc_v}")
        if rc_a != 0:
            # rc != 0 from the audio side is common (no audio stream → exit 1);
            # log at debug only.
            logger.debug(f"LiveTV feeder: audio sub for scene {scene_id} exited rc={rc_a}")
        return rc_v == 0

    async def _drain_sub_stderr(self, sub: asyncio.subprocess.Process,
                                 cid: str, scene_id: str) -> None:
        buf = self._stderr.get(cid)
        fh = self._stderr_fh.get(cid)
        try:
            while True:
                line = await sub.stderr.readline()
                if not line:
                    break
                text = line.decode(errors="replace").rstrip()
                tagged = f"[scene {scene_id}] {text}"
                if buf is not None:
                    buf.append(tagged)
                    if len(buf) > 60:
                        buf.pop(0)
                if fh is not None:
                    try:
                        fh.write(tagged + "\n")
                        fh.flush()
                    except Exception:
                        pass
        except Exception:
            pass

    async def _drain_stderr(self, proc: asyncio.subprocess.Process, cid: str) -> None:
        """Read master FFmpeg stderr continuously so the pipe buffer can't fill.

        Each line is appended to the rolling in-memory buffer (last 60) and
        tee'd to the per-channel session log file (self._stderr_fh[cid]).
        """
        buf = self._stderr.setdefault(cid, [])
        fh = self._stderr_fh.get(cid)
        try:
            while True:
                line = await proc.stderr.readline()
                if not line:
                    break
                text = line.decode(errors="replace").rstrip()
                buf.append(text)
                if len(buf) > 60:
                    buf.pop(0)
                if fh is not None:
                    try:
                        fh.write(text + "\n")
                        fh.flush()
                    except Exception:
                        pass
        except Exception:
            pass

    async def _stop_locked(self, cid: str) -> None:
        # Cancel the feeder first so it stops spawning new sub-FFmpegs.
        # Sub-FFmpegs already running are not killed here; they'll exit on
        # their own (and the closure of the backend's writer endpoints below
        # ensures the master will then see EOF and shut down cleanly).
        feeder = self._feeders.pop(cid, None)
        if feeder and not feeder.done():
            feeder.cancel()
            try:
                await asyncio.wait_for(feeder, timeout=2.0)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass

        # Tear down the pipe backend (closes FIFO writer FDs or TCP relay
        # listeners + master connection).  Signals EOF to the master.
        backend = self._backends.pop(cid, None)
        if backend is not None:
            try:
                await backend.close()
            except Exception:
                pass

        proc = self._procs.pop(cid, None)
        if proc and proc.returncode is None:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                proc.kill()

        # Close the per-channel session log file (held open across the whole
        # channel session so master + sub stderr all go to the same file).
        fh = self._stderr_fh.pop(cid, None)
        if fh is not None:
            try:
                fh.write("===== FFmpeg session end =====\n")
                fh.close()
            except Exception:
                pass

        d = self._dirs.pop(cid, None)
        if d:
            shutil.rmtree(d, ignore_errors=True)
        self._stderr.pop(cid, None)
        self._launch_info.pop(cid, None)
        self._current_scene.pop(cid, None)
        self._consumed_until.pop(cid, None)

    async def _idle_loop(self) -> None:
        idle_secs = float(getattr(config, "LIVE_TV_IDLE_TIMEOUT", 300))
        while self._procs:
            await asyncio.sleep(20)
            now  = time.time()
            idle = [
                cid for cid, ts in list(self._last.items())
                if now - ts > idle_secs
            ]
            for cid in idle:
                logger.info(
                    f"LiveTV FFmpeg: channel {cid!r} idle "
                    f"{idle_secs:.0f}s — shutting down"
                )
                await self.stop(cid)
                self._last.pop(cid, None)


_ffmpeg_manager = _FFmpegChannelManager()


# ─── Pipe backends ──────────────────────────────────────────────────────────
#
# Bridges sub-FFmpeg (per-scene producer) and master FFmpeg (long-running
# consumer).  Two endpoints — one for raw video, one for raw PCM audio.
#
# Linux/macOS use named pipes via os.mkfifo with the kernel doing the byte
# forwarding (near-zero overhead).  Windows uses TCP loopback with a Python
# relay (slower but lets you iterate locally in the IDE without WSL/Docker).

class _PipeBackend:
    """Abstract base.  Subclasses must implement start, master_inputs,
    sub_outputs, and close."""
    kind: str = "abstract"

    async def start(self) -> None:
        raise NotImplementedError

    def master_inputs(self) -> tuple[str, str]:
        """URL or path the master FFmpeg should `-i` for video and audio."""
        raise NotImplementedError

    def sub_outputs(self) -> tuple[str, str]:
        """URL or path each sub-FFmpeg writes its raw video/audio output to."""
        raise NotImplementedError

    async def wait_master_video_attached(self, timeout: float = 10.0) -> bool:
        """Block until master has connected to the video endpoint.
        Default no-op (FIFO backend has no race).  Returns True on success,
        False on timeout.
        """
        return True

    async def wait_master_audio_attached(self, timeout: float = 10.0) -> bool:
        """Block until master has connected to the audio endpoint.
        Default no-op (FIFO backend has no race).  Returns True on success,
        False on timeout.
        """
        return True

    async def close(self) -> None:
        raise NotImplementedError


class _FifoPipeBackend(_PipeBackend):
    """Linux/macOS — mkfifo + O_RDWR keepalive FDs.

    Master and sub both open the same path; master O_RDONLY, sub O_WRONLY.
    Parent process keeps an O_RDWR handle to each so the master never sees
    EOF when a sub exits between scenes.
    """
    kind = "fifo"

    def __init__(self, tmpdir: str):
        self.dir = tmpdir
        self.fifo_v = os.path.join(tmpdir, "v.fifo")
        self.fifo_a = os.path.join(tmpdir, "a.fifo")
        self.wfd_v: int | None = None
        self.wfd_a: int | None = None

    async def start(self) -> None:
        os.mkfifo(self.fifo_v, 0o600)
        os.mkfifo(self.fifo_a, 0o600)
        # O_RDWR avoids the "blocks until reader" semantics of O_WRONLY —
        # these opens return immediately and they satisfy the master's
        # subsequent O_RDONLY open.  We never read from these FDs; their
        # job is to keep the FIFO alive across per-scene sub restarts.
        self.wfd_v = os.open(self.fifo_v, os.O_RDWR | os.O_NONBLOCK)
        self.wfd_a = os.open(self.fifo_a, os.O_RDWR | os.O_NONBLOCK)
        # Bump pipe buffer to 1 MB so sub-FFmpeg spawn latency between
        # scenes can't starve the master mid-frame.  Linux default is 64 KB;
        # 1 MB ≈ 11 ms of raw 1080p30 video, enough to ride out a spawn.
        if fcntl is not None and hasattr(fcntl, "F_SETPIPE_SZ"):
            for fd in (self.wfd_v, self.wfd_a):
                try:
                    fcntl.fcntl(fd, fcntl.F_SETPIPE_SZ, 1024 * 1024)
                except OSError:
                    pass

    def master_inputs(self) -> tuple[str, str]:
        return self.fifo_v, self.fifo_a

    def sub_outputs(self) -> tuple[str, str]:
        return self.fifo_v, self.fifo_a

    async def close(self) -> None:
        for fd in (self.wfd_v, self.wfd_a):
            if fd is not None:
                try: os.close(fd)
                except OSError: pass
        self.wfd_v = None
        self.wfd_a = None


class _TcpRelayPipeBackend(_PipeBackend):
    """Windows fallback — TCP loopback relay.

    Two TCP server sockets per channel (video + audio).  Each socket accepts
    one master connection (first to connect) and a series of sub connections
    (one per scene).  Bytes from the current sub are forwarded to the master;
    master stays connected across sub restarts, so it never sees EOF.

    The relay does NOT match the FIFO backend's kernel-side throughput — it
    is suitable for IDE iteration but not heavy production load.  Plan for
    Linux/macOS deployment for real channels.
    """
    kind = "tcp"

    def __init__(self, tmpdir: str):
        self.dir = tmpdir
        self.v_server: asyncio.base_events.Server | None = None
        self.a_server: asyncio.base_events.Server | None = None
        self.v_port: int = 0
        self.a_port: int = 0
        self._v_state: dict = {
            "master_w": None,
            "master_ready": asyncio.Event(),
            "running": True,
            "label": "video",
        }
        self._a_state: dict = {
            "master_w": None,
            "master_ready": asyncio.Event(),
            "running": True,
            "label": "audio",
        }

    async def _handler(self, state: dict, reader: asyncio.StreamReader,
                        writer: asyncio.StreamWriter) -> None:
        # First connection on this socket = master (consumer).  We keep
        # the writer reference and hold the connection open; we never read
        # from this side (master only reads).
        if state["master_w"] is None:
            state["master_w"] = writer
            state["master_ready"].set()
            peer = writer.get_extra_info("peername")
            logger.info(f"LiveTV TCP relay [{state['label']}]: master connected from {peer}")
            try:
                # Spin while the consumer is alive.  Closing happens via close().
                while state["running"] and not writer.is_closing():
                    await asyncio.sleep(0.5)
            finally:
                logger.info(f"LiveTV TCP relay [{state['label']}]: master connection closed")
            return

        # Subsequent connection = sub producer.  Forward bytes to master.
        await state["master_ready"].wait()
        master_w = state["master_w"]
        if master_w is None or master_w.is_closing():
            try: writer.close()
            except Exception: pass
            return
        peer = writer.get_extra_info("peername")
        logger.debug(f"LiveTV TCP relay [{state['label']}]: sub connected from {peer}")
        total = 0
        try:
            while state["running"]:
                data = await reader.read(65536)
                if not data:
                    break
                total += len(data)
                master_w.write(data)
                await master_w.drain()
        except (ConnectionError, asyncio.CancelledError, OSError) as exc:
            logger.debug(f"LiveTV TCP relay [{state['label']}]: sub forward ended ({exc})")
        finally:
            logger.debug(
                f"LiveTV TCP relay [{state['label']}]: sub forwarded {total} bytes"
            )
            try: writer.close()
            except Exception: pass

    async def start(self) -> None:
        # Bind to 127.0.0.1:0 so the kernel picks a free port.
        self.v_server = await asyncio.start_server(
            lambda r, w: self._handler(self._v_state, r, w),
            host="127.0.0.1", port=0, family=socket.AF_INET,
        )
        self.a_server = await asyncio.start_server(
            lambda r, w: self._handler(self._a_state, r, w),
            host="127.0.0.1", port=0, family=socket.AF_INET,
        )
        self.v_port = self.v_server.sockets[0].getsockname()[1]
        self.a_port = self.a_server.sockets[0].getsockname()[1]

    def master_inputs(self) -> tuple[str, str]:
        # Master is a TCP client (no ?listen=1); it connects out to our
        # listening relay.
        return (
            f"tcp://127.0.0.1:{self.v_port}",
            f"tcp://127.0.0.1:{self.a_port}",
        )

    def sub_outputs(self) -> tuple[str, str]:
        # Sub is also a TCP client, connecting to the same listening relay
        # after the master has already taken its slot.
        return (
            f"tcp://127.0.0.1:{self.v_port}",
            f"tcp://127.0.0.1:{self.a_port}",
        )

    async def wait_master_video_attached(self, timeout: float = 10.0) -> bool:
        try:
            await asyncio.wait_for(
                self._v_state["master_ready"].wait(), timeout=timeout
            )
            return True
        except asyncio.TimeoutError:
            return False

    async def wait_master_audio_attached(self, timeout: float = 10.0) -> bool:
        try:
            await asyncio.wait_for(
                self._a_state["master_ready"].wait(), timeout=timeout
            )
            return True
        except asyncio.TimeoutError:
            return False

    async def close(self) -> None:
        self._v_state["running"] = False
        self._a_state["running"] = False
        for state in (self._v_state, self._a_state):
            w = state.get("master_w")
            if w is not None:
                try:
                    w.close()
                except Exception:
                    pass
        for srv in (self.v_server, self.a_server):
            if srv is not None:
                try:
                    srv.close()
                    await srv.wait_closed()
                except Exception:
                    pass


def _next_scheduled_segment_after(ch: dict, t: float) -> dict | None:
    """Return the first scheduled segment whose stop_ts > t (i.e. the next
    segment the feeder should play given a wall-clock pointer)."""
    tvg_id = ch["tvg_id"]
    if ch.get("stash_type") == "shorts":
        for block in _stash_schedule.get(tvg_id, []):
            for seg in block.get("segments") or []:
                if float(seg.get("stop_ts", 0)) > t:
                    return seg
    else:
        for e in _stash_schedule.get(tvg_id, []):
            if float(e.get("stop_ts", 0)) > t:
                return e
    return None


def _upcoming_scheduled_segments(ch: dict, after_t: float, count: int = 5) -> list[dict]:
    """Return up to `count` upcoming segments after the given wall-clock
    pointer.  Used by the now-playing modal to show what's queued."""
    tvg_id = ch["tvg_id"]
    out: list[dict] = []
    if ch.get("stash_type") == "shorts":
        for block in _stash_schedule.get(tvg_id, []):
            for seg in block.get("segments") or []:
                if float(seg.get("stop_ts", 0)) > after_t:
                    out.append({
                        "scene_id":     seg.get("scene_id"),
                        "title":        seg.get("title", ""),
                        "duration_sec": float(seg.get("duration_sec") or 0),
                    })
                    if len(out) >= count:
                        return out
    else:
        for e in _stash_schedule.get(tvg_id, []):
            if float(e.get("stop_ts", 0)) > after_t:
                out.append({
                    "scene_id":     e.get("scene_id"),
                    "title":        e.get("title", ""),
                    "duration_sec": float(e.get("duration_sec") or 0),
                })
                if len(out) >= count:
                    return out
    return out


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _to_uuid_key(hex_id: str) -> str:
    """Convert a 32-char hex ID to hyphenated UUID key format (8-4-4-4-12)."""
    h = hex_id.replace("-", "")[:32].ljust(32, "0")
    return f"{h[0:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:32]}"


def _logo_tag(logo_url: str) -> str:
    """Stable image-tag hash derived from the logo URL."""
    return hashlib.md5(logo_url.encode()).hexdigest() if logo_url else ""


def _logo_dir() -> str:
    """Return (and create) the directory where custom channel logos are stored.

    Derives from CONFIG_FILE so Docker deployments always write to /config/channel_logos
    even when LOG_DIR has not been explicitly set.
    """
    config_dir = os.path.dirname(config.CONFIG_FILE)
    d = os.path.join(config_dir, "channel_logos")
    os.makedirs(d, exist_ok=True)
    return d


def _custom_logo_path(tvg_id: str) -> str | None:
    """Return the path to a custom logo file for this channel, or None."""
    if not tvg_id:
        return None
    base = os.path.join(_logo_dir(), tvg_id)
    for ext in (".png", ".jpg", ".jpeg", ".webp", ".gif"):
        p = base + ext
        if os.path.exists(p):
            return p
    return None


def _stash_screenshot_url(scene_id: str) -> str:
    """Return the proxied Stash screenshot URL for a scene."""
    stash_base = config.get_stash_base()
    apikey = getattr(config, "STASH_API_KEY", "")
    url = f"{stash_base}/scene/{scene_id}/screenshot"
    return f"{url}?apikey={apikey}" if apikey else url


def _live_tv_enabled() -> bool:
    """True if any Live TV source is enabled and the master switch is on."""
    if not getattr(config, "ENABLE_LIVE_TV", False):
        return False
    return getattr(config, "ENABLE_TUNARR", False) or getattr(config, "ENABLE_STASH_CHANNELS", False)


def _schedule_path() -> str:
    log_dir = getattr(config, "LOG_DIR", "/config")
    return os.path.join(log_dir, "stash_schedule.json")


def _save_schedule():
    try:
        path = _schedule_path()
        tmp = path + ".tmp"
        payload = {"built_at": _stash_schedule_built_at, "schedule": _stash_schedule}
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f)
        os.replace(tmp, path)
        logger.notice(f"LiveTV: schedule saved to {path}")
    except Exception as e:
        logger.warning(f"LiveTV: could not save schedule: {e}")


def _load_schedule():
    global _stash_schedule, _stash_schedule_built_at
    path = _schedule_path()
    if not os.path.exists(path):
        return
    try:
        with open(path, "r", encoding="utf-8") as f:
            payload = json.load(f)
        _stash_schedule_built_at = float(payload.get("built_at", 0))
        _stash_schedule = payload.get("schedule", {})
        # Migration: drop legacy Shorts blocks that lack a `segments` field
        # (older format used synthetic "Shorts" placeholders).  Dropping them
        # forces _ensure_stash_schedules to rebuild with the new structure.
        for tvg_id, entries in list(_stash_schedule.items()):
            if entries and any(e.get("title") == "Shorts" and "segments" not in e for e in entries):
                logger.info(f"LiveTV: dropping legacy Shorts schedule for '{tvg_id}' — will rebuild")
                _stash_schedule.pop(tvg_id, None)
        age_h = (time.time() - _stash_schedule_built_at) / 3600
        logger.notice(f"LiveTV: loaded schedule from disk ({len(_stash_schedule)} channels, {age_h:.1f}h old)")
    except Exception as e:
        logger.warning(f"LiveTV: could not load schedule: {e}")


# ---------------------------------------------------------------------------
# Channel configuration persistence (channels.json)
# ---------------------------------------------------------------------------

def _channels_config_path() -> str:
    log_dir = getattr(config, "LOG_DIR", "/config")
    return os.path.join(log_dir, "channels.json")


def _load_channels_config():
    """Load channel configs from disk; does NOT migrate legacy settings (async)."""
    global _channels_config
    path = _channels_config_path()
    if not os.path.exists(path):
        logger.info("LiveTV: channels.json not found — will migrate from legacy config on first request")
        return
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        _channels_config = data.get("channels", [])
        logger.info(f"LiveTV: loaded {len(_channels_config)} channel configs from disk")
    except Exception as e:
        logger.warning(f"LiveTV: could not load channels.json: {e}")


def _save_channels_config():
    try:
        path = _channels_config_path()
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"channels": _channels_config}, f, indent=2)
        os.replace(tmp, path)
    except Exception as e:
        logger.warning(f"LiveTV: could not save channels.json: {e}")


async def _migrate_from_legacy_config() -> list[dict]:
    """One-time migration: build channels.json from STASH_TV_TAGS / STASH_TV_FILTERS."""
    global _channels_config
    from core import stash_client
    migrated: list[dict] = []
    num = int(getattr(config, "STASH_CHANNEL_START_NUMBER", 5001))

    raw_tags = getattr(config, "STASH_TV_TAGS", "") or ""
    tag_names = [t.strip() for t in (raw_tags.split(",") if isinstance(raw_tags, str) else raw_tags) if str(t).strip()]
    if tag_names:
        all_tags = await stash_client.get_all_tags()
        tags_by_name = {t["name"].lower(): t for t in all_tags}
        for name in tag_names:
            tag = tags_by_name.get(name.lower())
            if tag:
                migrated.append({"tvg_id": f"t{tag['id']}", "name": name,
                                  "number": str(num), "stash_type": "tag",
                                  "source_ids": [tag["id"]], "order": len(migrated)})
                num += 1

    raw_filters = getattr(config, "STASH_TV_FILTERS", "") or ""
    filter_names = [f.strip() for f in (raw_filters.split(",") if isinstance(raw_filters, str) else raw_filters) if str(f).strip()]
    if filter_names:
        saved = await stash_client.get_saved_filters()
        filters_by_name = {f["name"].lower(): f for f in saved}
        for name in filter_names:
            sf = filters_by_name.get(name.lower())
            if sf:
                migrated.append({"tvg_id": f"f{sf['id']}", "name": name,
                                  "number": str(num), "stash_type": "filter",
                                  "source_ids": [sf["id"]], "order": len(migrated)})
                num += 1

    if getattr(config, "ENABLE_SHORTS_CHANNEL", False):
        migrated.append({"tvg_id": "shorts", "name": "Shorts", "number": str(num),
                          "stash_type": "shorts", "source_ids": [], "order": len(migrated)})

    _channels_config = migrated
    if migrated:
        _save_channels_config()
        logger.info(f"LiveTV: migrated {len(migrated)} channels from legacy config to channels.json")
    return migrated


# ---------------------------------------------------------------------------
# Parsers
# ---------------------------------------------------------------------------

def _parse_m3u(content: str) -> list[dict]:
    channels = []
    lines = content.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if line.startswith("#EXTINF:"):
            attrs: dict[str, str] = {}
            for m in re.finditer(r'([\w-]+)="([^"]*)"', line):
                attrs[m.group(1)] = m.group(2)
            display_name = line.rsplit(",", 1)[-1].strip() if "," in line else ""
            i += 1
            while i < len(lines) and not lines[i].strip():
                i += 1
            stream_url = lines[i].strip() if i < len(lines) and not lines[i].startswith("#") else ""
            tvg_id = attrs.get("tvg-id") or display_name or str(len(channels))
            channels.append({
                "tvg_id": tvg_id,
                "name": attrs.get("tvg-name") or display_name,
                "logo": attrs.get("tvg-logo", ""),
                "number": attrs.get("tvg-chno", str(len(channels) + 1)),
                "stream_url": stream_url,
            })
        i += 1
    return channels


def _parse_xmltv_dt(s: str) -> tuple[str, float]:
    """Return (ISO-8601 UTC string, unix timestamp). Both empty/0 on failure."""
    try:
        parts = s.strip().split()
        dt = datetime.strptime(parts[0], "%Y%m%d%H%M%S")
        if len(parts) > 1:
            sign = 1 if parts[1][0] == "+" else -1
            dt -= timedelta(hours=int(parts[1][1:3]), minutes=int(parts[1][3:5])) * sign
        ts = dt.replace(tzinfo=timezone.utc).timestamp()
        return dt.strftime("%Y-%m-%dT%H:%M:%S.0000000Z"), ts
    except Exception:
        return "", 0.0


def _parse_xmltv(content: str) -> list[dict]:
    programs = []
    try:
        root = ET.fromstring(content)
        for prog in root.findall("programme"):
            title_el = prog.find("title")
            desc_el = prog.find("desc")
            cat_el = prog.find("category")
            date_el = prog.find("date")
            start_iso, start_ts = _parse_xmltv_dt(prog.get("start", ""))
            stop_iso, stop_ts = _parse_xmltv_dt(prog.get("stop", ""))
            duration_ticks = max(0, int((stop_ts - start_ts) * 10_000_000)) if stop_ts and start_ts else 0
            year = None
            if date_el is not None and date_el.text:
                try:
                    year = int(date_el.text[:4])
                except ValueError:
                    pass
            icon_el = prog.find("icon")
            icon_url = icon_el.get("src", "") if icon_el is not None else ""
            rating_el = prog.find("rating")
            rating = ""
            if rating_el is not None:
                val_el = rating_el.find("value")
                if val_el is not None and val_el.text:
                    rating = val_el.text.strip()
            programs.append({
                "channel_id": prog.get("channel", ""),
                "title": (title_el.text or "Unknown").strip() if title_el is not None else "Unknown",
                "desc": (desc_el.text or "").strip() if desc_el is not None else "",
                "genre": cat_el.text if cat_el is not None else "",
                "year": year,
                "rating": rating,
                "start": start_iso,
                "start_ts": start_ts,
                "stop": stop_iso,
                "stop_ts": stop_ts,
                "run_time_ticks": duration_ticks,
                "icon": icon_url,
            })
    except Exception as e:
        logger.warning(f"XMLTV parse error: {e}")
    return programs


# ---------------------------------------------------------------------------
# Cached fetchers
# ---------------------------------------------------------------------------

async def _get_channels() -> list[dict]:
    now = time.time()
    if _m3u_cache["data"] is not None and now - _m3u_cache["ts"] < CACHE_TTL:
        return _m3u_cache["data"]

    m3u_url = getattr(config, "TUNER_M3U_URL", "")
    if not m3u_url:
        return []

    try:
        resp = await _live_client.get(m3u_url)
        resp.raise_for_status()
        channels = _parse_m3u(resp.text)
        _m3u_cache["data"] = channels
        _m3u_cache["ts"] = now
        _channel_stream_map.clear()
        # Remove stale Tunarr entries without disturbing Stash channel entries
        for k in [k for k, v in _channel_info_map.items() if not v.get("stash_type")]:
            _channel_info_map.pop(k, None)
            _channel_tvgid_map.pop(k, None)
        for ch in channels:
            eid = encode_id("ch", ch["tvg_id"])
            _channel_stream_map[eid] = ch["stream_url"]
            _channel_stream_map[eid.replace("-", "")] = ch["stream_url"]
            _channel_info_map[eid] = ch
            _channel_info_map[eid.replace("-", "")] = ch
            _channel_tvgid_map[eid.replace("-", "")] = ch["tvg_id"]
        logger.notice(f"LiveTV: loaded {len(channels)} channels from M3U")
        return channels
    except Exception as e:
        logger.warning(f"LiveTV: failed to fetch M3U: {e}")
        return _m3u_cache["data"] or []


async def _get_programs() -> list[dict]:
    now = time.time()
    if _xmltv_cache["data"] is not None and now - _xmltv_cache["ts"] < CACHE_TTL:
        return _xmltv_cache["data"]

    xmltv_url = getattr(config, "TUNER_XMLTV_URL", "")
    if not xmltv_url:
        return []

    try:
        resp = await _live_client.get(xmltv_url)
        resp.raise_for_status()
        programs = _parse_xmltv(resp.text)
        _xmltv_cache["data"] = programs
        _xmltv_cache["ts"] = now
        _program_info_map.clear()
        _program_tvgkey_map.clear()
        for prog in programs:
            eid = encode_id("program", f"{prog['channel_id']}|{prog['start']}")
            _program_info_map[eid] = prog
            _program_info_map[eid.replace("-", "")] = prog
            _program_tvgkey_map[eid.replace("-", "")] = (prog["channel_id"], prog["start"])
        logger.notice(f"LiveTV: loaded {len(programs)} programs from XMLTV")
        return programs
    except Exception as e:
        logger.warning(f"LiveTV: failed to fetch XMLTV: {e}")
        return _xmltv_cache["data"] or []


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Dynamic Stash Channels
# ---------------------------------------------------------------------------

async def _fetch_scenes_for_stash_channel(ch: dict) -> list[dict]:
    """Return [{id, title, duration_sec, …}] for the given Stash channel config.

    Supports multi-source_ids: tag channels union all tag IDs in one query;
    filter channels union results from each saved filter by scene ID.
    """
    from core.stash_client import call_graphql
    channel_type = ch.get("stash_type", "")
    source_ids = [str(s) for s in (ch.get("source_ids") or [])]
    if not source_ids and ch.get("stash_id"):
        source_ids = [str(ch["stash_id"])]

    _SCENE_FIELDS = "id title files { duration } organized rating100 o_counter tags { name } performers { id } details"

    if channel_type == "tag":
        # INCLUDES with multiple IDs = OR — scenes matching ANY of the selected tags
        scene_filter = {"tags": {"value": source_ids, "modifier": "INCLUDES", "depth": 1}}
        query = f"""
        query($sf: SceneFilterType) {{
            findScenes(filter: {{per_page: -1, sort: "id", direction: ASC}}, scene_filter: $sf) {{
                scenes {{ {_SCENE_FIELDS} }}
            }}
        }}
        """
        data = await call_graphql(query, {"sf": scene_filter})
        raw = (data or {}).get("findScenes", {}).get("scenes", [])

    elif channel_type == "filter":
        from core.query_builder import transform_saved_filter
        from core.stash_client import get_saved_filters
        # Union results from all source filters, deduplicating by scene ID
        saved_all = await get_saved_filters()
        saved_by_id = {str(f["id"]): f for f in saved_all}
        raw_by_id: dict[str, dict] = {}
        q = f"""
        query($filter: FindFilterType, $sf: SceneFilterType) {{
            findScenes(filter: $filter, scene_filter: $sf) {{
                scenes {{ {_SCENE_FIELDS} }}
            }}
        }}
        """
        for filter_id in source_ids:
            fd = saved_by_id.get(filter_id)
            if not fd:
                continue
            scene_filter: dict = {}
            filter_args: dict = {"per_page": -1, "sort": "id", "direction": "ASC"}
            if fd.get("object_filter"):
                scene_filter = transform_saved_filter(fd["object_filter"])
            elif fd.get("filter"):
                import json as _json
                parsed = _json.loads(fd["filter"])
                if "scene_filter" in parsed:
                    scene_filter = transform_saved_filter(parsed["scene_filter"])
                for k in ("q", "sort", "direction"):
                    if k in parsed:
                        filter_args[k] = parsed[k]
            data = await call_graphql(q, {"filter": filter_args, "sf": scene_filter})
            for s in (data or {}).get("findScenes", {}).get("scenes", []):
                raw_by_id[s["id"]] = s
        raw = list(raw_by_id.values())

    elif channel_type == "shorts":
        max_secs = int(getattr(config, "SHORTS_MAX_MINUTES", 5)) * 60
        scene_filter = {"duration": {"value": max_secs, "modifier": "LESS_THAN"}}
        query = """
        query($sf: SceneFilterType) {
            findScenes(filter: {per_page: -1, sort: "id", direction: ASC}, scene_filter: $sf) {
                scenes { id title files { duration } }
            }
        }
        """
        data = await call_graphql(query, {"sf": scene_filter})
        raw = (data or {}).get("findScenes", {}).get("scenes", [])
    else:
        raw = []

    shorts_enabled  = bool(getattr(config, "ENABLE_SHORTS_CHANNEL", False))
    shorts_max_secs = int(getattr(config, "SHORTS_MAX_MINUTES", 5)) * 60

    result = []
    for s in raw:
        files = s.get("files") or []
        duration = float(files[0].get("duration") or 0) if files else 0.0
        if duration < 5.0:
            continue
        # When the shorts channel is enabled, exclude short scenes from regular channels
        # so the same scene never appears on both a shorts channel and a regular channel.
        if channel_type != "shorts" and shorts_enabled and duration < shorts_max_secs:
            continue
        result.append({
            "id": s["id"],
            "title": s.get("title") or f"Scene {s['id']}",
            "duration_sec": duration,
            "organized": bool(s.get("organized")),
            "rating": s.get("rating100") or 0,
            "o_counter": s.get("o_counter") or 0,
            "tag_count": len(s.get("tags") or []),
            "tags": [t["name"] for t in (s.get("tags") or [])],
            "performer_count": len(s.get("performers") or []),
            "has_description": bool((s.get("details") or "").strip()),
        })
    return result


def _new_eid() -> str:
    """Generate a compact random entry ID (8 hex chars, ~4 billion space)."""
    return os.urandom(4).hex()


def _scene_genre(scene: dict) -> str:
    """Map Stash scene metadata to a Jellyfin guide color category.

    Priority (first match wins):
        1. Movie  — organized OR rating=100 OR o_counter > 3  → purple
        2. Kids   — tag_count > 3 OR o_counter ≥ 1           → light blue
        3. Sports — tag_count < 3                             → indigo
        4. News   — no description                            → green

    o_counter = Stash "O Counter" (times marked as enjoyed, not watched).
    """
    if (scene.get("organized")
            or (scene.get("rating") or 0) == 100
            or (scene.get("o_counter") or 0) > 3):
        return "Movie"
    tag_count = scene.get("tag_count") or 0
    if tag_count >= 3 or (scene.get("o_counter") or 0) >= 1:
        return "Kids"
    if tag_count < 3:
        return "Sports"
    if not scene.get("has_description"):
        return "News"
    return ""


def _build_random_schedule(scenes: list[dict]) -> list[dict]:
    """Build a full schedule from scratch for a channel.

    Fills [now - KEEP_DAYS, now + SCHEDULE_DAYS].  Scenes cycle through a
    shuffled pool; when exhausted the pool refills so no scene repeats until
    every other scene has aired once.  Each entry receives a stable eid.
    """
    if not scenes:
        return []

    keep_days = max(1, int(getattr(config, "STASH_KEEP_DAYS", 2)))
    sched_days = max(1, int(getattr(config, "STASH_SCHEDULE_DAYS", 7)))

    now = time.time()
    window_start = now - keep_days * 86400
    window_end = now + sched_days * 86400

    pool: list[dict] = []
    entries: list[dict] = []
    cursor = window_start

    while cursor < window_end:
        if not pool:
            pool = list(scenes)
            random.shuffle(pool)
        s = pool.pop()
        stop = cursor + s["duration_sec"]
        entries.append({
            "eid": _new_eid(),
            "start_ts": cursor,
            "stop_ts": stop,
            "scene_id": s["id"],
            "title": s["title"],
            "duration_sec": s["duration_sec"],
            "genre": _scene_genre(s),
            "rating": s.get("rating") or 0,
            "o_counter": s.get("o_counter") or 0,
        })
        cursor = stop

    return entries


_HALF_HOUR = 1800  # seconds per Shorts block
_SHORTS_WIGGLE = 60  # max over/undershoot of block boundary, in seconds


def _half_hour_ceil(ts: float) -> float:
    return float(((int(ts) + _HALF_HOUR - 1) // _HALF_HOUR) * _HALF_HOUR)


def _half_hour_floor(ts: float) -> float:
    return float((int(ts) // _HALF_HOUR) * _HALF_HOUR)


def _shorts_blocks_in_range(scenes: list[dict], range_start: float, range_end: float) -> list[dict]:
    """Produce half-hour-aligned 30-minute Shorts blocks between range_start..range_end.

    Each block contains a `segments` list with the actual scenes that play in
    that block.  Block start times are aligned to UTC half-hour boundaries.
    Within a block, segments are sequential starting at block_start and ending
    at or near block_end with up to _SHORTS_WIGGLE seconds of slop on either
    side so a full scene can fit.  Scenes never span a block boundary.
    Partial blocks (less than a full 30 min available in the range) are
    omitted — the last block may end up to _HALF_HOUR seconds before
    range_end.
    """
    if not scenes:
        return []

    start_b = _half_hour_ceil(range_start)
    end_b   = _half_hour_floor(range_end)
    if start_b + _HALF_HOUR > end_b:
        return []

    pool = list(scenes)
    random.shuffle(pool)
    pool_idx = 0

    blocks: list[dict] = []
    block_start = start_b
    while block_start + _HALF_HOUR <= end_b:
        block_end = block_start + _HALF_HOUR
        segments: list[dict] = []
        cursor = block_start
        # Pack scenes greedily; rotate past ones that don't fit.  Stop once we
        # come within _SHORTS_WIGGLE of block_end, or after a full pool sweep
        # without progress.
        attempts = 0
        while attempts < len(pool):
            s = pool[pool_idx % len(pool)]
            pool_idx += 1
            attempts += 1
            if cursor + s["duration_sec"] > block_end + _SHORTS_WIGGLE:
                continue
            segments.append({
                "scene_id":     s["id"],
                "title":        s["title"],
                "start_ts":     cursor,
                "stop_ts":      cursor + s["duration_sec"],
                "duration_sec": s["duration_sec"],
                "genre":        _scene_genre(s),
                "rating":       s.get("rating") or 0,
                "o_counter":    s.get("o_counter") or 0,
            })
            cursor += s["duration_sec"]
            attempts = 0
            if cursor >= block_end - _SHORTS_WIGGLE:
                break

        if segments:
            block_stop = segments[-1]["stop_ts"]
            blocks.append({
                "eid":          _new_eid(),
                "start_ts":     block_start,
                "stop_ts":      block_stop,
                "title":        "Shorts",
                "duration_sec": block_stop - block_start,
                "segments":     segments,
            })
        block_start += _HALF_HOUR

    return blocks


def _build_shorts_block_schedule(scenes: list[dict]) -> list[dict]:
    """Build the full Shorts schedule across the keep/sched window."""
    if not scenes:
        return []
    keep_days  = max(1, int(getattr(config, "STASH_KEEP_DAYS", 2)))
    sched_days = max(1, int(getattr(config, "STASH_SCHEDULE_DAYS", 7)))
    now = time.time()
    return _shorts_blocks_in_range(scenes, now - keep_days * 86400, now + sched_days * 86400)


def _maintenance_extend_channel(
    existing: list[dict],
    all_scenes: list[dict],
    keep_days: int,
    sched_days: int,
) -> tuple[list[dict], int, int]:
    """Prune stale entries and extend a channel's schedule to fill the window.

    Scenes already retained in the schedule are treated as "used" — new
    entries draw from the remaining pool first, cycling through all available
    scenes before any repeats.  Returns (updated_entries, pruned_count, added_count).
    """
    now = time.time()
    cutoff     = now - keep_days * 86400
    target_end = now + sched_days * 86400

    retained  = [e for e in existing if e.get("stop_ts", 0) > cutoff]
    pruned    = len(existing) - len(retained)
    frontier  = max((e["stop_ts"] for e in retained), default=cutoff)

    if frontier >= target_end:
        return retained, pruned, 0

    # Scenes in the retained schedule are "used"; everything else is available.
    used_ids  = {e["scene_id"] for e in retained if e.get("scene_id")}
    available = [s for s in all_scenes if s["id"] not in used_ids]
    random.shuffle(available)
    used      = [s for s in all_scenes if s["id"] in used_ids]

    if not available:
        # Every scene is already scheduled — start a fresh cycle.
        available = list(all_scenes)
        random.shuffle(available)
        used = []

    new_entries: list[dict] = []
    cursor = frontier
    while cursor < target_end:
        if not available:
            available = used
            random.shuffle(available)
            used = []
        s = available.pop()
        used.append(s)
        stop = cursor + s["duration_sec"]
        new_entries.append({
            "eid":          _new_eid(),
            "start_ts":     cursor,
            "stop_ts":      stop,
            "scene_id":     s["id"],
            "title":        s["title"],
            "duration_sec": s["duration_sec"],
            "genre":        _scene_genre(s),
            "rating":       s.get("rating") or 0,
            "o_counter":    s.get("o_counter") or 0,
        })
        cursor = stop

    return retained + new_entries, pruned, len(new_entries)


def _maintenance_extend_shorts(
    existing: list[dict],
    scenes: list[dict],
    keep_days: int,
    sched_days: int,
) -> tuple[list[dict], int, int]:
    """Prune stale Shorts blocks and append new ones to fill the window."""
    now        = time.time()
    cutoff     = now - keep_days * 86400
    target_end = now + sched_days * 86400

    retained = [b for b in existing if b.get("stop_ts", 0) > cutoff]
    pruned   = len(existing) - len(retained)

    # Last retained block was aligned to a half-hour boundary; the next block
    # starts one half-hour after it.
    if retained:
        last_start = max(b["start_ts"] for b in retained)
        frontier   = last_start + _HALF_HOUR
    else:
        frontier = cutoff

    new_blocks = _shorts_blocks_in_range(scenes, frontier, target_end)
    return retained + new_blocks, pruned, len(new_blocks)


async def _get_stash_channels() -> list[dict]:
    """Build runtime channel list from channels.json config, migrating from legacy settings if needed."""
    global _channels_config
    from core import stash_client
    now = time.time()
    if _stash_channels_cache["data"] is not None and now - _stash_channels_cache["ts"] < CACHE_TTL:
        return _stash_channels_cache["data"]

    # First run: migrate from STASH_TV_TAGS / STASH_TV_FILTERS if no channels.json exists
    if not _channels_config and not os.path.exists(_channels_config_path()):
        await _migrate_from_legacy_config()

    configs = sorted(_channels_config, key=lambda c: c.get("order", 0))

    # Pre-fetch tag images for tag channels (single batch call)
    tag_info: dict[str, dict] = {}
    if any(c.get("stash_type") == "tag" for c in configs):
        all_tags = await stash_client.get_all_tags()
        tag_info = {t["id"]: t for t in all_tags}

    channels: list[dict] = []
    for cfg in configs:
        stash_type = cfg.get("stash_type", "tag")
        source_ids  = cfg.get("source_ids") or []

        # Build default logo from first tag's image_path (tags only)
        logo = ""
        if stash_type == "tag" and source_ids:
            raw_logo = tag_info.get(str(source_ids[0]), {}).get("image_path", "")
            if raw_logo:
                if not raw_logo.startswith("http"):
                    raw_logo = f"{config.get_stash_base()}{raw_logo}"
                api_key = getattr(config, "STASH_API_KEY", "")
                if api_key and "apikey=" not in raw_logo:
                    raw_logo += f"{'&' if '?' in raw_logo else '?'}apikey={api_key}"
                logo = raw_logo

        ch: dict = {
            "tvg_id": cfg["tvg_id"],
            "name": cfg.get("name", "Channel"),
            "number": cfg.get("number", "5001"),
            "logo": logo,
            "stash_type": stash_type,
            "source_ids": source_ids,
            # Legacy compat: single stash_id field (first source)
            "stash_id": str(source_ids[0]) if source_ids else "",
        }
        channels.append(ch)

    _stash_channels_cache["data"] = channels
    _stash_channels_cache["ts"] = now

    for ch in channels:
        enc = encode_id("ch", ch["tvg_id"])
        _channel_info_map[enc] = ch
        _channel_info_map[enc.replace("-", "")] = ch
        _stash_channel_map[enc] = ch
        _stash_channel_map[enc.replace("-", "")] = ch
        _channel_tvgid_map[enc.replace("-", "")] = ch["tvg_id"]

    logger.info(f"LiveTV: {len(channels)} Stash channels configured")
    return channels


async def _rebuild_stash_schedules():
    """Fetch scenes for every Stash channel and rebuild all schedules."""
    global _stash_schedule, _stash_schedule_built_at

    if not getattr(config, "ENABLE_STASH_CHANNELS", False):
        return

    async with _rebuild_lock:
        channels = await _get_stash_channels()
        new_schedule: dict[str, list] = {}

        for ch in channels:
            tvg_id = ch["tvg_id"]
            try:
                if ch.get("stash_type") == "shorts":
                    scenes = await _fetch_scenes_for_stash_channel(ch)
                    if not scenes:
                        logger.warning(f"LiveTV: no scenes for channel '{ch['name']}' — EPG will be empty")
                        continue
                    slots = _build_shorts_block_schedule(scenes)
                    new_schedule[tvg_id] = slots
                    seg_total = sum(len(b.get("segments", [])) for b in slots)
                    logger.notice(f"LiveTV: schedule built for '{ch['name']}' — {len(scenes)} scenes, {len(slots)} 30-min blocks, {seg_total} segments")
                else:
                    scenes = await _fetch_scenes_for_stash_channel(ch)
                    if not scenes:
                        logger.warning(f"LiveTV: no scenes for channel '{ch['name']}' — EPG will be empty")
                        continue
                    slots = _build_random_schedule(scenes)
                    new_schedule[tvg_id] = slots
                    logger.notice(f"LiveTV: schedule built for '{ch['name']}' — {len(scenes)} scenes, {len(slots)} EPG slots")
            except Exception as e:
                logger.error(f"LiveTV: schedule build failed for '{ch['name']}': {e}", exc_info=True)

        _stash_schedule = new_schedule
        _stash_schedule_built_at = time.time()
        _save_schedule()


async def _run_maintenance_update():
    """Prune old entries and extend each channel's schedule forward.

    Preserves all existing entries within the keep-days window so users see
    the same schedule they already looked at.  Only prunes the past and
    appends new content at the end.
    """
    global _stash_schedule, _stash_schedule_built_at

    if not getattr(config, "ENABLE_STASH_CHANNELS", False):
        return

    async with _rebuild_lock:
        channels  = await _get_stash_channels()
        keep_days = max(1, int(getattr(config, "STASH_KEEP_DAYS", 2)))
        sched_days = max(1, int(getattr(config, "STASH_SCHEDULE_DAYS", 7)))
        changed   = False

        for ch in channels:
            tvg_id   = ch["tvg_id"]
            existing = _stash_schedule.get(tvg_id, [])
            try:
                scenes = await _fetch_scenes_for_stash_channel(ch)
                if ch.get("stash_type") == "shorts":
                    # Shorts still prunes past blocks even when scenes is empty
                    # (channel temporarily without content); extend is just a no-op.
                    updated, pruned, added = _maintenance_extend_shorts(existing, scenes, keep_days, sched_days)
                else:
                    if not scenes:
                        continue
                    updated, pruned, added = _maintenance_extend_channel(existing, scenes, keep_days, sched_days)

                if pruned or added:
                    logger.notice(
                        f"LiveTV maintenance: '{ch['name']}' pruned={pruned} added={added} "
                        f"total={len(updated)}"
                    )
                _stash_schedule[tvg_id] = updated
                changed = True
            except Exception as e:
                logger.error(f"LiveTV maintenance: failed for '{ch['name']}': {e}", exc_info=True)

        if changed:
            _stash_schedule_built_at = time.time()
            _save_schedule()


async def _ensure_stash_schedules():
    """Bootstrap or refresh schedules on first request.

    - No schedule at all → full rebuild (first run or after a manual clear).
    - Schedule loaded from disk but older than TTL → incremental maintenance
      (preserves existing entries, only prunes + extends).
    """
    if not _stash_schedule:
        await _rebuild_stash_schedules()
    elif time.time() - _stash_schedule_built_at > _SCHEDULE_TTL:
        await _run_maintenance_update()


# ---------------------------------------------------------------------------
# Background maintenance task
# ---------------------------------------------------------------------------

_maintenance_task: asyncio.Task | None = None


async def _schedule_maintenance_loop():
    """Prune old entries and extend the schedule window — runs every 24 hours."""
    while True:
        await asyncio.sleep(_SCHEDULE_TTL)
        logger.info("LiveTV: 24h maintenance — pruning old entries and extending schedule window")
        try:
            await _run_maintenance_update()
        except Exception as e:
            logger.error(f"LiveTV: scheduled maintenance failed: {e}", exc_info=True)


async def start_maintenance_task():
    """Start the background schedule-maintenance loop (called from app lifespan)."""
    global _maintenance_task
    if _maintenance_task and not _maintenance_task.done():
        return
    _maintenance_task = asyncio.create_task(_schedule_maintenance_loop())
    logger.info("LiveTV: schedule maintenance task started (interval: 24h)")


async def stop_maintenance_task():
    """Cancel the maintenance loop (called from app lifespan on shutdown)."""
    global _maintenance_task
    if _maintenance_task and not _maintenance_task.done():
        _maintenance_task.cancel()
        try:
            await _maintenance_task
        except asyncio.CancelledError:
            pass
    _maintenance_task = None


def _get_stash_programs_for_channel(ch: dict, server_id: str, channels_by_tvg_id: dict) -> list[dict]:
    """Return Jellyfin-formatted programs from the Stash schedule for one channel."""
    tvg_id = ch["tvg_id"]
    schedule = _stash_schedule.get(tvg_id, [])
    now = time.time()
    keep_days = int(getattr(config, "STASH_KEEP_DAYS", 2))
    sched_days = int(getattr(config, "STASH_SCHEDULE_DAYS", 7))
    window_start = now - keep_days * 86400
    window_end = now + sched_days * 86400

    progs = []
    for entry in schedule:
        if entry["stop_ts"] < window_start or entry["start_ts"] > window_end:
            continue
        start_dt = datetime.fromtimestamp(entry["start_ts"], timezone.utc)
        stop_dt = datetime.fromtimestamp(entry["stop_ts"], timezone.utc)
        raw_prog = {
            "channel_id": tvg_id,
            "title": entry["title"],
            "start": start_dt.strftime("%Y-%m-%dT%H:%M:%S.0000000Z"),
            "stop": stop_dt.strftime("%Y-%m-%dT%H:%M:%S.0000000Z"),
            "start_ts": entry["start_ts"],
            "stop_ts": entry["stop_ts"],
            "run_time_ticks": int(entry["duration_sec"] * 10_000_000),
            "genre": entry.get("genre", ""),
            "desc": "",
            "scene_id": entry["scene_id"],
            "icon": _stash_screenshot_url(entry["scene_id"]) if entry.get("scene_id") else "",
        }
        prog_id = encode_id("program", f"{tvg_id}|{raw_prog['start']}")
        jellyfin_prog = _program_to_jellyfin(raw_prog, server_id, channels_by_tvg_id, prog_id)
        # Register for single-item lookup
        enc = prog_id.replace("-", "")
        _program_info_map[enc] = raw_prog
        progs.append(jellyfin_prog)
    return progs


async def _build_stash_channel_playlist(ch: dict) -> tuple[list[dict], float] | None:
    """Return (entries, seek_seconds) for the channel's current content, or None.

    Shared by stash_channel_playback_info (pre-start) and
    endpoint_stash_channel_stream (manifest serve).
    """
    tvg_id = ch["tvg_id"]
    now = time.time()

    if ch.get("stash_type") == "shorts":
        # Flatten the segments stored in each block into a single schedule and
        # play it the same way as a regular channel — seek into the segment
        # currently airing, then queue everything after it.  If we land in a
        # small wiggle gap between two blocks the first upcoming segment may
        # start slightly in the future; play it from its beginning rather than
        # failing so the user doesn't see a stall.
        blocks = _stash_schedule.get(tvg_id, [])
        all_segments: list[dict] = []
        for block in blocks:
            all_segments.extend(block.get("segments") or [])
        upcoming = [s for s in all_segments if s["stop_ts"] > now - 5]
        if not upcoming:
            return None
        playlist: list[dict] = []
        total = 0.0
        for s in upcoming:
            if total >= 3600:
                break
            playlist.append({
                "scene_id":     s["scene_id"],
                "title":        s.get("title", ""),
                "duration_sec": s["duration_sec"],
            })
            total += s["duration_sec"]
        return playlist, max(0.0, now - upcoming[0]["start_ts"])
    else:
        schedule = _stash_schedule.get(tvg_id, [])
        upcoming = [e for e in schedule if e["stop_ts"] > now - 5]
        if not upcoming or upcoming[0]["start_ts"] > now + 10:
            return None
        return upcoming, max(0.0, now - upcoming[0]["start_ts"])


async def stash_channel_playback_info(ch: dict, item_id: str, request=None) -> JSONResponse:
    """PlaybackInfo for a dynamic Stash channel.

    Advertises an HLS TranscodingUrl and disables direct play so the client uses
    hls.js / ExoPlayer HlsMediaSource.  Pre-warms FFmpeg so segments are ready
    by the time the client fetches the manifest.
    """
    item_id = item_id.replace("-", "")
    play_session_id = f"stash_live_{ch['tvg_id']}"

    # Relative HLS transcoding URL.  Advertising a TranscodingUrl with
    # TranscodingSubProtocol=hls makes jellyfin-web use hls.js and Jellyfin
    # Android TV use ExoPlayer's HlsMediaSource, both pointed straight at our
    # .m3u8.  Direct play (static=true) must be disabled — otherwise the
    # client requests /Videos/{id}/stream expecting a raw byte stream and
    # chokes on the HLS playlist it gets instead.
    transcode_url = f"/livetv/channels/{item_id}/stash-stream.m3u8"

    # Pre-warm FFmpeg so the manifest has segments by the time the client
    # fetches it (the readiness gate waits for >=3 segments).
    await _ensure_stash_schedules()
    playlist_result = await _build_stash_channel_playlist(ch)
    if playlist_result is not None:
        _entries, seek = playlist_result
        await _ffmpeg_manager.ensure(item_id, ch, seek)

    source: dict = {
        "Protocol": "Http",
        "Id": item_id,
        "Path": transcode_url,
        "Type": "Default",
        "Name": ch.get("name", "Live"),
        "IsRemote": False,
        "ReadAtNativeFramerate": True,
        "IgnoreDts": False,
        "IgnoreIndex": False,
        "GenPtsInput": False,
        "SupportsTranscoding": True,
        "SupportsDirectStream": False,
        "SupportsDirectPlay": False,
        "IsInfiniteStream": True,
        "IsLive": True,
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
             "DisplayTitle": "SDR", "IsInterlaced": False, "IsDefault": False,
             "IsForced": False, "IsHearingImpaired": False, "Type": "Video", "Index": -1,
             "IsExternal": False, "IsTextSubtitleStream": False, "SupportsExternalStream": False},
            {"VideoRange": "Unknown", "VideoRangeType": "Unknown", "AudioSpatialFormat": "None",
             "DisplayTitle": "", "IsInterlaced": False, "IsDefault": False,
             "IsForced": False, "IsHearingImpaired": False, "Type": "Audio", "Index": -1,
             "IsExternal": False, "IsTextSubtitleStream": False, "SupportsExternalStream": False},
        ],
        "MediaAttachments": [],
        "Formats": [],
        "RequiredHttpHeaders": {},
        "DefaultAudioStreamIndex": -1,
        "HasSegments": False,
    }

    logger.info(f"LiveTV: stash channel playback_info '{ch['name']}' ({item_id}) → {transcode_url}")
    return JSONResponse({
        "MediaSources": [source],
        "PlaySessionId": play_session_id,
    })


async def endpoint_stash_channel_stream(request: Request):
    """FFmpeg-based live HLS stream for a Stash channel.

    On first play request, spawns an FFmpeg process that:
      • opens each scene as its own input via raw Stash HTTP streams (no
        Stash-side transcode — byte-range seeking via -ss on the first input)
      • uses the concat filter (not demuxer) with per-input normalization to
        feed the encoder a uniform 1920x1080 yuv420p 30fps / 48 kHz stereo
        stream so no decoder/filter-graph reconfigure happens at scene
        transitions
      • transcodes once to H.264+AAC
      • writes live HLS segments to a per-channel temp directory

    The process is killed automatically after LIVE_TV_IDLE_TIMEOUT seconds
    (default 300 s) of no manifest/segment requests.  Restarted on next play.

    ── Earlier approaches kept for reference ──────────────────────────────
    REDIRECT (best raw quality, no auto-advance):
      # return RedirectResponse(
      #     url=f"{stash_base}/scene/{scene_id}/stream.m3u8?start={seek:.3f}"
      #         + (f"&apikey={api_key}" if api_key else ""),
      #     status_code=302,
      # )

    SLIDING-WINDOW PROXY (auto-advance works, Stash session restarts caused freezes):
      # Fetched Stash m3u8 once per scene_id, served rolling 30-s window of
      # absolute segment URLs, stripped EXT-X-ENDLIST.  Worked until Stash's
      # FFmpeg session expired and old segment URLs became invalid.
    ───────────────────────────────────────────────────────────────────────
    """
    channel_id = request.path_params.get("channel_id", "")
    channel_id_clean = channel_id.replace("-", "")
    is_manifest = request.url.path.lower().endswith(".m3u8")

    ch = _stash_channel_map.get(channel_id) or _stash_channel_map.get(channel_id_clean)
    if not ch:
        await _get_stash_channels()
        ch = _stash_channel_map.get(channel_id) or _stash_channel_map.get(channel_id_clean)
    if not ch:
        logger.warning(f"LiveTV: stash-stream — unknown channel {channel_id}")
        return Response(status_code=404)

    if is_manifest:
        logger.debug(f"LiveTV: stash-stream manifest requested for '{ch.get('name')}' ({channel_id_clean})")

    await _ensure_stash_schedules()

    playlist_result = await _build_stash_channel_playlist(ch)
    if playlist_result is None:
        logger.warning(f"LiveTV: stash-stream — no current program for {ch['tvg_id']}")
        return Response(status_code=404)
    _entries, seek = playlist_result

    ok = await _ffmpeg_manager.ensure(channel_id_clean, ch, seek)
    if not ok:
        return Response(status_code=502, content="FFmpeg failed to start")

    _ffmpeg_manager.touch(channel_id_clean)

    manifest_path = _ffmpeg_manager.manifest_path(channel_id_clean)
    if not manifest_path:
        return Response(status_code=502)

    try:
        with open(manifest_path, "r", encoding="utf-8") as fh:
            raw = fh.read()
    except OSError:
        return Response(status_code=502)

    # Rewrite relative segment filenames → absolute URLs through our proxy.
    # Strip #EXT-X-DISCONTINUITY: all content is normalized to the same
    # codec/resolution/fps so there's no actual discontinuity.  The tag causes
    # Android TV's hardware H.264 decoder to tear down and reinitialize (~20s
    # freeze per scene transition) even though the codec parameters are identical.
    # We no longer inject #EXT-X-SERVER-CONTROL:HOLD-BACK — real Jellyfin/Tunarr
    # do not use it.  #EXT-X-PROGRAM-DATE-TIME (added via the FFmpeg
    # program_date_time hls_flag) is the correct live-edge anchor and replaces
    # that hack.
    base = f"{request.url.scheme}://{request.url.netloc}"
    out_lines = []
    for line in raw.splitlines():
        s = line.strip()
        if s == "#EXT-X-DISCONTINUITY":
            continue
        if s and not s.startswith("#"):
            out_lines.append(f"{base}/livetv/channels/{channel_id_clean}/seg/{s}")
        else:
            out_lines.append(line)

    logger.trace(
        f"LiveTV FFmpeg: served manifest for '{ch['name']}' "
        f"channel={channel_id_clean} seek={seek:.1f}s entries={len(_entries)}"
    )
    return Response(
        "\n".join(out_lines),
        media_type="application/vnd.apple.mpegurl",
        headers={"Cache-Control": "no-cache, no-store", "Access-Control-Allow-Origin": "*"},
    )


async def endpoint_stash_channel_segment(request: Request):
    """Serve one FFmpeg-generated HLS segment for a live Stash channel."""
    channel_id = request.path_params.get("channel_id", "").replace("-", "")
    seg_name   = request.path_params.get("seg_name",   "")

    if not re.match(r"^seg\d+\.ts$", seg_name):
        return Response(status_code=400)

    seg_dir = _ffmpeg_manager.seg_dir(channel_id)
    if not seg_dir:
        return Response(status_code=404)

    seg_path = os.path.join(seg_dir, seg_name)
    if not os.path.exists(seg_path):
        return Response(status_code=404)

    _ffmpeg_manager.touch(channel_id)

    async def _iter():
        with open(seg_path, "rb") as fh:
            while chunk := fh.read(65536):
                yield chunk

    return StreamingResponse(
        _iter(),
        media_type="video/mp2t",
        headers={"Access-Control-Allow-Origin": "*"},
    )


# Public lookup API (used by metadata_routes and stream_routes)
# ---------------------------------------------------------------------------

def _normalize_id(item_id: str) -> str:
    """Jellyfin SDK normalizes item IDs to UUID format (with hyphens) before
    putting them in request paths.  Strip hyphens so lookups always work
    regardless of which format arrives."""
    return item_id.replace("-", "")


_STASH_PREFIXES = (b"scene-", b"root-", b"tag-", b"filter-",
                   b"studio-", b"year-", b"person-", b"performer-")

def _is_stash_item(item_id: str) -> bool:
    """Return True if this encoded ID decodes to a known Stash (non-Live TV) prefix.
    Used to silently skip the channel/program lookup for ordinary library items."""
    normalized = _normalize_id(item_id)
    try:
        decoded = bytes.fromhex(normalized[:32].ljust(32, "0"))
        return any(decoded.startswith(p) for p in _STASH_PREFIXES)
    except Exception:
        return False


async def get_channel_by_jellyfin_id(item_id: str) -> dict | None:
    from core.jellyfin_mapper import decode_id
    if _is_stash_item(item_id):
        return None
    normalized = _normalize_id(item_id)
    ch = _channel_info_map.get(item_id) or _channel_info_map.get(normalized)
    if ch is not None:
        return ch
    await _get_channels()
    if getattr(config, "ENABLE_STASH_CHANNELS", False):
        await _get_stash_channels()
    ch = _channel_info_map.get(item_id) or _channel_info_map.get(normalized)
    if ch is not None:
        return ch
    # Fallback: decode the ID — if it resolves to "ch-{tvg_id}", look up by tvg_id
    # directly. Handles any edge case where the encoded ID isn't in the map yet.
    decoded = decode_id(item_id)
    if decoded.startswith("ch-"):
        tvg_id = decoded[3:]
        tunarr = _m3u_cache.get("data") or []
        stash = _stash_channels_cache.get("data") or []
        ch = next((c for c in tunarr + stash if c.get("tvg_id") == tvg_id), None)
        if ch:
            logger.debug(f"LiveTV: channel lookup via decoded tvg_id {tvg_id!r}")
            return ch
    # Ultimate fallback: registry lookup for MD5-hashed IDs (long tvg_ids that exceed the
    # 32-char hex limit and can't be reversed through decode_id).
    tvg_id = _channel_tvgid_map.get(normalized)
    if tvg_id:
        tunarr = _m3u_cache.get("data") or []
        stash = _stash_channels_cache.get("data") or []
        ch = next((c for c in tunarr + stash if c.get("tvg_id") == tvg_id), None)
        if ch:
            logger.debug(f"LiveTV: channel lookup via registry for {normalized!r} → tvg_id {tvg_id!r}")
            return ch
    logger.debug(f"LiveTV: channel lookup MISS for {item_id} (map has {len(_channel_info_map)} entries)")
    return None


async def get_program_by_jellyfin_id(item_id: str) -> dict | None:
    if _is_stash_item(item_id):
        return None
    normalized = _normalize_id(item_id)

    # 1. Fast path: Stash-specific map (never cleared by XMLTV refreshes)
    prog = _stash_program_map.get(normalized)
    logger.debug(f"LiveTV: program lookup L1 for {normalized}: {'HIT' if prog is not None else f'MISS (map has {len(_stash_program_map)} entries)'}")
    if prog is not None:
        return prog

    # 2. Shared map (Tunarr programs + any Stash entries not yet cleared)
    prog = _program_info_map.get(item_id) or _program_info_map.get(normalized)
    if prog is not None:
        return prog

    # 3. Search _stash_schedule directly — handles cold-start where endpoint_programs
    #    hasn't been called yet and _stash_program_map is empty.
    if _stash_schedule:
        from datetime import datetime as _dt, timezone as _tz
        for tvg_id, schedule in _stash_schedule.items():
            for entry in schedule:
                start_str = _dt.fromtimestamp(entry["start_ts"], _tz.utc).strftime("%Y-%m-%dT%H:%M:%S.0000000Z")
                pid = encode_id("program", f"{tvg_id}|{start_str}").replace("-", "")
                if pid == normalized:
                    raw_prog = {
                        "channel_id": tvg_id,
                        "title": entry["title"],
                        "start": start_str,
                        "stop": _dt.fromtimestamp(entry["stop_ts"], _tz.utc).strftime("%Y-%m-%dT%H:%M:%S.0000000Z"),
                        "start_ts": entry["start_ts"],
                        "stop_ts": entry["stop_ts"],
                        "run_time_ticks": int(entry["duration_sec"] * 10_000_000),
                        "genre": entry.get("genre", ""), "desc": "",
                        "scene_id": entry.get("scene_id"),
                        "icon": _stash_screenshot_url(entry["scene_id"]) if entry.get("scene_id") else "",
                    }
                    _stash_program_map[normalized] = raw_prog  # cache for future lookups
                    return raw_prog

    # 4. Fall back to XMLTV/Tunarr refresh (does not affect _stash_program_map)
    await _get_programs()
    prog = _program_info_map.get(item_id) or _program_info_map.get(normalized)
    if prog:
        return prog
    # 5. Registry lookup for MD5-hashed program IDs (long channel_id|start strings).
    key = _program_tvgkey_map.get(normalized)
    if key:
        channel_id, start = key
        programs = _xmltv_cache.get("data") or []
        prog = next((p for p in programs if p.get("channel_id") == channel_id and p.get("start") == start), None)
        if prog:
            logger.debug(f"LiveTV: program lookup via registry for {normalized!r} → {channel_id}|{start}")
            return prog
    logger.debug(f"LiveTV: program lookup MISS for {item_id} (map has {len(_program_info_map)} entries)")
    return None


# ---------------------------------------------------------------------------
# Jellyfin format helpers
# ---------------------------------------------------------------------------

def _current_program_for(tvg_id: str, programs: list[dict],
                          server_id: str, channels_by_tvg_id: dict) -> dict | None:
    now_ts = time.time()
    for prog in programs:
        if (prog["channel_id"] == tvg_id
                and prog.get("start_ts") and prog.get("stop_ts")
                and prog["start_ts"] <= now_ts <= prog["stop_ts"]):
            return _program_to_jellyfin(prog, server_id, channels_by_tvg_id)
    return None


def _channel_to_jellyfin(ch: dict, server_id: str, item_id: str | None = None,
                          current_program: dict | None = None) -> dict:
    if item_id is None:
        item_id = encode_id("ch", ch["tvg_id"])
    # Id and ItemId must be non-hyphenated (Jellyfin normalizes on the way in but stores raw)
    item_id = item_id.replace("-", "")
    logo = ch.get("logo", "")
    custom = _custom_logo_path(ch.get("tvg_id", ""))
    if custom:
        tag = hashlib.md5(f"custom:{ch['tvg_id']}:{os.path.getmtime(custom):.0f}".encode()).hexdigest()
    elif logo:
        tag = _logo_tag(logo)
    else:
        tag = ""
    num = ch.get("number", "")
    sort_name = f"{str(num).zfill(5)}.0-{ch['name']}"
    livetv_parent = encode_id("root", "livetv")

    item: dict = {
        "Name": ch["name"],
        "ServerId": server_id,
        "Id": item_id,
        "Etag": hashlib.md5(item_id.encode()).hexdigest(),
        "ChannelId": None,
        "Number": num,
        "ChannelNumber": num,
        "SortName": sort_name,
        "IsFolder": False,
        "Type": "TvChannel",
        "ChannelType": "TV",
        "MediaType": "Video",
        "LocationType": "Remote",
        "PrimaryImageAspectRatio": 1.0,
        "ImageTags": {"Primary": tag} if tag else {},
        "ImageBlurHashes": {},
        "BackdropImageTags": [],
        "UserData": {
            "PlaybackPositionTicks": 0,
            "PlayCount": 0,
            "IsFavorite": False,
            "Played": False,
            "Key": _to_uuid_key(item_id),
            "ItemId": item_id,
        },
        # Full-detail fields (harmless in list context)
        "ParentId": livetv_parent,
        "EnableMediaSourceDisplay": True,
        "PlayAccess": "Full",
        "CanRecord": False,
        "CanDelete": False,
        "CanDownload": False,
        "ExternalUrls": [],
        "ProviderIds": {},
        "People": [],
        "Studios": [],
        "GenreItems": [],
        "Genres": [],
        "Tags": [],
        "Taglines": [],
        "RemoteTrailers": [],
        "MediaStreams": [],
        "LockedFields": [],
        "LockData": False,
        "LocalTrailerCount": 0,
        "SpecialFeatureCount": 0,
        "MediaSources": [
            {
                "Protocol": "File",
                "Id": item_id,
                "Type": "Placeholder",
                "Name": ch["name"],
                "IsRemote": False,
                "ReadAtNativeFramerate": False,
                "IgnoreDts": False,
                "IgnoreIndex": False,
                "GenPtsInput": False,
                "SupportsTranscoding": True,
                "SupportsDirectStream": True,
                "SupportsDirectPlay": True,
                "IsInfiniteStream": True,
                "UseMostCompatibleTranscodingProfile": False,
                "RequiresOpening": False,
                "RequiresClosing": False,
                "RequiresLooping": False,
                "SupportsProbing": True,
                "MediaStreams": [],
                "MediaAttachments": [],
                "Formats": [],
                "RequiredHttpHeaders": {},
                "TranscodingSubProtocol": "http",
                "HasSegments": False,
            }
        ],
    }
    if current_program is not None:
        item["CurrentProgram"] = current_program
    return item


def _program_to_jellyfin(prog: dict, server_id: str, channels_by_tvg_id: dict,
                          prog_id: str | None = None) -> dict:
    ch = channels_by_tvg_id.get(prog["channel_id"], {})
    ch_encoded_id = encode_id("ch", prog["channel_id"])
    if prog_id is None:
        prog_id = encode_id("program", f"{prog['channel_id']}|{prog['start']}")

    ch_logo = ch.get("logo", "")
    ch_tag = _logo_tag(ch_logo) if ch_logo else ""

    icon_url = prog.get("icon", "")
    icon_tag = _logo_tag(icon_url) if icon_url else ""

    # UserData.ItemId must be non-hyphenated; Key must be hyphenated UUID
    prog_id_clean = prog_id.replace("-", "")

    item: dict = {
        "Name": prog["title"],
        "ServerId": server_id,
        "Id": prog_id_clean,
        "ChannelId": ch_encoded_id,
        "ChannelName": ch.get("name", ""),
        "ChannelNumber": ch.get("number", ""),
        "Type": "Program",
        "MediaType": "Video",
        "PlayAccess": "Full",
        "CanRecord": False,
        "StartDate": prog["start"],
        "EndDate": prog["stop"],
        "IsRepeat": True,
        "Tags": ["Repeat"],
        "ImageTags": {"Primary": icon_tag} if icon_tag else {},
        "ImageBlurHashes": {},
        "BackdropImageTags": [],
        "UserData": {
            "PlaybackPositionTicks": 0,
            "PlayCount": 0,
            "IsFavorite": False,
            "Played": False,
            "Key": _to_uuid_key(prog_id_clean),
            "ItemId": prog_id_clean,
        },
        "ChannelPrimaryImageTag": ch_tag,
        "ParentId": ch_encoded_id,
        "ExternalUrls": [],
        "ProviderIds": {},
        "People": [],
        "Studios": [],
        "GenreItems": [],
        "Genres": [prog["genre"]] if prog.get("genre") else [],
        "Taglines": [],
        "RemoteTrailers": [],
        "LockedFields": [],
        "LockData": False,
        # Boolean type flags — Jellyfin Web and Wholphin use these (not Genres) for EPG color coding.
        "IsMovie":  prog.get("genre") == "Movie",
        "IsKids":   prog.get("genre") == "Kids",
        "IsSports": prog.get("genre") == "Sports",
        "IsNews":   prog.get("genre") == "News",
        "IsSeries": prog.get("genre") not in ("Movie", ""),
    }

    if icon_tag:
        item["PrimaryImageAspectRatio"] = 1.7777777777777777
    if prog.get("run_time_ticks"):
        item["RunTimeTicks"] = prog["run_time_ticks"]
    if prog.get("year"):
        item["ProductionYear"] = prog["year"]
    if prog.get("desc"):
        item["Overview"] = prog["desc"]

    return item


def channel_playback_info(ch: dict, item_id: str, request=None) -> JSONResponse:
    """PlaybackInfo for a Tunarr TvChannel.

    Advertises an HLS TranscodingUrl (SubProtocol=hls) and disables direct
    play, so the client uses hls.js / ExoPlayer HlsMediaSource pointed at our
    proxied .m3u8 instead of requesting /Videos/{id}/stream?static=true.
    """
    item_id = item_id.replace("-", "")
    play_session_id = f"live_{item_id}"

    transcode_url = f"/livetv/channels/{item_id}/stream.m3u8"
    logger.info(f"LiveTV: channel_playback_info for {ch.get('name')} ({item_id}) → {transcode_url}")

    source: dict = {
        "Protocol": "Http",
        "Id": item_id,
        "Path": transcode_url,
        "Type": "Default",
        "Name": ch.get("name", "Live"),
        "IsRemote": False,
        "ReadAtNativeFramerate": True,
        "IgnoreDts": False,
        "IgnoreIndex": False,
        "GenPtsInput": False,
        "SupportsTranscoding": True,
        "SupportsDirectStream": False,
        "SupportsDirectPlay": False,
        "IsInfiniteStream": True,
        "IsLive": True,
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
             "DisplayTitle": "SDR", "IsInterlaced": False, "IsDefault": False,
             "IsForced": False, "IsHearingImpaired": False, "Type": "Video", "Index": -1,
             "IsExternal": False, "IsTextSubtitleStream": False, "SupportsExternalStream": False},
            {"VideoRange": "Unknown", "VideoRangeType": "Unknown", "AudioSpatialFormat": "None",
             "DisplayTitle": "", "IsInterlaced": False, "IsDefault": False,
             "IsForced": False, "IsHearingImpaired": False, "Type": "Audio", "Index": -1,
             "IsExternal": False, "IsTextSubtitleStream": False, "SupportsExternalStream": False},
        ],
        "MediaAttachments": [],
        "Formats": [],
        "RequiredHttpHeaders": {},
        "DefaultAudioStreamIndex": -1,
        "HasSegments": False,
    }

    return JSONResponse({
        "MediaSources": [source],
        "PlaySessionId": play_session_id,
    })


async def endpoint_live_streams_open(request: Request):
    """POST /LiveStreams/Open — stub.

    PlaybackInfo uses RequiresOpening=False so no client calls this in normal
    flow.  Registered to prevent 404s from clients with stale cached state.
    """
    try:
        data = await request.json()
    except Exception:
        data = {}
    open_token = (data.get("OpenToken") or request.query_params.get("OpenToken", "")).strip()
    logger.info(f"LiveTV: POST /LiveStreams/Open (unexpected) token={open_token!r}")
    return Response(status_code=204)


async def endpoint_live_streams_close(request: Request):
    try:
        data = await request.json()
    except Exception:
        data = {}
    live_stream_id = data.get("LiveStreamId") or request.query_params.get("LiveStreamId", "")
    logger.info(f"LiveTV: POST /LiveStreams/Close id={live_stream_id!r}")
    return Response(status_code=204)


async def endpoint_live_streams_ping(request: Request):
    live_stream_id = request.query_params.get("LiveStreamId", "")
    logger.debug(f"LiveTV: POST /LiveStreams/Ping id={live_stream_id!r}")
    return Response(status_code=204)


async def endpoint_channel_m3u8(request: Request):
    """Proxy the Tunarr HLS playlist through our server.

    Rewrites relative and origin-relative segment URLs to absolute Tunarr URLs
    so clients can fetch segments directly.  Serving the playlist from our
    origin eliminates browser CORS issues; the .m3u8 extension ensures
    ExoPlayer and hls.js select the correct player automatically.
    """
    from urllib.parse import urlparse, urljoin

    channel_id = request.path_params.get("channel_id", "")
    stream_url = _channel_stream_map.get(channel_id)
    if not stream_url:
        await _get_channels()
        stream_url = _channel_stream_map.get(channel_id)
    if not stream_url:
        logger.warning(f"LiveTV: m3u8 proxy — no stream URL for channel {channel_id}")
        return Response(status_code=404)

    try:
        resp = await _live_client.get(stream_url, timeout=10.0)
        final_url = str(resp.url)
        if final_url != stream_url:
            logger.debug(f"LiveTV: Tunarr redirected {stream_url} -> {final_url}")
        if resp.status_code != 200:
            logger.warning(f"LiveTV: Tunarr returned {resp.status_code} for {final_url}")
            return Response(status_code=resp.status_code)

        resolved_url = final_url
        parsed = urlparse(resolved_url)
        tunarr_origin = f"{parsed.scheme}://{parsed.netloc}"
        base_path = resolved_url.split("?")[0].rsplit("/", 1)[0] + "/"

        lines = []
        for line in resp.text.splitlines():
            stripped = line.strip()
            if stripped and not stripped.startswith("#"):
                if stripped.startswith("http://") or stripped.startswith("https://"):
                    lines.append(stripped)
                elif stripped.startswith("/"):
                    lines.append(tunarr_origin + stripped)
                else:
                    lines.append(urljoin(base_path, stripped))
            else:
                lines.append(line)

        logger.debug(f"LiveTV: proxied m3u8 for channel {channel_id}")
        return Response(
            content="\n".join(lines),
            media_type="application/vnd.apple.mpegurl",
            headers={"Access-Control-Allow-Origin": "*", "Cache-Control": "no-cache, no-store"},
        )
    except Exception as e:
        logger.error(f"LiveTV: m3u8 proxy failed for channel {channel_id}: {e}")
        return Response(status_code=500)


# ---------------------------------------------------------------------------
# Route handlers
# ---------------------------------------------------------------------------

async def endpoint_program_detail(request: Request):
    program_id = request.path_params.get("program_id", "")
    logger.notice(f"LiveTV: GET /livetv/programs/{program_id}")
    prog = await get_program_by_jellyfin_id(program_id)
    if prog is None:
        return Response(status_code=404)
    server_id = getattr(config, "SERVER_ID", "stash-proxy")
    tunarr_channels = await _get_channels() if getattr(config, "ENABLE_TUNARR", False) else []
    stash_channels = await _get_stash_channels() if getattr(config, "ENABLE_STASH_CHANNELS", False) else []
    channels_by_tvg_id = {ch["tvg_id"]: ch for ch in tunarr_channels + stash_channels}
    return JSONResponse(_program_to_jellyfin(prog, server_id, channels_by_tvg_id, program_id))


async def endpoint_timer_defaults(request: Request):
    logger.debug("LiveTV: GET /livetv/timers/defaults")
    server_id = getattr(config, "SERVER_ID", "stash-proxy")
    return JSONResponse({
        "Type": "SeriesTimer",
        "RecordAnyChannel": False,
        "RecordAnyTime": True,
        "RecordNewOnly": False,
        "KeepUntil": "UntilDeleted",
        "Priority": 0,
        "IsPrePaddingRequired": False,
        "IsPostPaddingRequired": False,
        "PrePaddingSeconds": 0,
        "PostPaddingSeconds": 0,
        "SkipEpisodesInLibrary": False,
        "EnabledByDefault": False,
        "ImageTags": {},
        "BackdropImageTags": [],
        "Id": "",
        "ServerId": server_id,
    })


async def endpoint_recordings_folders(request: Request):
    logger.debug("LiveTV: GET /livetv/recordings/folders")
    return JSONResponse({"Items": [], "TotalRecordCount": 0, "StartIndex": 0})


async def endpoint_live_tv_info(request: Request):
    logger.debug("LiveTV: GET /livetv/info")
    m3u_url = getattr(config, "TUNER_M3U_URL", "")
    stash_enabled = getattr(config, "ENABLE_STASH_CHANNELS", False)
    services = []
    if getattr(config, "ENABLE_TUNARR", False):
        services.append({
            "Name": "Tunarr Passthrough",
            "HomePageUrl": m3u_url or "",
            "Status": "Running" if m3u_url else "Unavailable",
            "IsVisible": True,
            "HasCancelTimer": False,
            "HasProgramImages": True,
            "HasSeriesTimer": False,
            "CanCreateSeriesTimers": False,
            "CanSetRecordingPath": False,
            "SupportsDirectStreamImport": False,
            "SupportsRecordings": False,
        })
    if stash_enabled:
        services.append({
            "Name": "Stash Dynamic Channels",
            "HomePageUrl": "",
            "Status": "Running",
            "IsVisible": True,
            "HasCancelTimer": False,
            "HasProgramImages": False,
            "HasSeriesTimer": False,
            "CanCreateSeriesTimers": False,
            "CanSetRecordingPath": False,
            "SupportsDirectStreamImport": False,
            "SupportsRecordings": False,
        })
    return JSONResponse({
        "Services": services,
        "IsEnabled": _live_tv_enabled(),
        "HasRecordingSupport": False,
        "EnabledUsers": [],
    })


async def endpoint_channels(request: Request):
    logger.notice(f"LiveTV: GET /livetv/channels params={dict(request.query_params)}")
    server_id = getattr(config, "SERVER_ID", "stash-proxy")

    tunarr_channels = await _get_channels() if getattr(config, "ENABLE_TUNARR", False) else []
    stash_channels = await _get_stash_channels() if getattr(config, "ENABLE_STASH_CHANNELS", False) else []
    all_channels = tunarr_channels + stash_channels
    logger.notice(f"LiveTV: returning {len(all_channels)} channels ({len(tunarr_channels)} Tunarr, {len(stash_channels)} Stash)")

    add_current = request.query_params.get("addCurrentProgram", "").lower() == "true"
    tunarr_programs: list[dict] = []
    channels_by_tvg_id: dict = {ch["tvg_id"]: ch for ch in all_channels}
    if add_current and tunarr_channels:
        tunarr_programs = await _get_programs()

    items = []
    for ch in all_channels:
        eid = encode_id("ch", ch["tvg_id"])
        current = None
        if add_current:
            if ch.get("stash_type"):
                now = time.time()
                sched = _stash_schedule.get(ch["tvg_id"], [])
                entry = next((e for e in sched if e["start_ts"] <= now <= e["stop_ts"]), None)
                if entry:
                    raw = {"channel_id": ch["tvg_id"], "title": entry["title"],
                           "start": datetime.fromtimestamp(entry["start_ts"], timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.0000000Z"),
                           "stop": datetime.fromtimestamp(entry["stop_ts"], timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.0000000Z"),
                           "start_ts": entry["start_ts"], "stop_ts": entry["stop_ts"],
                           "run_time_ticks": int(entry["duration_sec"] * 10_000_000), "genre": "", "desc": ""}
                    prog_id = encode_id("program", f"{ch['tvg_id']}|{raw['start']}").replace("-", "")
                    _stash_program_map[prog_id] = raw
                    logger.debug(f"LiveTV: addCurrentProgram stored key={prog_id} for ch={ch['tvg_id']} start={raw['start']}")
                    current = _program_to_jellyfin(raw, server_id, channels_by_tvg_id)
            else:
                current = _current_program_for(ch["tvg_id"], tunarr_programs, server_id, channels_by_tvg_id)
        items.append(_channel_to_jellyfin(ch, server_id, eid, current))

    return JSONResponse({"Items": items, "TotalRecordCount": len(items), "StartIndex": 0})


async def endpoint_channel_single(request: Request):
    """Return a single TvChannel object by encoded ID.

    Jellyfin Android calls GET /LiveTv/Channels/{Id} for individual channel
    lookups.  Without this route the request falls to the blackhole which
    returns Items:[] — Jellyfin Android throws InvalidContentException and
    crashes the guide view.
    """
    channel_id = request.path_params.get("channel_id", "").replace("-", "")
    server_id = getattr(config, "SERVER_ID", "stash-proxy")

    tunarr_channels = await _get_channels() if getattr(config, "ENABLE_TUNARR", False) else []
    stash_channels = await _get_stash_channels() if getattr(config, "ENABLE_STASH_CHANNELS", False) else []

    for ch in tunarr_channels + stash_channels:
        eid = encode_id("ch", ch["tvg_id"]).replace("-", "")
        if eid == channel_id:
            return JSONResponse(_channel_to_jellyfin(ch, server_id, eid))

    return Response(status_code=404)


async def endpoint_channel_now_playing(request: Request):
    """Return the currently playing scene for an active Stash channel's FFmpeg stream.

    Query params:
        tvg_id: channel tvg_id (e.g. "shorts")
    """
    tvg_id = request.query_params.get("tvg_id", "")
    if not tvg_id:
        return JSONResponse({"active": False, "error": "tvg_id required"}, status_code=400)

    enc = encode_id("ch", tvg_id).replace("-", "")
    if not _ffmpeg_manager.is_alive(enc):
        return JSONResponse({"active": False})

    stash_channels = await _get_stash_channels()
    ch = next((c for c in stash_channels if c["tvg_id"] == tvg_id), None)
    scene_info = _ffmpeg_manager.get_scene_at(enc, ch)
    if not scene_info:
        return JSONResponse({"active": True})

    return JSONResponse({"active": True, **scene_info})


async def endpoint_shorts_block_preview(request: Request):
    """Return the scene list for a Shorts EPG block, read from the stored schedule.

    Query params:
        ts:     block start Unix timestamp (matched against stored block start_ts)
        tvg_id: (optional) channel tvg_id; defaults to first shorts channel found
    """
    ts_str = request.query_params.get("ts", "")
    tvg_id = request.query_params.get("tvg_id", "")
    try:
        block_start = float(ts_str)
    except (ValueError, TypeError):
        return JSONResponse({"error": "invalid ts"}, status_code=400)

    if not tvg_id:
        stash_channels = await _get_stash_channels()
        sch = next((c for c in stash_channels if c.get("stash_type") == "shorts"), None)
        if sch:
            tvg_id = sch["tvg_id"]

    schedule = _stash_schedule.get(tvg_id, []) if tvg_id else []
    block = next((b for b in schedule if abs(b.get("start_ts", 0) - block_start) < 5), None)
    if not block:
        return JSONResponse({"scenes": []})

    scenes = [{
        "id":           seg["scene_id"],
        "title":        seg.get("title", ""),
        "start_ts":     seg["start_ts"],
        "stop_ts":      seg["stop_ts"],
        "duration_sec": seg["duration_sec"],
    } for seg in (block.get("segments") or [])]

    return JSONResponse({
        "block_start": block["start_ts"],
        "block_end":   block["stop_ts"],
        "scenes":      scenes,
    })


async def endpoint_programs(request: Request):
    logger.notice(f"LiveTV: {request.method} /livetv/programs params={dict(request.query_params)}")
    server_id = getattr(config, "SERVER_ID", "stash-proxy")

    tunarr_channels = await _get_channels() if getattr(config, "ENABLE_TUNARR", False) else []
    tunarr_programs = await _get_programs() if getattr(config, "ENABLE_TUNARR", False) else []
    stash_channels = await _get_stash_channels() if getattr(config, "ENABLE_STASH_CHANNELS", False) else []

    if getattr(config, "ENABLE_STASH_CHANNELS", False):
        await _ensure_stash_schedules()

    all_channels = tunarr_channels + stash_channels
    channels_by_tvg_id = {ch["tvg_id"]: ch for ch in all_channels}

    # Combine raw program dicts for filtering; Stash programs have a scene_id field
    programs: list[dict] = list(tunarr_programs)
    for ch in stash_channels:
        tvg_id = ch["tvg_id"]
        for entry in _stash_schedule.get(tvg_id, []):
            raw_prog = {
                "channel_id": tvg_id,
                "title": entry["title"],
                "start": datetime.fromtimestamp(entry["start_ts"], timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.0000000Z"),
                "stop": datetime.fromtimestamp(entry["stop_ts"], timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.0000000Z"),
                "start_ts": entry["start_ts"],
                "stop_ts": entry["stop_ts"],
                "run_time_ticks": int(entry["duration_sec"] * 10_000_000),
                "genre": entry.get("genre", ""), "desc": "", "scene_id": entry.get("scene_id"),
                "icon": _stash_screenshot_url(entry["scene_id"]) if entry.get("scene_id") else "",
            }
            prog_id = encode_id("program", f"{tvg_id}|{raw_prog['start']}")
            pid_norm = prog_id.replace("-", "")
            # Register for single-item lookup in both maps.
            # _stash_program_map is never cleared by XMLTV refreshes so the entry
            # survives concurrent/subsequent _get_programs() calls.
            _program_info_map[pid_norm] = raw_prog
            _stash_program_map[pid_norm] = raw_prog
            programs.append(raw_prog)

    # POST body may carry filters as JSON (Wholphin sends POST instead of GET)
    body: dict = {}
    if request.method == "POST":
        try:
            body = await request.json()
        except Exception:
            body = {}

    def _qp(key: str, default: str = "") -> str:
        """Check query params first, then POST body (case-insensitive)."""
        val = next((v for k, v in request.query_params.items() if k.lower() == key.lower()), None)
        if val is not None:
            return val
        return str(body.get(key, body.get(key.lower(), default)))

    # Channel filter — ChannelIds may be:
    #   • repeated query params:  ?channelIds=a&channelIds=b  (Jellyfin Android TV)
    #   • a single comma-sep value: ?channelIds=a,b
    #   • a JSON array in a POST body (Wholphin)
    # Starlette's query_params.items() deduplicates keys (last value wins), so we
    # use multi_items() to capture every occurrence of channelIds.
    requested: set[str] = set()
    all_qs_channel_ids = [v for k, v in request.query_params.multi_items() if k.lower() == "channelids"]
    if all_qs_channel_ids:
        for val in all_qs_channel_ids:
            requested.update(val.split(","))
    else:
        body_ids = body.get("ChannelIds", body.get("channelIds", body.get("channelids")))
        if isinstance(body_ids, list):
            requested = set(str(x) for x in body_ids)
        elif isinstance(body_ids, str) and body_ids:
            requested = set(body_ids.split(","))
    # Normalize to unhyphenated hex so hyphenated UUID IDs from clients still match.
    # Drop sentinel values that clients send when they mean "no filter" (e.g. "null").
    _SENTINEL_IDS = {"null", "undefined", "", "0"}
    requested = {r.replace("-", "") for r in requested if r.lower() not in _SENTINEL_IDS}
    logger.debug(f"LiveTV: programs channel filter requested={requested or 'ALL'}")
    if requested:
        wanted = {tvg for tvg in channels_by_tvg_id
                  if encode_id("ch", tvg).replace("-", "") in requested}
        logger.debug(f"LiveTV: programs channel filter matched tvg_ids={wanted}")
        programs = [p for p in programs if p["channel_id"] in wanted]

    # Time filters
    now_ts = time.time()
    is_airing = _qp("IsAiring", "").lower()
    has_aired = _qp("HasAired", "").lower()

    # Guide time-window filter.  Jellyfin Web sends MaxStartDate/MinEndDate for
    # the visible window.  Clients like Wholphin send neither and get the full
    # schedule — 8000+ items — which overwhelms mobile/TV clients and causes
    # channels (especially shorts) to silently drop from the guide.
    # Default to a 14-hour window (2h past → 12h future) when no params arrive.
    def _parse_guide_ts(raw: str) -> float | None:
        raw = raw.strip()
        if not raw:
            return None
        try:
            raw = raw.replace("Z", "+00:00") if raw.endswith("Z") else raw
            return datetime.fromisoformat(raw).timestamp()
        except Exception:
            return None

    max_start_ts = _parse_guide_ts(_qp("MaxStartDate", ""))
    min_end_ts   = _parse_guide_ts(_qp("MinEndDate",   ""))

    # Guide time-window capping strategy:
    # - If no date range provided (Wholphin home-screen widgets, old clients): default to a
    #   14h window (2h past → 12h future) so we don't flood with thousands of short-clip entries.
    # - If an explicit MaxStartDate is provided (Jellyfin Web guide, Wholphin EPG): honour it
    #   so the full guide day renders. Cap at 48h absolute max to prevent absurdly large responses.
    DEFAULT_FORWARD   = 43_200    # 12h — used only when client sends no MaxStartDate
    GUIDE_FORWARD_MAX = 172_800   # 48h — hard ceiling even when client provides a date
    GUIDE_PAST_CAP    =  7_200    #  2h back

    if max_start_ts is None:
        max_start_ts = now_ts + DEFAULT_FORWARD
    elif max_start_ts > now_ts + GUIDE_FORWARD_MAX:
        max_start_ts = now_ts + GUIDE_FORWARD_MAX
    if min_end_ts is None or min_end_ts < now_ts - GUIDE_PAST_CAP:
        min_end_ts = now_ts - GUIDE_PAST_CAP

    programs = [p for p in programs if p.get("start_ts", 0) <= max_start_ts]
    programs = [p for p in programs if p.get("stop_ts", now_ts) >= min_end_ts]

    if is_airing == "true":
        programs = [p for p in programs
                    if p.get("start_ts") and p.get("stop_ts")
                    and p["start_ts"] <= now_ts <= p["stop_ts"]]
    elif is_airing == "false":
        programs = [p for p in programs
                    if not (p.get("start_ts") and p.get("stop_ts")
                            and p["start_ts"] <= now_ts <= p["stop_ts"])]

    if has_aired == "false":
        programs = [p for p in programs if p.get("stop_ts", 0) > now_ts]
    elif has_aired == "true":
        programs = [p for p in programs if p.get("stop_ts", now_ts + 1) <= now_ts]

    # Genre filters — used by Jellyfin/Wholphin home-screen carousels
    _GENRE_PARAM_MAP = {
        "IsMovie":       ("true",  "Movie"),
        "IsSports":      ("true",  "Sports"),
        "IsKids":        ("true",  "Kids"),
        "IsNews":        ("true",  "News"),
        "IsSeries":      ("true",  None),   # "Series" means non-Movie in Jellyfin
        "IsMovie_false": ("false", "Movie"),
    }
    is_movie = _qp("IsMovie",  "").lower()
    is_sports = _qp("IsSports", "").lower()
    is_kids   = _qp("IsKids",   "").lower()
    is_news   = _qp("IsNews",   "").lower()
    is_series = _qp("IsSeries", "").lower()
    if is_movie == "true":
        programs = [p for p in programs if p.get("genre") == "Movie"]
    elif is_movie == "false":
        programs = [p for p in programs if p.get("genre") != "Movie"]
    if is_sports == "true":
        programs = [p for p in programs if p.get("genre") == "Sports"]
    if is_kids == "true":
        programs = [p for p in programs if p.get("genre") == "Kids"]
    if is_news == "true":
        programs = [p for p in programs if p.get("genre") == "News"]
    if is_series == "true":
        programs = [p for p in programs if p.get("genre") not in ("Movie", "")]

    # Pagination
    total = len(programs)
    try:
        start_index = int(_qp("StartIndex", "0"))
    except ValueError:
        start_index = 0
    try:
        limit = int(_qp("Limit", "0"))
    except ValueError:
        limit = 0
    if start_index:
        programs = programs[start_index:]
    if limit:
        programs = programs[:limit]

    items = [_program_to_jellyfin(p, server_id, channels_by_tvg_id) for p in programs]
    logger.notice(f"LiveTV: programs returning {len(items)}/{total} items")
    return JSONResponse({"Items": items, "TotalRecordCount": total, "StartIndex": start_index})


async def endpoint_channel_stream(request: Request):
    channel_id = request.path_params.get("channel_id", "")
    stream_url = _channel_stream_map.get(channel_id)
    if not stream_url:
        await _get_channels()
        stream_url = _channel_stream_map.get(channel_id)
    if not stream_url:
        return Response(status_code=404)
    return RedirectResponse(url=stream_url, status_code=302)


async def endpoint_guide_info(request: Request):
    now = datetime.now(timezone.utc)
    end = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=7)
    return JSONResponse({
        "StartDate": now.strftime("%Y-%m-%dT%H:%M:%S.0000000Z"),
        "EndDate": end.strftime("%Y-%m-%dT%H:%M:%S.0000000Z"),
    })


async def endpoint_recordings(request: Request):
    return JSONResponse({"Items": [], "TotalRecordCount": 0, "StartIndex": 0})


async def endpoint_timers(request: Request):
    return JSONResponse({"Items": [], "TotalRecordCount": 0, "StartIndex": 0})


async def endpoint_series_timers(request: Request):
    return JSONResponse({"Items": [], "TotalRecordCount": 0, "StartIndex": 0})


async def _rebuild_single_channel(tvg_id: str):
    """Rebuild (wipe + regenerate) the schedule for one channel."""
    channels = await _get_stash_channels()
    ch = next((c for c in channels if c["tvg_id"] == tvg_id), None)
    if not ch:
        logger.warning(f"LiveTV: single-channel rebuild — unknown channel {tvg_id}")
        return
    async with _rebuild_lock:
        try:
            scenes = await _fetch_scenes_for_stash_channel(ch)
            if not scenes:
                logger.warning(f"LiveTV: no scenes for '{ch['name']}' — schedule will be empty")
                return
            if ch.get("stash_type") == "shorts":
                _stash_schedule[tvg_id] = _build_shorts_block_schedule(scenes)
            else:
                _stash_schedule[tvg_id] = _build_random_schedule(scenes)
            logger.info(f"LiveTV: rebuilt schedule for '{ch['name']}' — {len(scenes)} scenes")
            _stash_schedule_built_at = time.time()
            _save_schedule()
        except Exception as e:
            logger.error(f"LiveTV: single-channel rebuild failed for '{ch['name']}': {e}", exc_info=True)


# ---------------------------------------------------------------------------
# Channel config CRUD endpoints
# ---------------------------------------------------------------------------

async def endpoint_stash_tags_list(request: Request):
    """Return all Stash tags that have at least one scene."""
    from core.stash_client import get_all_tags
    tags = await get_all_tags()
    return JSONResponse({"tags": [{"id": t["id"], "name": t["name"], "has_image": bool(t.get("image_path"))} for t in tags]})


async def endpoint_stash_tag_image(request: Request):
    """Proxy a Stash tag's image through the server."""
    tag_id = request.path_params.get("tag_id", "")
    if not re.match(r'^\d+$', tag_id):
        return Response(status_code=400)
    stash_base = config.get_stash_base()
    apikey = getattr(config, "STASH_API_KEY", "")
    url = f"{stash_base}/tag/{tag_id}/image"
    if apikey:
        url += f"?apikey={apikey}"
    from api.image_routes import _proxy_image
    return await _proxy_image(url)


async def endpoint_channel_logo_set_from_tag(request: Request):
    """Download a Stash tag's image and save it as the channel logo."""
    tvg_id = request.path_params.get("tvg_id", "")
    if not re.match(r'^[a-zA-Z0-9_-]{1,60}$', tvg_id):
        return JSONResponse({"error": "invalid tvg_id"}, status_code=400)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON"}, status_code=400)
    tag_id = str(body.get("tag_id", "")).strip()
    if not re.match(r'^\d+$', tag_id):
        return JSONResponse({"error": "invalid tag_id"}, status_code=400)

    stash_base = config.get_stash_base()
    apikey = getattr(config, "STASH_API_KEY", "")
    image_url = f"{stash_base}/tag/{tag_id}/image"
    if apikey:
        image_url += f"?apikey={apikey}"

    from api.image_routes import image_client
    try:
        r = await image_client.get(image_url)
        if r.status_code != 200:
            return JSONResponse({"error": "failed to fetch tag image"}, status_code=502)
        content = r.content
        ct = r.headers.get("content-type", "image/jpeg").split(";")[0].strip()
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=502)

    ext_map = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp", "image/gif": ".gif"}
    ext = ext_map.get(ct, ".jpg")
    logo_dir = _logo_dir()
    for existing in glob.glob(os.path.join(logo_dir, f"{tvg_id}.*")):
        os.remove(existing)
    dest = os.path.join(logo_dir, f"{tvg_id}{ext}")
    with open(dest, "wb") as fh:
        fh.write(content)
    _stash_channels_cache["data"] = None
    logger.info(f"LiveTV: tag {tag_id} image saved as logo for '{tvg_id}' → {dest}")
    return JSONResponse({"ok": True})


async def endpoint_stash_filters_list(request: Request):
    """Return all Stash saved scene filters."""
    from core.stash_client import get_saved_filters
    filters = await get_saved_filters()
    return JSONResponse({"filters": [{"id": f["id"], "name": f["name"]} for f in filters]})


async def endpoint_channels_config_list(request: Request):
    """Return ordered channel config list."""
    return JSONResponse({"channels": sorted(_channels_config, key=lambda c: c.get("order", 0))})


async def endpoint_channels_config_create(request: Request):
    """Create a new channel and immediately fire its schedule build in the background."""
    global _channels_config
    body = await request.json()
    name       = str(body.get("name", "")).strip()
    stash_type = str(body.get("stash_type", "tag"))
    source_ids = [str(s) for s in (body.get("source_ids") or [])]
    if not name or (stash_type != "shorts" and not source_ids):
        return JSONResponse({"error": "name and source_ids are required"}, status_code=400)

    # Auto-assign next available channel number
    used_numbers = {int(c["number"]) for c in _channels_config if str(c.get("number", "")).isdigit()}
    start = int(getattr(config, "STASH_CHANNEL_START_NUMBER", 5001))
    requested = body.get("number")
    if requested and str(requested).isdigit():
        number = str(int(requested))
    else:
        n = start
        while n in used_numbers:
            n += 1
        number = str(n)

    tvg_id = "ch_" + os.urandom(4).hex()
    new_cfg = {"tvg_id": tvg_id, "name": name, "number": number,
               "stash_type": stash_type, "source_ids": source_ids,
               "order": len(_channels_config)}
    _channels_config.append(new_cfg)
    _save_channels_config()
    _stash_channels_cache["data"] = None
    _stash_channels_cache["ts"] = 0.0

    # Build the schedule in the background so the response is immediate
    asyncio.create_task(_rebuild_single_channel(tvg_id))
    return JSONResponse({"ok": True, "channel": new_cfg}, status_code=201)


async def endpoint_channels_config_update(request: Request):
    """Update channel metadata (name, number, sources).  Does NOT rebuild the schedule."""
    global _channels_config
    tvg_id = request.path_params.get("tvg_id", "")
    idx = next((i for i, c in enumerate(_channels_config) if c["tvg_id"] == tvg_id), None)
    if idx is None:
        return JSONResponse({"error": "channel not found"}, status_code=404)

    body = await request.json()
    cfg  = _channels_config[idx]
    if "name"       in body: cfg["name"]       = str(body["name"]).strip()
    if "number"     in body: cfg["number"]      = str(body["number"])
    if "stash_type" in body: cfg["stash_type"]  = str(body["stash_type"])
    if "source_ids" in body: cfg["source_ids"]  = [str(s) for s in body["source_ids"]]
    _channels_config[idx] = cfg
    _save_channels_config()
    _stash_channels_cache["data"] = None
    _stash_channels_cache["ts"] = 0.0
    return JSONResponse({"ok": True, "channel": cfg})


async def endpoint_channels_config_delete(request: Request):
    """Delete a channel and its schedule."""
    global _channels_config, _stash_schedule
    tvg_id = request.path_params.get("tvg_id", "")
    before = len(_channels_config)
    _channels_config = [c for c in _channels_config if c["tvg_id"] != tvg_id]
    if len(_channels_config) == before:
        return JSONResponse({"error": "channel not found"}, status_code=404)
    for i, c in enumerate(_channels_config):
        c["order"] = i
    _stash_schedule.pop(tvg_id, None)
    _save_channels_config()
    _save_schedule()
    _stash_channels_cache["data"] = None
    _stash_channels_cache["ts"] = 0.0
    # Remove custom logo if present
    custom = _custom_logo_path(tvg_id)
    if custom and os.path.exists(custom):
        try: os.remove(custom)
        except Exception: pass
    return JSONResponse({"ok": True})


def _renumber_after_reorder(new_order: list[dict], src_id: str, src_old_idx: int) -> None:
    """Assign a new channel number to the moved channel and cascade-shift displaced channels.

    Algorithm:
    1. Look at the new neighbors (above/below in list order) and check if a free
       integer exists in the gap between their numbers → assign it, done.
    2. Otherwise sequential-cascade: collect the numbers of all channels in the
       affected index range (src's old..new positions, inclusive), sort them, then
       assign them in ascending order to those channels sorted by new position.
       This is equivalent to the displaced channels each taking the number from
       their neighbor toward src's origin, cascading until src's vacated slot is
       consumed.
    """
    src_new_idx = next((i for i, c in enumerate(new_order) if c["tvg_id"] == src_id), None)
    if src_new_idx is None or src_new_idx == src_old_idx:
        return

    def _to_int(c: dict) -> int:
        try:
            return int(c.get("number", 0))
        except (ValueError, TypeError):
            return 0

    # All occupied numbers except src's old number (src vacated it)
    src_old_num = _to_int(new_order[src_new_idx])
    occupied = {_to_int(c) for c in new_order if c["tvg_id"] != src_id}

    # Neighbors in new order (using old numbers, not yet reassigned)
    above_num = _to_int(new_order[src_new_idx - 1]) if src_new_idx > 0 else None
    below_num = _to_int(new_order[src_new_idx + 1]) if src_new_idx < len(new_order) - 1 else None

    # Step 1: gap check
    lo_bound = above_num if above_num is not None else 0
    hi_bound = below_num if below_num is not None else lo_bound + 2
    for n in range(lo_bound + 1, hi_bound):
        if n not in occupied:
            new_order[src_new_idx]["number"] = str(n)
            return

    # Step 2: cascade via sequential assignment of affected range
    lo = min(src_old_idx, src_new_idx)
    hi = max(src_old_idx, src_new_idx)
    affected = new_order[lo:hi + 1]   # channels at these new-order positions
    nums = sorted(_to_int(c) for c in affected)
    for i, c in enumerate(affected):
        c["number"] = str(nums[i])


async def endpoint_channels_config_reorder(request: Request):
    """Accept an ordered list of tvg_ids and persist the new sort order + renumber."""
    global _channels_config
    body = await request.json()
    ordered_ids: list[str] = [str(x) for x in (body.get("order") or [])]
    src_id: str = str(body.get("src_id", ""))

    # Remember src's old position before reordering
    old_index = {c["tvg_id"]: i for i, c in enumerate(_channels_config)}
    src_old_idx = old_index.get(src_id, -1)

    cfg_by_id = {c["tvg_id"]: c for c in _channels_config}
    new_order: list[dict] = []
    for i, tid in enumerate(ordered_ids):
        if tid in cfg_by_id:
            cfg_by_id[tid]["order"] = i
            new_order.append(cfg_by_id[tid])
    # Append anything not in the submitted list (shouldn't normally happen)
    present = {c["tvg_id"] for c in new_order}
    for c in _channels_config:
        if c["tvg_id"] not in present:
            c["order"] = len(new_order)
            new_order.append(c)

    # Renumber the moved channel (and cascade-shift displaced ones)
    if src_id and src_old_idx >= 0:
        _renumber_after_reorder(new_order, src_id, src_old_idx)

    _channels_config = new_order
    _save_channels_config()
    _stash_channels_cache["data"] = None
    _stash_channels_cache["ts"] = 0.0
    return JSONResponse({"ok": True})


async def endpoint_channel_rebuild(request: Request):
    """Wipe and rebuild the schedule for a single channel."""
    tvg_id = request.path_params.get("tvg_id", "")
    if not any(c["tvg_id"] == tvg_id for c in _channels_config):
        return JSONResponse({"error": "channel not found"}, status_code=404)
    _stash_schedule.pop(tvg_id, None)
    await _rebuild_single_channel(tvg_id)
    return JSONResponse({"ok": True, "programs": len(_stash_schedule.get(tvg_id, []))})


async def endpoint_rebuild_schedule(request: Request):
    """Force a fresh Stash schedule rebuild — wipes existing data and regenerates."""
    global _stash_schedule, _stash_schedule_built_at
    if not getattr(config, "ENABLE_STASH_CHANNELS", False):
        return JSONResponse({"error": "Stash channels not enabled"}, status_code=400)
    _stash_schedule = {}
    _stash_schedule_built_at = 0.0
    await _rebuild_stash_schedules()
    channel_count = len(_stash_schedule)
    prog_count = sum(len(v) for v in _stash_schedule.values())
    logger.info(f"Schedule rebuild complete: {channel_count} channels, {prog_count} entries")
    return JSONResponse({"ok": True, "channels": channel_count, "programs": prog_count})


async def endpoint_guide_data(request: Request):
    """Return full-day EPG data for all channels (Tunarr + Stash).

    Query params:
        date (optional): YYYY-MM-DD in UTC; defaults to today UTC.
    """
    # Prefer a Unix timestamp sent by the browser (local midnight in the user's
    # timezone).  Fall back to a YYYY-MM-DD date string interpreted as UTC midnight,
    # then to today UTC midnight.
    ts_str   = request.query_params.get("ts", "")
    date_str = request.query_params.get("date", "")
    try:
        if ts_str:
            day_start = int(float(ts_str))
        elif date_str:
            d = _date_cls.fromisoformat(date_str)
            day_start = int(datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp())
        else:
            d = _date_cls.today()
            day_start = int(datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp())
    except Exception:
        return JSONResponse({"error": "invalid date"}, status_code=400)

    day_end = day_start + 86400
    date_label = datetime.fromtimestamp(day_start, tz=timezone.utc).strftime("%Y-%m-%d")

    tunarr_enabled = getattr(config, "ENABLE_TUNARR", False)
    stash_enabled  = getattr(config, "ENABLE_STASH_CHANNELS", False)

    if not tunarr_enabled and not stash_enabled:
        return JSONResponse({"date": date_label, "day_start": day_start, "day_end": day_end, "channels": []})

    result: list[dict] = []

    # ── Tunarr / Ersatz channels (read-only — managed externally) ──────────
    if tunarr_enabled:
        tunarr_channels = await _get_channels()
        tunarr_programs = await _get_programs()
        programs_by_channel: dict[str, list] = {}
        for prog in tunarr_programs:
            programs_by_channel.setdefault(prog["channel_id"], []).append(prog)

        for ch in tunarr_channels:
            tvg_id = ch["tvg_id"]
            programs = []
            for prog in programs_by_channel.get(tvg_id, []):
                if prog.get("stop_ts", 0) <= day_start or prog.get("start_ts", 0) >= day_end:
                    continue
                programs.append({
                    "eid": "",
                    "start_ts": prog["start_ts"],
                    "stop_ts": prog["stop_ts"],
                    "title": prog["title"],
                    "scene_id": None,
                    "genre": prog.get("genre", ""),
                    "desc": prog.get("desc", ""),
                    "year": prog.get("year"),
                    "rating": prog.get("rating", ""),
                    "icon_url": prog.get("icon", ""),
                })
            custom = _custom_logo_path(tvg_id)
            if custom:
                logo_url = f"/api/livetv/channel-logo/{tvg_id}?v={int(os.path.getmtime(custom))}"
            elif ch.get("logo"):
                logo_url = f"/api/livetv/channel-logo/{tvg_id}"
            else:
                logo_url = ""
            result.append({
                "tvg_id": tvg_id,
                "name": ch["name"],
                "number": ch.get("number", ""),
                "logo_url": logo_url,
                "programs": programs,
                "readonly": True,
            })

    # ── Dynamic Stash channels (editable) ──────────────────────────────────
    if stash_enabled:
        await _ensure_stash_schedules()
        channels = _stash_channels_cache["data"]
        if channels is None:
            channels = await _get_stash_channels()

        for ch in channels:
            tvg_id = ch["tvg_id"]
            schedule = _stash_schedule.get(tvg_id, [])
            programs = []
            for entry in schedule:
                if entry["stop_ts"] <= day_start or entry["start_ts"] >= day_end:
                    continue
                programs.append({
                    "eid": entry.get("eid", ""),
                    "start_ts": entry["start_ts"],
                    "stop_ts": entry["stop_ts"],
                    "title": entry["title"],
                    "scene_id": entry.get("scene_id"),
                    "genre": entry.get("genre", ""),
                })
            custom = _custom_logo_path(tvg_id)
            if custom:
                logo_url = f"/api/livetv/channel-logo/{tvg_id}?v={int(os.path.getmtime(custom))}"
            elif ch.get("logo"):
                logo_url = f"/api/livetv/channel-logo/{tvg_id}"
            else:
                logo_url = ""
            result.append({
                "tvg_id": tvg_id,
                "name": ch["name"],
                "number": ch.get("number", ""),
                "logo_url": logo_url,
                "programs": programs,
                "stash_type": ch.get("stash_type", ""),
            })

    return JSONResponse({"date": date_label, "day_start": day_start, "day_end": day_end, "channels": result})


async def endpoint_channel_logo_get(request: Request):
    """Serve the logo for a channel: custom file → Stash proxy → 404."""
    tvg_id = request.path_params.get("tvg_id", "")

    custom = _custom_logo_path(tvg_id)
    if custom:
        mt = mimetypes.guess_type(custom)[0] or "image/jpeg"
        return FileResponse(custom, media_type=mt, headers={"Cache-Control": "public, max-age=3600"})

    channels = _stash_channels_cache["data"] or []
    if not channels:
        channels = await _get_stash_channels()
    ch = next((c for c in channels if c["tvg_id"] == tvg_id), None)
    if ch and ch.get("logo"):
        from api.image_routes import _proxy_image
        return await _proxy_image(ch["logo"])

    tunarr_channels = _m3u_cache.get("data") or []
    tunarr_ch = next((c for c in tunarr_channels if c["tvg_id"] == tvg_id), None)
    if tunarr_ch and tunarr_ch.get("logo"):
        from api.image_routes import _proxy_image
        return await _proxy_image(tunarr_ch["logo"])

    return Response(status_code=404)


async def endpoint_channel_logo_upload(request: Request):
    """Upload a custom logo for a channel (multipart/form-data, field name: 'file')."""
    tvg_id = request.path_params.get("tvg_id", "")
    if not re.match(r'^[a-zA-Z0-9_-]{1,60}$', tvg_id):
        return JSONResponse({"error": "invalid tvg_id"}, status_code=400)

    try:
        form = await request.form()
        upload = form.get("file")
        if not upload or not getattr(upload, "filename", None):
            return JSONResponse({"error": "no file provided"}, status_code=400)

        ext = os.path.splitext(upload.filename)[1].lower()
        if ext not in (".jpg", ".jpeg", ".png", ".webp", ".gif"):
            return JSONResponse({"error": "unsupported file type"}, status_code=400)

        logo_dir = _logo_dir()
        # Remove any existing custom logo for this channel (any extension)
        for existing in glob.glob(os.path.join(logo_dir, f"{tvg_id}.*")):
            os.remove(existing)

        dest = os.path.join(logo_dir, f"{tvg_id}{ext}")
        content = await upload.read()
        with open(dest, "wb") as fh:
            fh.write(content)

        logger.info(f"LiveTV: custom logo saved for '{tvg_id}' ({len(content)} bytes) → {dest}")
        return JSONResponse({"ok": True})
    except Exception as exc:
        logger.error(f"LiveTV: logo upload failed for '{tvg_id}': {exc}")
        return JSONResponse({"error": str(exc)}, status_code=500)


async def endpoint_epg_scene_match(request: Request):
    """Search Stash for a scene matching an XMLTV title + year.

    Returns full scene metadata (same shape as endpoint_scene_detail) plus
    {"match": true} when exactly one title-exact scene is found, or
    {"match": false} when there is no confident hit.
    """
    title = request.query_params.get("title", "").strip()
    year_str = request.query_params.get("year", "").strip()

    if not title:
        return JSONResponse({"match": False})

    from core import stash_client

    scene_filter: dict = {
        "title": {"modifier": "EQUALS", "value": title}
    }
    if year_str:
        try:
            y = int(year_str)
            scene_filter["date"] = {
                "modifier": "BETWEEN",
                "value": f"{y}-01-01",
                "value2": f"{y}-12-31",
            }
        except ValueError:
            pass

    _MATCH_FIELDS = (
        "id title code date details o_counter play_count rating100 organized "
        "studio { name } performers { name } tags { name } files { duration }"
    )
    query = (
        f"query($filter: FindFilterType, $scene_filter: SceneFilterType) {{"
        f" findScenes(filter: $filter, scene_filter: $scene_filter)"
        f" {{ count scenes {{ {_MATCH_FIELDS} }} }} }}"
    )
    data = await stash_client.call_graphql(
        query, {"filter": {"per_page": 5}, "scene_filter": scene_filter}
    )
    scenes = (data or {}).get("findScenes", {}).get("scenes", [])

    if len(scenes) != 1:
        return JSONResponse({"match": False})

    scene = scenes[0]
    files = scene.get("files") or []
    duration = files[0].get("duration") if files else None

    return JSONResponse({
        "match": True,
        "id": scene.get("id"),
        "title": scene.get("title") or "",
        "code": scene.get("code") or "",
        "date": scene.get("date") or "",
        "details": scene.get("details") or "",
        "rating": scene.get("rating100"),
        "o_counter": scene.get("o_counter", 0),
        "play_count": scene.get("play_count", 0),
        "organized": scene.get("organized", False),
        "studio": (scene.get("studio") or {}).get("name", ""),
        "performers": [p["name"] for p in (scene.get("performers") or [])],
        "tags": [t["name"] for t in (scene.get("tags") or [])],
        "duration": duration,
    })


async def endpoint_scene_detail(request: Request):
    """Return scene metadata for the guide scene-detail popup (no file paths)."""
    scene_id = request.path_params.get("scene_id", "")
    if not re.match(r'^\d+$', scene_id):
        return JSONResponse({"error": "invalid scene id"}, status_code=400)

    from core import stash_client
    scene = await stash_client.get_scene(scene_id)
    if not scene:
        return JSONResponse({"error": "not found"}, status_code=404)

    duration = None
    files = scene.get("files") or []
    if files:
        duration = files[0].get("duration")

    result = {
        "id": scene.get("id"),
        "title": scene.get("title") or "",
        "code": scene.get("code") or "",
        "date": scene.get("date") or "",
        "details": scene.get("details") or "",
        "rating": scene.get("rating100"),
        "o_counter": scene.get("o_counter", 0),
        "play_count": scene.get("play_count", 0),
        "organized": scene.get("organized", False),
        "studio": (scene.get("studio") or {}).get("name", ""),
        "performers": [p["name"] for p in (scene.get("performers") or [])],
        "tags": [t["name"] for t in (scene.get("tags") or [])],
        "duration": duration,
    }
    return JSONResponse(result)


async def endpoint_scene_screenshot(request: Request):
    """Proxy the Stash screenshot for a scene so the guide modal can display it."""
    scene_id = request.path_params.get("scene_id", "")
    if not re.match(r'^\d+$', scene_id):
        return Response(status_code=400)

    stash_base = config.get_stash_base()
    apikey = getattr(config, "STASH_API_KEY", "")
    url = f"{stash_base}/scene/{scene_id}/screenshot"
    if apikey:
        url += f"?apikey={apikey}"

    try:
        r = await _live_client.get(url)
        if r.status_code != 200:
            return Response(status_code=r.status_code)
        ct = r.headers.get("content-type", "image/jpeg")
        return Response(r.content, media_type=ct, headers={"Cache-Control": "public, max-age=3600"})
    except Exception as exc:
        logger.error(f"LiveTV: scene screenshot proxy failed for {scene_id}: {exc}")
        return Response(status_code=502)


async def endpoint_channel_logo_delete(request: Request):
    """Delete the custom logo for a channel, reverting to Stash art or default."""
    tvg_id = request.path_params.get("tvg_id", "")
    if not re.match(r'^[a-zA-Z0-9_-]{1,60}$', tvg_id):
        return JSONResponse({"error": "invalid tvg_id"}, status_code=400)

    custom = _custom_logo_path(tvg_id)
    if not custom:
        return JSONResponse({"ok": True, "removed": False})

    try:
        os.remove(custom)
        logger.info(f"LiveTV: custom logo cleared for '{tvg_id}'")
        return JSONResponse({"ok": True, "removed": True})
    except Exception as exc:
        logger.error(f"LiveTV: logo delete failed for '{tvg_id}': {exc}")
        return JSONResponse({"error": str(exc)}, status_code=500)


# ---------------------------------------------------------------------------
# Schedule editing endpoints
# ---------------------------------------------------------------------------

async def endpoint_channel_scenes(request: Request):
    """GET /api/livetv/channel-scenes/{tvg_id}
    Return all scenes in the channel's configured lineup so the editor can
    present a filterable scene picker.
    """
    tvg_id = request.path_params.get("tvg_id", "")
    channels = _stash_channels_cache.get("data") or []
    ch = next((c for c in channels if c["tvg_id"] == tvg_id), None)
    if ch is None:
        channels = await _get_stash_channels()
        ch = next((c for c in channels if c["tvg_id"] == tvg_id), None)
    if ch is None:
        return JSONResponse({"error": "channel not found"}, status_code=404)

    scenes = await _fetch_scenes_for_stash_channel(ch)
    result = []
    for s in scenes:
        result.append({
            "id":            s["id"],
            "title":         s["title"],
            "duration_sec":  s["duration_sec"],
            "organized":     s.get("organized", False),
            "rating":        s.get("rating", 0),
            "o_counter":     s.get("o_counter", 0),
            "tag_count":     s.get("tag_count", 0),
            "tags":          s.get("tags", []),
            "has_description": s.get("has_description", False),
            "genre":         _scene_genre(s),
            "thumb":         _stash_screenshot_url(s["id"]),
        })
    return JSONResponse({"scenes": result})


async def endpoint_schedule_delete(request: Request):
    """DELETE /api/livetv/schedule/{tvg_id}/{eid}
    Remove one entry and shift all subsequent entries earlier by its duration.
    """
    tvg_id = request.path_params.get("tvg_id", "")
    eid    = request.path_params.get("eid", "")

    schedule = _stash_schedule.get(tvg_id)
    if not schedule:
        return JSONResponse({"error": "channel not found"}, status_code=404)

    idx = next((i for i, e in enumerate(schedule) if e.get("eid") == eid), None)
    if idx is None:
        return JSONResponse({"error": "entry not found"}, status_code=404)

    removed  = schedule.pop(idx)
    shift    = removed["duration_sec"]
    for entry in schedule[idx:]:
        entry["start_ts"] -= shift
        entry["stop_ts"]  -= shift

    _save_schedule()
    logger.info(f"LiveTV: deleted schedule entry {eid} from '{tvg_id}', shifted {len(schedule)-idx} entries by -{shift:.1f}s")
    return JSONResponse({"ok": True})


async def endpoint_schedule_reorder(request: Request):
    """POST /api/livetv/schedule/{tvg_id}/reorder
    Body: {"eids": ["eid1", "eid2", ...]} — full ordered list for the channel.
    Rebuilds timestamps from the current window start in the new order.
    """
    tvg_id = request.path_params.get("tvg_id", "")
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON"}, status_code=400)

    eids = body.get("eids", [])
    schedule = _stash_schedule.get(tvg_id)
    if not schedule:
        return JSONResponse({"error": "channel not found"}, status_code=404)

    entry_map = {e["eid"]: e for e in schedule}
    unknown = [eid for eid in eids if eid not in entry_map]
    if unknown:
        return JSONResponse({"error": f"unknown eids: {unknown}"}, status_code=400)

    window_start = schedule[0]["start_ts"]
    reordered    = [entry_map[eid] for eid in eids]
    # Any entries not in the submitted list go at the end (shouldn't happen in normal use)
    submitted    = set(eids)
    tail         = [e for e in schedule if e["eid"] not in submitted]
    new_schedule = reordered + tail

    cursor = window_start
    for entry in new_schedule:
        entry["start_ts"] = cursor
        entry["stop_ts"]  = cursor + entry["duration_sec"]
        cursor = entry["stop_ts"]

    _stash_schedule[tvg_id] = new_schedule
    _save_schedule()
    logger.info(f"LiveTV: reordered {len(new_schedule)} entries for '{tvg_id}'")
    return JSONResponse({"ok": True})


async def endpoint_schedule_insert(request: Request):
    """POST /api/livetv/schedule/{tvg_id}/insert
    Body: {"after_eid": "<eid or null>", "scene_id": "<stash scene id>"}
    Insert a scene immediately after after_eid (or at the start if null),
    then shift all subsequent entries forward by the scene's duration.
    """
    tvg_id = request.path_params.get("tvg_id", "")
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON"}, status_code=400)

    after_eid = body.get("after_eid")   # None → insert at beginning
    scene_id  = body.get("scene_id", "")

    schedule = _stash_schedule.get(tvg_id, [])
    channels = _stash_channels_cache.get("data") or []
    ch = next((c for c in channels if c["tvg_id"] == tvg_id), None)
    if ch is None:
        return JSONResponse({"error": "channel not found"}, status_code=404)

    scenes = await _fetch_scenes_for_stash_channel(ch)
    scene  = next((s for s in scenes if s["id"] == scene_id), None)
    if scene is None:
        return JSONResponse({"error": "scene not found in channel lineup"}, status_code=404)

    new_entry = {
        "eid":          _new_eid(),
        "scene_id":     scene["id"],
        "title":        scene["title"],
        "duration_sec": scene["duration_sec"],
        "genre":        _scene_genre(scene),
        "rating":       scene.get("rating", 0),
        "o_counter":    scene.get("o_counter", 0),
        "start_ts":     0,
        "stop_ts":      0,
    }

    if after_eid is None:
        insert_idx = 0
    else:
        idx = next((i for i, e in enumerate(schedule) if e.get("eid") == after_eid), None)
        if idx is None:
            return JSONResponse({"error": "after_eid not found"}, status_code=404)
        insert_idx = idx + 1

    # Anchor: the start time of whatever currently occupies insert_idx (or end of schedule)
    if schedule and insert_idx < len(schedule):
        insert_start = schedule[insert_idx]["start_ts"]
    elif schedule:
        insert_start = schedule[-1]["stop_ts"]
    else:
        insert_start = time.time()

    new_entry["start_ts"] = insert_start
    new_entry["stop_ts"]  = insert_start + new_entry["duration_sec"]
    schedule.insert(insert_idx, new_entry)

    # Shift everything after the new entry forward
    shift = new_entry["duration_sec"]
    for entry in schedule[insert_idx + 1:]:
        entry["start_ts"] += shift
        entry["stop_ts"]  += shift

    _stash_schedule[tvg_id] = schedule
    _save_schedule()
    logger.info(f"LiveTV: inserted scene {scene_id} at position {insert_idx} in '{tvg_id}'")
    return JSONResponse({"ok": True, "eid": new_entry["eid"]})
