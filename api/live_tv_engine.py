import asyncio
import logging
import os
import platform
import shutil
import socket
import tempfile
import time
from datetime import datetime, timezone
try:
    import fcntl
except ImportError:
    fcntl = None  # type: ignore

import config
from api.live_tv_data import _next_scheduled_segment_after, _upcoming_scheduled_segments
from core.vertical import vdebug

logger = logging.getLogger(__name__)

_IS_WINDOWS = platform.system().lower().startswith("win")
_HAS_MKFIFO = hasattr(os, "mkfifo")


async def _iter_stderr_lines(stream: asyncio.StreamReader):
    """Yield FFmpeg stderr lines, splitting on \\n OR \\r.

    FFmpeg's periodic progress line (``frame=... speed=...``) is terminated
    with a bare ``\\r`` when stderr is a pipe, so StreamReader.readline()
    (which only splits on ``\\n``) accumulates every progress update into one
    ever-growing "line".  After ~5–10 minutes that exceeds the reader's 64 KiB
    limit, readline() raises, the drain task dies, and once the OS stderr pipe
    fills FFmpeg blocks on its next stderr write — stalling the whole playout.
    Chunk-reading and splitting on both separators avoids both failure modes.
    """
    pending = b""
    while True:
        chunk = await stream.read(4096)
        if not chunk:
            break
        pending += chunk
        lines = pending.replace(b"\r\n", b"\n").replace(b"\r", b"\n").split(b"\n")
        pending = lines.pop()  # partial tail — completed by the next chunk
        for raw in lines:
            if raw:
                yield raw.decode(errors="replace")
    if pending:
        yield pending.decode(errors="replace")
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
        self._cleaners: dict[str, asyncio.Task] = {}  # channel_id → segment cleanup task
        self._backends: dict[str, "_PipeBackend"] = {}  # channel_id → pipe backend (FIFO on Linux, TCP on Windows)
        self._consumed_until: dict[str, float] = {}  # channel_id → wall-clock fed up to
        self._current_scene: dict[str, dict] = {}    # channel_id → live "what's being fed now"
        self._feeder_waiting: dict[str, bool] = {}   # channel_id → True while feeder sleeps for air time (reserved for future use)
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
            # Small live window so the client only sees ~24 s of look-ahead
            # and cannot fast-forward past the live edge.  Segments are NOT
            # auto-deleted by FFmpeg; our _cleanup_loop() handles eviction
            # once they are well past the live edge.
            "-hls_list_size", str(int(getattr(config, "LIVE_TV_HLS_LIST_SIZE", 6))),
            "-hls_flags",
            "append_list+omit_endlist+program_date_time+independent_segments",
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
        cleaner = asyncio.create_task(self._cleanup_loop(cid))
        self._cleaners[cid] = cleaner

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
        if ch.get("stash_type") == "vertical_tv":
            await self._feeder_vertical(cid, backend)
            return

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
                    stash_base, api_key, ffmpeg_bin, scene_seek, scene_dur,
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

    async def _feeder_vertical(self, cid: str, backend: "_PipeBackend") -> None:
        """Feeder for the Vertical TV channel (Feature 1 Phase 2).

        There's no fixed schedule to walk — instead of the next scheduled scene,
        every round picks a fresh random center + 2 side clips from the Vertical
        Multi-View library (`core.vertical_selection`, the exact algorithm the
        VOD compositor uses) and composites them via `_feed_one_vertical_round`
        until the center ends, then picks a new round.  Runs until cancelled by
        _stop_locked, exactly like the scheduled `_feeder`.
        """
        from core.vertical_selection import pick_center_and_sides

        recent_centers: list[str] = []
        rounds_played = 0
        logger.info(f"Vertical TV: feeder started for channel {cid!r}")
        try:
            while True:
                picked = await pick_center_and_sides(exclude_ids=set(recent_centers))
                if picked is None:
                    logger.warning(
                        f"Vertical TV: channel {cid!r} has no eligible vertical scenes "
                        f"for a triptych — retrying in 10s"
                    )
                    await asyncio.sleep(10)
                    continue
                center, sides = picked
                center_id = str(center.get("id"))
                recent_centers.append(center_id)
                del recent_centers[:-5]  # avoid immediate repeats without tracking full history

                files = center.get("files") or []
                duration = float(files[0].get("duration") or 0) if files else 0.0
                self._current_scene[cid] = {
                    "scene_id":     center_id,
                    "title":        center.get("title", ""),
                    "duration_sec": duration,
                    "scene_seek":   0.0,
                    "started_at":   time.time(),
                }

                t0 = time.time()
                logger.info(
                    f"Vertical TV: channel {cid!r} round #{rounds_played+1} "
                    f"center={center_id} sides={sides} dur={duration:.1f}s"
                )
                ok = await self._feed_one_vertical_round(cid, center_id, sides, backend)
                logger.info(
                    f"Vertical TV: channel {cid!r} round center={center_id} "
                    f"finished ok={ok} elapsed={time.time()-t0:.1f}s"
                )
                rounds_played += 1
        except asyncio.CancelledError:
            logger.info(f"Vertical TV: feeder cancelled for {cid!r} after {rounds_played} round(s)")
            raise
        except Exception:
            logger.error(f"Vertical TV: feeder crashed for {cid!r}", exc_info=True)

    async def _feed_one_vertical_round(self, cid: str, center_id: str, sides: list,
                                        backend: "_PipeBackend") -> bool:
        """Spawn ONE composite round: a 3-input hstack video sub (2 looping sides
        + center) and a center-only audio sub — the same pair
        `api.vertical_engine._VerticalSessionManager` spawns per VOD play, reused
        here via its command builders so the compositor's filtergraph lives in
        one place (see docs/Triptych.md § Vertical TV).  Ends (both subs exit)
        when the center clip ends (`-shortest` in the composite filtergraph).
        """
        from api.vertical_engine import build_composite_cmd, build_audio_cmd

        ffmpeg_bin = getattr(config, "FFMPEG_PATH", "ffmpeg")
        sub_out_v, sub_out_a = backend.sub_outputs()
        left_id, right_id = sides[0], sides[1]
        fh = self._stderr_fh.get(cid)

        def _log_cmd(label: str, cmd: list) -> None:
            # Full command goes to the app log (VERTICAL_DEBUG promotes it to INFO)
            # and to the per-channel FFmpeg session log next to the stderr it produces.
            vdebug(logger, f"Vertical TV: {label} cmd for {cid!r}: {' '.join(cmd)}")
            if fh is not None:
                try:
                    fh.write(f"[round center={center_id}] {label} cmd: {' '.join(cmd)}\n")
                    fh.flush()
                except Exception:
                    pass

        composite_cmd = build_composite_cmd(ffmpeg_bin, left_id, center_id, right_id, 0.0, sub_out_v)
        _log_cmd("composite", composite_cmd)
        try:
            sub_v = await asyncio.create_subprocess_exec(
                *composite_cmd,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
        except Exception as exc:
            logger.error(f"Vertical TV: could not spawn composite sub for {cid!r} center={center_id}: {exc}")
            return False
        logger.info(f"Vertical TV: channel {cid!r} center={center_id} composite pid={sub_v.pid}")
        asyncio.create_task(self._drain_sub_stderr(sub_v, cid, f"{center_id}/composite"))

        # Master can only attach to the audio endpoint after find_stream_info() on
        # the video input completes — which needs the composite sub already writing
        # (identical constraint to the scheduled feeder's _feed_one_scene).
        a_ready = await backend.wait_master_audio_attached(timeout=15.0)
        if not a_ready:
            logger.error(
                f"Vertical TV: master never attached to audio endpoint for {cid!r} "
                f"center={center_id} — killing composite sub and skipping round"
            )
            try: sub_v.terminate()
            except Exception: pass
            await sub_v.wait()
            return False
        vdebug(logger, f"Vertical TV: channel {cid!r} master attached to audio endpoint")

        audio_cmd = build_audio_cmd(ffmpeg_bin, center_id, 0.0, sub_out_a)
        _log_cmd("audio", audio_cmd)
        try:
            sub_a = await asyncio.create_subprocess_exec(
                *audio_cmd,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
        except Exception as exc:
            logger.error(f"Vertical TV: could not spawn audio sub for {cid!r} center={center_id}: {exc}")
            try: sub_v.terminate()
            except Exception: pass
            await sub_v.wait()
            return False
        logger.info(f"Vertical TV: channel {cid!r} center={center_id} audio pid={sub_a.pid}")
        asyncio.create_task(self._drain_sub_stderr(sub_a, cid, f"{center_id}/audio"))

        # Same fast-fail safety net as _feed_one_scene: a center with no audio
        # stream exits sub_a almost immediately, which would otherwise starve the
        # master's audio input indefinitely (it has no EOF to react to — the
        # parent holds a keepalive writer FD open) and stall the whole channel,
        # not just one VOD play.  Replace with lavfi silence so the round still
        # completes in roughly the center's duration.
        try:
            rc_a_fast = await asyncio.wait_for(asyncio.shield(sub_a.wait()), timeout=2.0)
            if rc_a_fast != 0:
                # Duration unknown here without re-fetching the scene; fall back
                # to a generous fixed silence window — the composite's own
                # `-shortest` (bounded by the center's video track) is what
                # actually ends the round, this just keeps the audio pipe fed
                # until then.
                silence_cmd = [
                    ffmpeg_bin, "-y", "-hide_banner", "-loglevel", "warning",
                    "-f", "lavfi", "-i", "aevalsrc=0:c=stereo:s=48000",
                    "-t", "7200",
                    "-ar", "48000", "-ac", "2", "-c:a", "pcm_s16le",
                    "-f", "s16le", sub_out_a,
                ]
                sub_a = await asyncio.create_subprocess_exec(
                    *silence_cmd,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                logger.info(
                    f"Vertical TV: channel {cid!r} center={center_id} has no audio "
                    f"— spawned silence filler pid={sub_a.pid}"
                )
        except asyncio.TimeoutError:
            pass  # sub_a still running after 2 s — has audio, proceed normally

        rc_v, rc_a = await asyncio.gather(sub_v.wait(), sub_a.wait())
        if rc_v != 0:
            logger.warning(f"Vertical TV: composite sub for {cid!r} center={center_id} exited rc={rc_v}")
        if rc_a != 0:
            logger.debug(f"Vertical TV: audio sub for {cid!r} center={center_id} exited rc={rc_a}")
        return rc_v == 0

    async def _feed_one_scene(self, cid: str, scene_id: str,
                               backend: "_PipeBackend",
                               stash_base: str, api_key: str,
                               ffmpeg_bin: str, scene_seek: float,
                               scene_dur: float = 0.0) -> bool:
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
            # Read the Stash source at its native playback speed (1×).  This
            # makes segment generation hardware-independent and prevents the
            # client from fast-forwarding: the master can only produce HLS
            # segments as fast as the sub delivers frames.
            "-re",
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

        # Detect fast failure of audio sub (scene has no audio stream).
        # When sub_a fails before connecting to the relay, master's audio
        # input waits indefinitely, stalling HLS output long enough for
        # ExoPlayer to throw PlaylistStuckException.  If sub_a exits within
        # 2 s with a non-zero rc, replace it with a lavfi silence filler so
        # the relay audio port receives a connection and master can continue.
        try:
            rc_a_fast = await asyncio.wait_for(asyncio.shield(sub_a.wait()), timeout=2.0)
            if rc_a_fast != 0:
                silence_dur = max(1.0, scene_dur - max(0.0, scene_seek))
                silence_cmd = [
                    ffmpeg_bin, "-y", "-hide_banner", "-loglevel", "warning",
                    "-f", "lavfi", "-i", "aevalsrc=0:c=stereo:s=48000",
                    "-t", f"{silence_dur:.3f}",
                    "-ar", "48000", "-ac", "2", "-c:a", "pcm_s16le",
                    "-f", "s16le", sub_out_a,
                ]
                sub_a = await asyncio.create_subprocess_exec(
                    *silence_cmd,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                logger.info(
                    f"LiveTV feeder: scene {scene_id} has no audio — "
                    f"spawned {silence_dur:.1f}s silence filler pid={sub_a.pid}"
                )
        except asyncio.TimeoutError:
            pass  # sub_a still running after 2 s — has audio, proceed normally

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
            async for text in _iter_stderr_lines(sub.stderr):
                tagged = f"[scene {scene_id}] {text.rstrip()}"
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
            async for line in _iter_stderr_lines(proc.stderr):
                text = line.rstrip()
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

        cleaner = self._cleaners.pop(cid, None)
        if cleaner and not cleaner.done():
            cleaner.cancel()
            try:
                await asyncio.wait_for(cleaner, timeout=2.0)
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
        self._feeder_waiting.pop(cid, None)

    async def _cleanup_loop(self, cid: str) -> None:
        """Delete segment files that have aged past the retention window.

        Because we removed the ``delete_segments`` HLS flag, FFmpeg writes
        segments to disk but never deletes them.  We evict them here once they
        are older than RETENTION_SEGMENTS segments behind the playlist's current
        minimum media-sequence number.  This keeps disk usage bounded while
        giving clients time to fetch segments that have scrolled off the live
        window but haven't been downloaded yet.
        """
        retention = int(getattr(config, "LIVE_TV_SEG_RETENTION", 30))  # extra segs to keep
        seg_dir = self._dirs.get(cid)

        try:
            while True:
                await asyncio.sleep(15)
                seg_dir = self._dirs.get(cid)
                if not seg_dir:
                    break
                manifest = os.path.join(seg_dir, "stream.m3u8")
                if not os.path.exists(manifest):
                    continue
                try:
                    with open(manifest, "r", encoding="utf-8") as fh:
                        content = fh.read()
                except OSError:
                    continue

                min_seq = None
                for line in content.splitlines():
                    if line.startswith("#EXT-X-MEDIA-SEQUENCE:"):
                        try:
                            min_seq = int(line.split(":", 1)[1].strip())
                        except (ValueError, IndexError):
                            pass
                        break

                if min_seq is None:
                    continue

                cutoff = min_seq - retention
                if cutoff <= 0:
                    continue

                try:
                    entries = os.listdir(seg_dir)
                except OSError:
                    continue

                for fname in entries:
                    if not (fname.startswith("seg") and fname.endswith(".ts")):
                        continue
                    try:
                        seq = int(fname[3:-3])
                    except ValueError:
                        continue
                    if seq < cutoff:
                        try:
                            os.remove(os.path.join(seg_dir, fname))
                            logger.debug(
                                f"LiveTV cleanup: {cid!r} deleted {fname} "
                                f"(seq {seq} < cutoff {cutoff})"
                            )
                        except OSError:
                            pass
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.warning(f"LiveTV cleanup: loop crashed for {cid!r}: {exc}")

    async def _idle_loop(self) -> None:
        idle_secs = float(getattr(config, "LIVE_TV_IDLE_TIMEOUT", 300))
        while self._procs:
            await asyncio.sleep(20)
            now  = time.time()
            idle = [
                cid for cid, ts in list(self._last.items())
                if now - ts > idle_secs
                # Don't evict a channel whose feeder is sleeping for air-time
                # — it looks "idle" because the client isn't requesting yet,
                # but the channel is healthy and about to start encoding.
                and not self._feeder_waiting.get(cid, False)
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


