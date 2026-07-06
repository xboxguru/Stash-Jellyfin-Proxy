"""VOD compositor for Vertical Multi-View ("Triptych") — Feature 1, Phase 1b.

Reuses the Live TV playout spine (api/live_tv_engine.py): a long-lived master
FFmpeg process encoding a uniform 1920×1080 / yuv420p / 30 fps + s16le 48 kHz
stereo raw stream fed over a pipe backend (FIFO on Linux, TCP relay on Windows)
into an HLS ladder.  The critical spine invariant is preserved verbatim — the
video and audio feeds are **separate sub-processes** so the ~500× bandwidth gap
between raw video and raw PCM can't deadlock a single sub on backpressure (see
live_tv_engine.py `_feed_one_scene` for the full write-up).

Shape A (Phase 1): ONE composite sub-FFmpeg takes 3 HTTP inputs (2 looping side
clips + 1 center clip) from Stash `/scene/{id}/stream`, `hstack`s them into the
1920×1080 master video pipe; a SECOND sub feeds the master audio pipe from the
center clip only.  The composite ends when the center ends (`-shortest` against
`-stream_loop -1` sides).  Inputs are read with `-re` so the client can never
outrun the encoder.

Sessions are keyed by a **play-session id** (`{scene_id}-{nonce}`): fresh side
clips per play, stable across seeks within that play.  A center seek is a full
session relaunch with `-ss` on the center input only (the sides just keep
looping) — Phase 1's simple, correct approach (§1.5).
"""
import asyncio
import logging
import os
import shutil
import tempfile
import time
from datetime import datetime, timezone

import config
from core.hw_encoder import EncoderConfig, resolve_h264_encoder
from core.vertical_selection import select_side_clips
# Reuse the Live TV pipe backends + platform probes verbatim — the byte-forwarding
# plumbing is identical; only the FFmpeg graph feeding it differs.
from api.live_tv_engine import (
    _PipeBackend, _FifoPipeBackend, _TcpRelayPipeBackend,
    _IS_WINDOWS, _HAS_MKFIFO,
)

logger = logging.getLogger(__name__)


def _stash_stream_url(scene_id: str) -> str:
    stash_base = config.get_stash_base()
    api_key = getattr(config, "STASH_API_KEY", "")
    url = f"{stash_base}/scene/{scene_id}/stream"
    if api_key:
        url += f"?apikey={api_key}"
    return url


def build_composite_cmd(ffmpeg_bin: str, left_id: str, center_id: str,
                         right_id: str, seek: float, sub_out_v: str) -> list:
    """Shape A composite: 3 HTTP inputs → per-lane scale/crop → hstack → pad → raw video.

    Sides loop forever (`-stream_loop -1`); the center is the clock — `-shortest`
    ends the composite when the center ends. `-ss` (input seek) applies only to
    the center so a seek relaunch keeps the sides looping from their own start.
    Lane geometry per §1.5: 1080-high scale, crop to 608×1080, hstack→1824×1080,
    pad to exactly 1920×1080.

    Module-level so both the VOD compositor (`_VerticalSessionManager`) and the
    always-on Vertical TV Live TV channel (`api/live_tv_engine.py`) share the one
    filtergraph definition — see docs/Triptych.md § Vertical TV.
    """
    left_url = _stash_stream_url(left_id)
    center_url = _stash_stream_url(center_id)
    right_url = _stash_stream_url(right_id)

    common_pre = [
        ffmpeg_bin, "-y", "-hide_banner", "-loglevel", "info", "-stats",
        "-reconnect", "1", "-reconnect_streamed", "1",
        "-reconnect_at_eof", "1", "-reconnect_delay_max", "5",
    ]
    center_seek = ["-ss", f"{seek:.3f}"] if seek > 0.1 else []

    return common_pre + [
        # input 0 — left side (loops)
        "-re", "-stream_loop", "-1", "-i", left_url,
        # input 1 — center (the clock); seek applies here only
        "-re", *center_seek, "-i", center_url,
        # input 2 — right side (loops)
        "-re", "-stream_loop", "-1", "-i", right_url,
        "-filter_complex",
        "[0:v]scale=-2:1080,crop=608:1080,setsar=1[l];"
        "[1:v]scale=-2:1080,crop=608:1080,setsar=1[c];"
        "[2:v]scale=-2:1080,crop=608:1080,setsar=1[r];"
        "[l][c][r]hstack=inputs=3,pad=1920:1080:(ow-iw)/2:0:black,fps=30,format=yuv420p[v]",
        "-map", "[v]", "-shortest",
        "-pix_fmt", "yuv420p", "-f", "rawvideo", sub_out_v,
    ]


def build_audio_cmd(ffmpeg_bin: str, center_id: str, seek: float, sub_out_a: str) -> list:
    """Center-only audio, normalized to 48 kHz stereo PCM (reuses the Live TV
    normalization). `-map 0:a:0?` keeps a silent center from failing the sub.

    Module-level for the same reason as `build_composite_cmd` above.
    """
    center_url = _stash_stream_url(center_id)
    common_pre = [
        ffmpeg_bin, "-y", "-hide_banner", "-loglevel", "info", "-stats",
        "-reconnect", "1", "-reconnect_streamed", "1",
        "-reconnect_at_eof", "1", "-reconnect_delay_max", "5",
    ]
    center_seek = ["-ss", f"{seek:.3f}"] if seek > 0.1 else []
    return common_pre + [
        "-re", *center_seek, "-i", center_url,
        "-map", "0:a:0?", "-vn", "-sn",
        "-af",
        "aresample=async=1000:first_pts=0,"
        "aformat=sample_rates=48000:channel_layouts=stereo",
        "-ar", "48000", "-ac", "2", "-c:a", "pcm_s16le",
        "-f", "s16le", sub_out_a,
    ]


class _VerticalSessionManager:
    """One master FFmpeg HLS process per active triptych play-session.

    Modeled on live_tv_engine._FFmpegChannelManager but keyed by play-session id
    instead of channel id, and without the schedule feeder: a triptych session
    runs exactly two sub-FFmpegs (composite video + center audio) for the whole
    play, rather than looping a schedule.  An idle watchdog tears a session down
    after VERTICAL_IDLE_TIMEOUT seconds of no manifest/segment requests.
    """

    def __init__(self):
        self._procs:     dict[str, asyncio.subprocess.Process] = {}   # session → master proc
        self._dirs:      dict[str, str]   = {}                        # session → temp dir
        self._last:      dict[str, float] = {}                        # session → last request ts
        self._stderr:    dict[str, list[str]] = {}                    # session → rolling stderr (last 60)
        self._stderr_fh: dict[str, object] = {}                       # session → per-session log file handle
        self._launch_info: dict[str, dict] = {}                       # session → {seek, sides, center, pid, ...}
        self._backends:  dict[str, _PipeBackend] = {}                 # session → pipe backend
        self._subs:      dict[str, list] = {}                         # session → [composite_sub, audio_sub]
        self._monitors:  dict[str, asyncio.Task] = {}                 # session → sub-monitor task
        self._sides:     dict[str, list] = {}                         # session → [left_id, right_id] (stable across seeks)
        self._lock       = asyncio.Lock()
        self._watchdog: asyncio.Task | None = None

    # ── log file management (mirrors live_tv_engine) ───────────────────────────

    @staticmethod
    def _ffmpeg_log_path(sid: str) -> str:
        log_dir = getattr(config, "LOG_DIR", "/config")
        d = os.path.join(log_dir, "vertical_ffmpeg")
        os.makedirs(d, exist_ok=True)
        return os.path.join(d, f"{sid}.log")

    def _open_stderr_file(self, sid: str):
        """Open the per-session FFmpeg log in append mode, rotating past 10 MB."""
        try:
            path = self._ffmpeg_log_path(sid)
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
            logger.warning(f"Vertical FFmpeg: could not open log file for {sid!r}: {exc}")
            return None

    # ── public API ─────────────────────────────────────────────────────────────

    def touch(self, sid: str) -> None:
        """Record activity; (re)start the idle watchdog if needed."""
        self._last[sid] = time.time()
        if self._watchdog is None or self._watchdog.done():
            self._watchdog = asyncio.create_task(self._idle_loop())

    def manifest_path(self, sid: str) -> str | None:
        d = self._dirs.get(sid)
        if not d:
            return None
        p = os.path.join(d, "stream.m3u8")
        return p if os.path.exists(p) else None

    def seg_dir(self, sid: str) -> str | None:
        return self._dirs.get(sid)

    def is_alive(self, sid: str) -> bool:
        p = self._procs.get(sid)
        return p is not None and p.returncode is None

    def active_count(self) -> int:
        """Number of sessions with a live master process (for the concurrency cap)."""
        return sum(1 for p in self._procs.values() if p.returncode is None)

    async def ensure(self, sid: str, center_scene: dict, seek: float = 0.0) -> bool:
        """Start (or seek-relaunch) the composite session; True once it's serving.

        - Not running        → launch (selecting side clips on first launch).
        - Running, same seek  → no-op (steady-state manifest polling).
        - Running, new seek   → full relaunch with the new center `-ss`, keeping
          the same side clips (§1.5 center-seek rule).

        Returns False (caller falls back to single-video playback) when the
        concurrency cap is hit, side selection finds no other vertical scenes,
        or FFmpeg fails to start.
        """
        async with self._lock:
            if self.is_alive(sid) and self.manifest_path(sid):
                cur_seek = float(self._launch_info.get(sid, {}).get("seek", 0.0))
                if abs(cur_seek - seek) < 0.5:
                    return True
                logger.info(
                    f"Vertical: session {sid!r} center seek {cur_seek:.1f}s → {seek:.1f}s "
                    f"— relaunching composite (sides unchanged)"
                )
                sides = self._sides.get(sid)
                await self._stop_locked(sid, keep_sides=True)
                return await self._launch(sid, center_scene, seek, sides=sides)
            await self._stop_locked(sid)
            return await self._launch(sid, center_scene, seek)

    async def seek(self, sid: str, center_scene: dict, position: float) -> bool:
        """Explicit center seek — thin wrapper over ensure()'s relaunch path."""
        return await self.ensure(sid, center_scene, max(0.0, position))

    async def stop(self, sid: str) -> None:
        async with self._lock:
            await self._stop_locked(sid)

    async def cleanup_all(self) -> None:
        for sid in list(self._procs.keys()):
            await self.stop(sid)

    # ── internals ──────────────────────────────────────────────────────────────

    async def _resolve_encoder(self) -> EncoderConfig:
        """Resolve VERTICAL_HWACCEL to a probed-working encoder (Phase 1.5).

        Delegates to the shared ``core.hw_encoder`` probe: ``none`` → libx264,
        ``auto`` → first of NVENC/QSV/VAAPI that initializes, an explicit encoder
        → that one or a logged CPU fallback.  The probe test-encodes a frame, which
        blocks, so it's run off the event loop; the result is cached after the
        first call (usually the startup probe) so per-session launches are cheap.
        """
        mode = str(getattr(config, "VERTICAL_HWACCEL", "auto")).lower()
        ffmpeg_bin = getattr(config, "FFMPEG_PATH", "ffmpeg")
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, resolve_h264_encoder, mode, ffmpeg_bin)

    async def _launch(self, sid: str, center_scene: dict, seek: float,
                       sides: list | None = None) -> bool:
        """Set up the pipe-based composite pipeline for one session.

            composite sub (3 HTTP inputs, hstack) ── v pipe ─▶┐
                                                              ├─▶ master FFmpeg ─▶ HLS
            center-audio sub  ──────────────────── a pipe ─▶┘

        The parent holds writer FDs on both pipes for the session's lifetime so
        the master never sees EOF if a sub exits (e.g. center ends).  Ordering on
        the TCP backend matters: master must claim the video endpoint before the
        composite sub connects, and can only attach to the audio endpoint after
        its find_stream_info() on the video input completes — which needs the
        composite sub already writing (identical constraint to Live TV).
        """
        if not _HAS_MKFIFO and not _IS_WINDOWS:
            logger.error(
                "Vertical FFmpeg: pipe-based playout requires os.mkfifo (Linux/macOS) "
                "or the Windows TCP-relay fallback — session cannot start here."
            )
            return False

        # Concurrency cap — count OTHER live sessions; over cap → single-video fallback.
        max_sessions = int(getattr(config, "VERTICAL_MAX_SESSIONS", 2))
        active_others = [s for s in self._procs if s != sid and self.is_alive(s)]
        if len(active_others) >= max_sessions:
            logger.warning(
                f"Vertical: concurrency cap {max_sessions} reached "
                f"({len(active_others)} active) — refusing {sid!r}; client falls back to single video"
            )
            return False

        center_id = str(center_scene.get("id"))

        # Side-clip selection — once per session; reused verbatim on seek-relaunch
        # so the sides stay stable across a play (§1.1 "stable across seeks").
        if sides is None:
            sides = self._sides.get(sid)
        if sides is None:
            sides = await select_side_clips(center_scene)
            if not sides:
                logger.warning(
                    f"Vertical: no side clips available for center {center_id} "
                    f"— single-video fallback (session {sid!r} not started)"
                )
                return False
            logger.info(f"Vertical: session {sid!r} center={center_id} sides={sides}")
        self._sides[sid] = sides
        left_id, right_id = sides[0], sides[1]

        hls_base = getattr(config, "HLS_TEMP_DIR", None) or None
        d = tempfile.mkdtemp(prefix=f"sjv_{sid[:8]}_", dir=hls_base)
        self._dirs[sid] = d

        backend: _PipeBackend = _FifoPipeBackend(d) if _HAS_MKFIFO else _TcpRelayPipeBackend(d)
        try:
            await backend.start()
        except Exception as exc:
            logger.error(f"Vertical FFmpeg: pipe backend setup failed ({backend.kind}) — {exc}")
            shutil.rmtree(d, ignore_errors=True)
            self._dirs.pop(sid, None)
            return False
        self._backends[sid] = backend
        master_in_v, master_in_a = backend.master_inputs()
        sub_out_v, sub_out_a = backend.sub_outputs()

        enc = await self._resolve_encoder()
        ffmpeg_bin = getattr(config, "FFMPEG_PATH", "ffmpeg")
        seg_tmpl = os.path.join(d, "seg%05d.ts")
        manifest = os.path.join(d, "stream.m3u8")

        # Master: raw video + raw audio pipes → single H.264/AAC HLS ladder.
        # Same two-input, probe-suppressed design as Live TV (see the long comment
        # there re: why -probesize 32 / -analyzeduration 0 avoids a pipe deadlock).
        # VOD framing: hls_playlist_type=event keeps the full segment list and
        # writes EXT-X-ENDLIST when the center ends, so the client gets a proper
        # seekable VOD manifest rather than a rolling live window.
        # Hardware encoders (VAAPI/QSV) need device-init before the inputs and an
        # hwupload -vf on the raw frames; NVENC/libx264 take system frames as-is.
        # `enc` supplies each group so decode + hstack stay on CPU (this phase).
        master_cmd = [
            ffmpeg_bin, "-y", "-hide_banner",
            *enc.input_args,
            "-f", "rawvideo", "-pix_fmt", "yuv420p", "-s", "1920x1080", "-r", "30",
            "-probesize", "32", "-analyzeduration", "0", "-thread_queue_size", "1024",
            "-i", master_in_v,
            "-f", "s16le", "-ar", "48000", "-ac", "2",
            "-probesize", "32", "-analyzeduration", "0", "-thread_queue_size", "1024",
            "-i", master_in_a,
            "-map", "0:v:0", "-map", "1:a:0",
            *(["-vf", enc.vfilter] if enc.vfilter else []),
            *enc.output_args,
            "-force_key_frames", "expr:gte(t,n_forced*4)",
            "-c:a", "aac", "-b:a", "192k",
            "-hls_time", "4",
            "-hls_playlist_type", "event",
            "-hls_flags", "independent_segments",
            "-start_number", str(max(0, int(seek) // 4)),
            "-hls_segment_filename", seg_tmpl,
            manifest,
        ]

        logger.info(
            f"Vertical FFmpeg: launching session {sid!r} — center={center_id} "
            f"sides={sides} backend={backend.kind} encoder={enc.codec} seek={seek:.1f}s"
        )
        logger.debug(f"Vertical FFmpeg master cmd: {' '.join(master_cmd)}")

        fh = self._open_stderr_file(sid)
        self._stderr_fh[sid] = fh
        if fh is not None:
            try:
                ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                fh.write(
                    f"\n===== FFmpeg session start {ts} | session={sid} center={center_id} "
                    f"sides={sides} backend={backend.kind} seek={seek:.1f}s =====\n"
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
            logger.error(f"Vertical FFmpeg: master launch failed — {exc}")
            await self._teardown_partial(sid)
            return False

        self._procs[sid] = proc
        self._stderr[sid] = []
        self._launch_info[sid] = {
            "seek": seek, "sides": sides, "center": center_id,
            "pid": proc.pid, "start_ts": time.time(), "encoder": enc.codec,
        }
        logger.info(f"Vertical FFmpeg: session {sid!r} master pid={proc.pid}")
        asyncio.create_task(self._drain_stderr(proc, sid))

        # Master must attach to the video endpoint before the composite sub connects.
        v_ready = await backend.wait_master_video_attached(timeout=10.0)
        if not v_ready:
            logger.error(f"Vertical FFmpeg: master never attached to video endpoint for {sid!r} — aborting")
            await self._stop_locked(sid)
            return False

        # ── Composite (video) sub — 3 inputs, hstack, raw video out ──
        composite_cmd = self._build_composite_cmd(
            ffmpeg_bin, left_id, center_id, right_id, seek, sub_out_v
        )
        logger.debug(f"Vertical FFmpeg composite sub cmd: {' '.join(composite_cmd)}")
        try:
            sub_v = await asyncio.create_subprocess_exec(
                *composite_cmd,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
        except Exception as exc:
            logger.error(f"Vertical FFmpeg: composite sub spawn failed for {sid!r}: {exc}")
            await self._stop_locked(sid)
            return False
        logger.info(f"Vertical FFmpeg: session {sid!r} composite pid={sub_v.pid}")
        asyncio.create_task(self._drain_sub_stderr(sub_v, sid, "composite"))

        # Master can only attach to the audio endpoint after find_stream_info() on
        # the video input completes — which needs the composite sub already writing.
        a_ready = await backend.wait_master_audio_attached(timeout=15.0)
        if not a_ready:
            logger.error(f"Vertical FFmpeg: master never attached to audio endpoint for {sid!r} — aborting")
            try: sub_v.terminate()
            except Exception: pass
            await self._stop_locked(sid)
            return False

        # ── Center-audio sub — center clip only, normalized PCM out ──
        audio_cmd = self._build_audio_cmd(ffmpeg_bin, center_id, seek, sub_out_a)
        logger.debug(f"Vertical FFmpeg audio sub cmd: {' '.join(audio_cmd)}")
        try:
            sub_a = await asyncio.create_subprocess_exec(
                *audio_cmd,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
        except Exception as exc:
            logger.error(f"Vertical FFmpeg: audio sub spawn failed for {sid!r}: {exc}")
            try: sub_v.terminate()
            except Exception: pass
            await self._stop_locked(sid)
            return False
        logger.info(f"Vertical FFmpeg: session {sid!r} audio pid={sub_a.pid}")
        asyncio.create_task(self._drain_sub_stderr(sub_a, sid, "audio"))

        self._subs[sid] = [sub_v, sub_a]
        self._monitors[sid] = asyncio.create_task(self._monitor_subs(sid, sub_v, sub_a))

        # Readiness gate — wait for ≥3 segments before declaring the session live,
        # so the client's first manifest fetch isn't empty.
        return await self._await_ready(sid, proc, manifest, min_segments=3)

    def _build_composite_cmd(self, ffmpeg_bin: str, left_id: str, center_id: str,
                             right_id: str, seek: float, sub_out_v: str) -> list:
        """Thin wrapper — see module-level `build_composite_cmd` (also reused by
        the Vertical TV Live TV channel in api/live_tv_engine.py)."""
        return build_composite_cmd(ffmpeg_bin, left_id, center_id, right_id, seek, sub_out_v)

    def _build_audio_cmd(self, ffmpeg_bin: str, center_id: str, seek: float,
                         sub_out_a: str) -> list:
        """Thin wrapper — see module-level `build_audio_cmd`."""
        return build_audio_cmd(ffmpeg_bin, center_id, seek, sub_out_a)

    async def _await_ready(self, sid: str, proc: asyncio.subprocess.Process,
                           manifest: str, min_segments: int) -> bool:
        for _ in range(60):
            if proc.returncode is not None:
                logger.error(f"Vertical FFmpeg: session {sid!r} master exited prematurely (rc={proc.returncode})")
                buf = self._stderr.get(sid, [])
                errs = [l for l in buf if not l.startswith(("ffmpeg version", "  built", "  config", "  lib"))]
                if errs:
                    logger.error("Vertical FFmpeg stderr (errors):\n" + "\n".join(errs[-30:]))
                return False
            if os.path.exists(manifest):
                try:
                    with open(manifest, "r", encoding="utf-8") as mh:
                        seg_count = sum(1 for ln in mh if ln.strip() and not ln.startswith("#"))
                except OSError:
                    seg_count = 0
                if seg_count >= min_segments:
                    logger.info(f"Vertical FFmpeg: session {sid!r} ready ({seg_count} segments)")
                    return True
            await asyncio.sleep(0.5)
        logger.error(f"Vertical FFmpeg: session {sid!r} timed out waiting for segments")
        return False

    async def _monitor_subs(self, sid: str, sub_v: asyncio.subprocess.Process,
                            sub_a: asyncio.subprocess.Process) -> None:
        """Log sub exit.  We deliberately DON'T tear the master down here — when the
        center ends the composite sub exits, the parent's keepalive FD holds the
        pipe open, and hls_playlist_type=event finalizes the manifest with ENDLIST.
        The idle watchdog reaps the session once the client stops fetching.
        """
        try:
            rc_v, rc_a = await asyncio.gather(sub_v.wait(), sub_a.wait())
            logger.info(f"Vertical FFmpeg: session {sid!r} subs finished (composite rc={rc_v}, audio rc={rc_a})")
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.debug(f"Vertical FFmpeg: monitor for {sid!r} ended", exc_info=True)

    async def _drain_sub_stderr(self, sub: asyncio.subprocess.Process, sid: str, label: str) -> None:
        buf = self._stderr.get(sid)
        fh = self._stderr_fh.get(sid)
        try:
            while True:
                line = await sub.stderr.readline()
                if not line:
                    break
                text = f"[{label}] {line.decode(errors='replace').rstrip()}"
                if buf is not None:
                    buf.append(text)
                    if len(buf) > 60:
                        buf.pop(0)
                if fh is not None:
                    try:
                        fh.write(text + "\n"); fh.flush()
                    except Exception:
                        pass
        except Exception:
            pass

    async def _drain_stderr(self, proc: asyncio.subprocess.Process, sid: str) -> None:
        buf = self._stderr.setdefault(sid, [])
        fh = self._stderr_fh.get(sid)
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
                        fh.write(text + "\n"); fh.flush()
                    except Exception:
                        pass
        except Exception:
            pass

    async def _teardown_partial(self, sid: str) -> None:
        """Tear down a half-built session (backend + dir + log) after an early failure."""
        backend = self._backends.pop(sid, None)
        if backend is not None:
            try: await backend.close()
            except Exception: pass
        fh = self._stderr_fh.pop(sid, None)
        if fh is not None:
            try: fh.close()
            except Exception: pass
        d = self._dirs.pop(sid, None)
        if d:
            shutil.rmtree(d, ignore_errors=True)

    async def _stop_locked(self, sid: str, keep_sides: bool = False) -> None:
        # Cancel the sub monitor first so it doesn't race the teardown.
        monitor = self._monitors.pop(sid, None)
        if monitor and not monitor.done():
            monitor.cancel()
            try:
                await asyncio.wait_for(monitor, timeout=2.0)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass

        # Terminate the sub-FFmpegs (composite + audio).
        for sub in self._subs.pop(sid, []):
            if sub and sub.returncode is None:
                try: sub.terminate()
                except Exception: pass
                try:
                    await asyncio.wait_for(sub.wait(), timeout=3.0)
                except asyncio.TimeoutError:
                    try: sub.kill()
                    except Exception: pass

        # Close the pipe backend (signals EOF to the master).
        backend = self._backends.pop(sid, None)
        if backend is not None:
            try: await backend.close()
            except Exception: pass

        proc = self._procs.pop(sid, None)
        if proc and proc.returncode is None:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                proc.kill()

        fh = self._stderr_fh.pop(sid, None)
        if fh is not None:
            try:
                fh.write("===== FFmpeg session end =====\n")
                fh.close()
            except Exception:
                pass

        d = self._dirs.pop(sid, None)
        if d:
            shutil.rmtree(d, ignore_errors=True)
        self._stderr.pop(sid, None)
        self._launch_info.pop(sid, None)
        if not keep_sides:
            self._sides.pop(sid, None)

    async def _idle_loop(self) -> None:
        idle_secs = float(getattr(config, "VERTICAL_IDLE_TIMEOUT", 60))
        while self._procs:
            await asyncio.sleep(20)
            now = time.time()
            idle = [sid for sid, ts in list(self._last.items())
                    if sid in self._procs and now - ts > idle_secs]
            for sid in idle:
                logger.info(f"Vertical FFmpeg: session {sid!r} idle {idle_secs:.0f}s — shutting down")
                await self.stop(sid)
                self._last.pop(sid, None)


_vertical_manager = _VerticalSessionManager()
