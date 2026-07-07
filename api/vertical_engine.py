"""VOD compositor for Vertical Multi-View ("Triptych").

Reuses the Live TV playout spine (api/live_tv_engine.py): a long-lived master
FFmpeg process encoding a uniform 1920×1080 / yuv420p / 30 fps + s16le 48 kHz
stereo raw stream fed over a pipe backend (FIFO on Linux, TCP relay on Windows)
into an HLS ladder.  The critical spine invariant is preserved verbatim — the
video and audio feeds are **separate sub-processes** so the ~500× bandwidth gap
between raw video and raw PCM can't deadlock a single sub on backpressure (see
live_tv_engine.py `_feed_one_scene` for the full write-up).

Shape A: ONE composite sub-FFmpeg takes 3 HTTP inputs (2 looping side clips + 1
center clip) from Stash `/scene/{id}/stream`, `hstack`s them into the 1920×1080
master video pipe; a SECOND sub feeds the master audio pipe from the center clip
only.  The composite ends when the center ends (`-shortest` against
`-stream_loop -1` sides).

Full-length seek + segment cache
────────────────────────────────
Unlike Live TV (which stays paced with `-re`), the vertical subs encode at full
speed — the client reads static segment files off disk, the encoder outruns the
client, and there is no client-paced backpressure anywhere in the chain.  The
proxy owns a synthetic VOD playlist covering the whole center duration, so the
entire timeline is seekable immediately; the master only ever produces the .ts
files, indexed so segment N covers center time [N*4, N*4+4).

A session tracks which segment indexes exist on disk (produced ranges — seeks
create holes).  `ensure_segment(index)` is the single recovery path for a missing
segment: it relaunches the subs with `-ss index*4` on the center and
`-start_number index` on the master, into the *same* session dir (old segments
stay valid).  That one path serves seek-past-head, seek-into-a-gap, and
resume-after-reap identically.  Sides are phased to the timeline (`-ss
seek mod side_duration`) so a re-encoded segment is byte-compatible with what the
continuous encode would have produced.

Teardown is two-stage: stage 1 reaps the FFmpeg processes after
VERTICAL_IDLE_TIMEOUT of no fetches but keeps the cached segments; stage 2
(VERTICAL_SESSION_TTL or an explicit stop) deletes the temp dir and frees the
concurrency slot.  A fetch resets both clocks; a client that unpauses after a
reap transparently respins via `ensure_segment`.

Sessions are keyed by a **play-session id** (`{scene_id}-{nonce}`): fresh side
clips per play, stable across seeks within that play.
"""
import asyncio
import logging
import math
import os
import re
import shutil
import tempfile
import time
from datetime import datetime, timezone

import config
from core import stash_client
from core.hw_encoder import EncoderConfig, resolve_h264_encoder
from core.vertical import redact_apikey, vdebug
from core.vertical_selection import select_side_clips
# Reuse the Live TV pipe backends + platform probes verbatim — the byte-forwarding
# plumbing is identical; only the FFmpeg graph feeding it differs.  The stderr
# line iterator is shared too (it handles FFmpeg's \r-terminated progress lines).
from api.live_tv_engine import (
    _PipeBackend, _FifoPipeBackend, _TcpRelayPipeBackend,
    _IS_WINDOWS, _HAS_MKFIFO, _iter_stderr_lines,
)

logger = logging.getLogger(__name__)

# One segment = 4 s.  This MUST stay in lockstep with the master's
# `force_key_frames expr:gte(t,n_forced*4)` and `-hls_time 4`, and with the
# synthetic playlist's EXTINF cadence — segment index N ⇔ center time N*4.
SEG_DURATION = 4.0

# A forward segment request within this many segments of the live encode head is
# treated as normal read-ahead and WAITED for (the run advances to the end on its
# own, so it will produce it); only a request farther ahead than this is treated
# as a real forward seek and relaunches the encode at that point.  This MUST exceed
# any client's read-ahead buffer — ExoPlayer/hls.js buffer ~30–60 s (≈8–15
# segments) ahead, and treating that read-ahead as a seek causes a relaunch storm
# (each buffered-ahead segment kills and restarts the healthy encode).  24 segments
# = 96 s of look-ahead, comfortably above typical client buffers.
_FORWARD_WAIT_SEGMENTS = 24
# How long a segment fetch waits for the encoder to produce the segment before
# giving up (the client simply retries).
_SEG_WAIT_TIMEOUT = 15.0
# Readiness gate: fail only if no new segment appears within this window (a slow
# backend can take tens of seconds per segment — see _await_ready).  Each new
# segment resets it, so a slow-but-progressing encode is never spuriously failed.
_READY_STALL_SECS = 45.0
# How long to wait for the master to finalize and exit on EOF (after the backend
# closes) before hard-killing it.  EOF→finalize is normally sub-second; this only
# caps a stuck flush.  A hard kill would drop the last partial segment, so we
# prefer the graceful exit (esp. on Windows, where terminate() is a hard kill).
_MASTER_GRACE_SECS = 8.0
# Refuse a launch when free space on the HLS temp volume is below this floor: a
# session renders the full center clip (~1–2 GB per 30 min at 1080p30).
_DISK_FREE_FLOOR_BYTES = 2 * 1024 * 1024 * 1024

# Read-rate cap for the vertical subs on the Windows TCP-relay backend only.  The
# Read-rate cap for the vertical subs on the Windows TCP-relay backend.  The
# synthetic VOD playlist assumes the encoder outruns the client (so read-ahead
# cache-hits), which wants a fast encode; full-speed raw 1080p30 is too much for
# the single-threaded relay, so cap it a few× above realtime.  The FIFO backend
# (Linux) has no relay and runs full-speed (VERTICAL_READRATE).
_TCP_RELAY_READRATE = 3.0

_SEG_FILE_RE = re.compile(r"seg(\d+)\.ts$")


def _stash_stream_url(scene_id: str) -> str:
    stash_base = config.get_stash_base()
    api_key = getattr(config, "STASH_API_KEY", "")
    url = f"{stash_base}/scene/{scene_id}/stream"
    if api_key:
        url += f"?apikey={api_key}"
    return url


def _in_seek(seconds: float) -> list:
    return ["-ss", f"{seconds:.3f}"] if seconds and seconds > 0.1 else []


def build_composite_cmd(ffmpeg_bin: str, left_id: str, center_id: str,
                        right_id: str, seek: float, sub_out_v: str, *,
                        left_seek: float = 0.0, right_seek: float = 0.0,
                        pace_args: tuple = ("-re",), duration: float | None = None) -> list:
    """Shape A composite: 3 HTTP inputs → per-lane scale/crop → hstack → pad → raw video.

    Sides loop forever (`-stream_loop -1`); the center is the clock.  `-ss seek`
    (input seek) applies to the center; `left_seek`/`right_seek` phase the looping
    sides so a relaunch at a mid-timeline position renders the sides exactly as the
    continuous start-to-finish encode would have (side phase = seek mod side_dur).

    `duration` (center length minus seek) bounds the output with `-t`.  This is
    load-bearing: `-shortest` is a *no-op* here because the filtergraph has a
    single output stream, so at center EOF `hstack` just stalls on the ended input
    and the composite hangs forever (frozen a couple frames short of the final
    segment → readiness never completes, and long clips never end).  `-t` forces a
    clean exit at the center duration.  Callers that omit it (the Vertical TV
    channel) keep the previous, un-bounded behavior byte-for-byte.
    Lane geometry: 1080-high scale, crop to 608×1080, hstack→1824×1080, pad to
    exactly 1920×1080.

    `pace_args` are the input-pacing tokens spliced before every `-i`.  The
    default `("-re",)` is realtime pacing — what the always-on Vertical TV Live TV
    channel (api/live_tv_engine.py) needs and what it passes by omitting the
    argument, so its command is byte-identical to before.  The VOD compositor
    passes the VERTICAL_READRATE-derived tokens instead (empty = unlimited
    full-speed encode).

    Module-level so both the VOD compositor (`_VerticalSessionManager`) and the
    Vertical TV channel share the one filtergraph definition — see
    docs/Triptych.md § Vertical TV.
    """
    left_url = _stash_stream_url(left_id)
    center_url = _stash_stream_url(center_id)
    right_url = _stash_stream_url(right_id)

    common_pre = [
        ffmpeg_bin, "-y", "-hide_banner", "-loglevel", "info", "-stats",
        "-reconnect", "1", "-reconnect_streamed", "1",
        "-reconnect_at_eof", "1", "-reconnect_delay_max", "5",
    ]
    pace = list(pace_args)

    return common_pre + [
        # input 0 — left side (loops), phased to the timeline
        *pace, "-stream_loop", "-1", *_in_seek(left_seek), "-i", left_url,
        # input 1 — center (the clock); seek applies here only
        *pace, *_in_seek(seek), "-i", center_url,
        # input 2 — right side (loops), phased to the timeline
        *pace, "-stream_loop", "-1", *_in_seek(right_seek), "-i", right_url,
        # Each lane: scale to COVER the 608×1080 cell (force_original_aspect_ratio=
        # increase), then centre-crop to exactly 608×1080.  A plain `scale=-2:1080`
        # leaves the width driven by aspect, so a source narrower than 608/1080
        # (≈0.563, i.e. taller than 9:16 — e.g. a 720×1282 clip → 606 px wide)
        # produces a frame narrower than the crop, and `crop=608:1080` aborts the
        # whole composite ("Invalid too big size for width 608").  Cover-then-crop
        # guarantees the scaled frame is always ≥ the crop in both dimensions.
        "-filter_complex",
        "[0:v]scale=608:1080:force_original_aspect_ratio=increase:force_divisible_by=2,crop=608:1080,setsar=1[l];"
        "[1:v]scale=608:1080:force_original_aspect_ratio=increase:force_divisible_by=2,crop=608:1080,setsar=1[c];"
        "[2:v]scale=608:1080:force_original_aspect_ratio=increase:force_divisible_by=2,crop=608:1080,setsar=1[r];"
        "[l][c][r]hstack=inputs=3,pad=1920:1080:(ow-iw)/2:0:black,fps=30,format=yuv420p[v]",
        "-map", "[v]", "-shortest",
        *(["-t", f"{duration:.3f}"] if duration and duration > 0 else []),
        "-pix_fmt", "yuv420p", "-f", "rawvideo", sub_out_v,
    ]


def build_audio_cmd(ffmpeg_bin: str, center_id: str, seek: float, sub_out_a: str,
                    *, pace_args: tuple = ("-re",), duration: float | None = None) -> list:
    """Center-only audio, normalized to 48 kHz stereo PCM (reuses the Live TV
    normalization). `-map 0:a:0?` keeps a silent center from failing the sub.

    `pace_args` matches `build_composite_cmd` — default realtime `-re` for the
    Vertical TV channel; the VOD compositor passes the readrate tokens.  `duration`
    bounds the sub with `-t` so it ends with the composite (kept in lockstep).
    """
    center_url = _stash_stream_url(center_id)
    common_pre = [
        ffmpeg_bin, "-y", "-hide_banner", "-loglevel", "info", "-stats",
        "-reconnect", "1", "-reconnect_streamed", "1",
        "-reconnect_at_eof", "1", "-reconnect_delay_max", "5",
    ]
    return common_pre + [
        *list(pace_args), *_in_seek(seek), "-i", center_url,
        "-map", "0:a:0?", "-vn", "-sn",
        "-af",
        "aresample=async=1000:first_pts=0,"
        "aformat=sample_rates=48000:channel_layouts=stereo",
        "-ar", "48000", "-ac", "2", "-c:a", "pcm_s16le",
        *(["-t", f"{duration:.3f}"] if duration and duration > 0 else []),
        "-f", "s16le", sub_out_a,
    ]


def total_segments_for(duration: float) -> int | None:
    """Number of 4 s segments covering `duration` (final segment shorter)."""
    if not duration or duration <= 0:
        return None
    return max(1, math.ceil(duration / SEG_DURATION))


class _VerticalSessionManager:
    """One master FFmpeg HLS process per active triptych play-session.

    Modeled on live_tv_engine._FFmpegChannelManager but keyed by play-session id
    instead of channel id.  A session owns a temp dir of cached segments for its
    whole lifetime (launch → stage-2 destroy) and runs, at any moment, at most one
    FFmpeg trio (master + composite + audio).  Relaunches ("respins") stop the
    current trio and start a fresh one at a new start index into the same dir —
    they never count against the concurrency cap because it's the same session.
    """

    def __init__(self):
        self._procs:     dict[str, asyncio.subprocess.Process] = {}   # session → master proc
        self._dirs:      dict[str, str]   = {}                        # session → temp dir (occupancy = slot held)
        self._last:      dict[str, float] = {}                        # session → last fetch ts (both teardown clocks)
        self._stderr:    dict[str, list[str]] = {}                    # session → rolling stderr (last 60)
        self._stderr_fh: dict[str, object] = {}                       # session → per-session log file handle
        self._launch_info: dict[str, dict] = {}                       # session → {start_index, sides, center, pid, ...}
        self._backends:  dict[str, _PipeBackend] = {}                 # session → pipe backend
        self._subs:      dict[str, list] = {}                         # session → [composite_sub, audio_sub]
        self._monitors:  dict[str, asyncio.Task] = {}                 # session → run-monitor task
        self._sides:     dict[str, list] = {}                         # session → [left_id, right_id] (stable across seeks)
        self._side_durs: dict[str, list] = {}                         # session → [left_dur, right_dur] (for side phasing)
        self._center_id: dict[str, str]  = {}                         # session → center scene id
        self._center_dur: dict[str, float] = {}                       # session → center duration (drives total segments)
        self._reaped:    dict[str, bool]  = {}                        # session → stage-1 process reap done
        self._stopped:   dict[str, bool]  = {}                        # session → explicit stop requested (suppresses backfill)
        self._backfill:  dict[str, asyncio.Task] = {}                 # session → background gap-backfill task
        self._backfill_last: dict[str, int] = {}                      # session → last gap a backfill run attempted (stall guard)
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

    def _log_session(self, sid: str, line: str) -> None:
        fh = self._stderr_fh.get(sid)
        if fh is not None:
            try:
                fh.write(line + "\n")
                fh.flush()
            except Exception:
                pass

    # ── public API ─────────────────────────────────────────────────────────────

    def touch(self, sid: str) -> None:
        """Record a fetch; resets both teardown clocks and (re)starts the watchdog."""
        self._last[sid] = time.time()
        if self._watchdog is None or self._watchdog.done():
            self._watchdog = asyncio.create_task(self._idle_loop())

    def seg_dir(self, sid: str) -> str | None:
        return self._dirs.get(sid)

    def is_alive(self, sid: str) -> bool:
        p = self._procs.get(sid)
        return p is not None and p.returncode is None

    def total_segments(self, sid: str) -> int | None:
        return total_segments_for(self._center_dur.get(sid))

    def session_for_scene(self, raw_scene_id: str) -> str | None:
        """An existing non-stopped session compositing this center scene, for
        dedupe — so PlaybackInfo's pre-warm and the /Videos/stream guard converge
        on ONE session per scene instead of each minting its own (which doubled
        the encode work and cap/disk pressure).  Prefers a live session; falls
        back to a cached (reaped) one that a fetch will transparently respin."""
        raw = str(raw_scene_id)
        live = cached = None
        for sid, cid in self._center_id.items():
            if cid != raw or self._stopped.get(sid) or sid not in self._dirs:
                continue
            if self.is_alive(sid):
                live = sid
            else:
                cached = sid
        return live or cached

    def active_count(self) -> int:
        """Sessions with a live encode (what the concurrency cap limits).

        Counts running masters, not cached dirs: a reaped/finished session keeps
        its segments on disk but consumes no CPU, so it must not hold a cap slot —
        otherwise abandoned or paused sessions strand slots until the session TTL
        (a resume respins the same session and doesn't re-check the cap anyway)."""
        return sum(1 for sid in self._dirs if self.is_alive(sid))

    async def ensure(self, sid: str, center_scene: dict, seek: float | None = None) -> bool:
        """Ensure the session exists and (optionally) honor a seek position.

        - Session not started → launch at the seek-derived index (0 if none).
        - Started, seek=None   → no-op steady state (a manifest poll without a
          position never disturbs the running encode).
        - Started, seek given  → route through `ensure_segment` for that index.

        Returns False (caller falls back to single-video playback) when the
        concurrency cap is hit, side selection finds no other vertical scenes,
        the center has no duration, disk is low, or FFmpeg fails to start.
        """
        idx = int(max(0.0, seek or 0.0) // SEG_DURATION)
        async with self._lock:
            fresh_launch = sid not in self._dirs
            if fresh_launch and not await self._launch(sid, center_scene, idx):
                return False
        if fresh_launch:
            # Readiness gate runs OUTSIDE the lock: the run monitor needs the lock
            # to terminate the master (which finalizes a short clip's last segment),
            # and a slow launch shouldn't block other sessions for the whole wait.
            if not await self._await_launch_ready(sid, idx):
                async with self._lock:
                    await self._destroy(sid, reason="readiness gate failed")
                return False
            self.touch(sid)
            return True
        if seek is None:
            self.touch(sid)
            return True
        return await self.ensure_segment(sid, idx, center_scene)

    async def _await_launch_ready(self, sid: str, start_index: int) -> bool:
        """Post-spawn readiness wait for a fresh launch (no lock held)."""
        proc = self._procs.get(sid)
        if proc is None:
            return False
        min_ready = max(1, int(getattr(config, "VERTICAL_READY_SEGMENTS", 2)))
        total = self.total_segments(sid)
        if total:
            min_ready = min(min_ready, total)
        return await self._await_ready(sid, proc, start_index, min_ready)

    async def seek(self, sid: str, center_scene: dict, position: float) -> bool:
        """Explicit center seek — routed through the single `ensure_segment` path."""
        return await self.ensure_segment(sid, int(max(0.0, position) // SEG_DURATION), center_scene)

    async def ensure_segment(self, sid: str, index: int, center_scene: dict | None = None) -> bool:
        """Guarantee segment `index` is (or will shortly be) on disk — the single
        recovery path for seek-past-head, seek-into-a-gap, and resume-after-reap.

        Fast path: the file already exists.  Otherwise, under the lock, decide:
        launch (session gone), wait (the live run will reach it imminently), or
        relaunch at `index` (back-seek / far forward seek / dead encoder).  The
        long *file-poll* happens outside the lock; the launch/relaunch spawn
        (including its endpoint-attach waits) is under the lock, as elsewhere in
        this manager, so a concurrent respin briefly serializes other sessions'
        lock-taking calls — acceptable given the cap is small and Stash is
        single-user.
        """
        async with self._lock:
            total = self.total_segments(sid)
            if total is None and center_scene is not None:
                total = total_segments_for(
                    float((center_scene.get("files") or [{}])[0].get("duration") or 0.0))
            if total is not None and total > 0:
                index = max(0, min(index, total - 1))
            else:
                index = max(0, index)

            if self._seg_exists(sid, index):
                self.touch(sid)
                return True

            if sid not in self._dirs:
                if center_scene is None:
                    return False
                if not await self._launch(sid, center_scene, index):
                    return False
            elif self._run_covers(sid, index):
                pass  # the live run will produce it — just wait below
            else:
                if not await self._relaunch(sid, index):
                    return False

        self.touch(sid)
        return await self._await_segment(sid, index)

    async def stop(self, sid: str, reason: str = "explicit stop") -> None:
        async with self._lock:
            await self._destroy(sid, reason=reason)

    async def cleanup_all(self) -> None:
        for sid in list(self._dirs.keys()):
            await self.stop(sid, reason="shutdown")

    # ── segment bookkeeping / range tracker ────────────────────────────────────

    def _seg_path(self, sid: str, index: int) -> str | None:
        d = self._dirs.get(sid)
        return os.path.join(d, f"seg{index:05d}.ts") if d else None

    def _seg_exists(self, sid: str, index: int) -> bool:
        p = self._seg_path(sid, index)
        return bool(p) and os.path.exists(p)

    def _produced_indices(self, sid: str) -> set:
        """Segment indexes present on disk (the produced ranges — seeks leave holes)."""
        d = self._dirs.get(sid)
        out: set = set()
        if not d:
            return out
        try:
            for f in os.listdir(d):
                m = _SEG_FILE_RE.match(f)
                if m:
                    out.add(int(m.group(1)))
        except OSError:
            pass
        return out

    def _run_head(self, sid: str, start: int) -> int:
        """Next index the current forward run is expected to write (its live edge)."""
        run = [i for i in self._produced_indices(sid) if i >= start]
        return (max(run) + 1) if run else start

    def _run_covers(self, sid: str, index: int) -> bool:
        """True when the live run will serve `index` by waiting, no relaunch needed.

        A live run started at `start_index` encodes monotonically to the center's
        end, so it will *eventually* produce any index >= start_index — the only
        question is whether waiting is acceptable.  Read-ahead (index within
        `_FORWARD_WAIT_SEGMENTS` of the head) waits; a far-forward seek relaunches
        to jump the encode there; a backward request (index < start) relaunches
        because this run will never revisit it.
        """
        if not self.is_alive(sid):
            return False
        start = int(self._launch_info.get(sid, {}).get("start_index", 0))
        if index < start:
            return False
        return index <= self._run_head(sid, start) + _FORWARD_WAIT_SEGMENTS

    def _first_gap(self, sid: str) -> int | None:
        total = self.total_segments(sid)
        if not total:
            return None
        produced = self._produced_indices(sid)
        for i in range(total):
            if i not in produced:
                return i
        return None

    def _range_summary(self, sid: str) -> str:
        """Compact contiguous-range string of produced segments, for logging."""
        produced = sorted(self._produced_indices(sid))
        if not produced:
            return "[]"
        ranges = []
        start = prev = produced[0]
        for i in produced[1:]:
            if i == prev + 1:
                prev = i
            else:
                ranges.append((start, prev))
                start = prev = i
        ranges.append((start, prev))
        return ",".join(f"{a}-{b}" if a != b else f"{a}" for a, b in ranges)

    @staticmethod
    def _phase(start_index: int, side_dur: float) -> float:
        """Side start offset so a relaunch renders the sides at their timeline phase."""
        if not side_dur or side_dur <= 0:
            return 0.0
        return (start_index * SEG_DURATION) % side_dur

    async def _scene_duration(self, scene_id: str) -> float:
        try:
            sc = await stash_client.get_scene(str(scene_id))
            files = (sc or {}).get("files") or []
            return float(files[0].get("duration") or 0.0) if files else 0.0
        except Exception:
            return 0.0

    async def _await_segment(self, sid: str, index: int, timeout: float = _SEG_WAIT_TIMEOUT) -> bool:
        """Poll for segment `index` to appear on disk, up to `timeout` seconds."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._seg_exists(sid, index):
                self.touch(sid)
                return True
            if sid not in self._dirs:
                return False  # session destroyed under us
            bf = self._backfill.get(sid)
            backfilling = bf is not None and not bf.done()
            if not self.is_alive(sid) and not backfilling:
                # No run producing it and no backfill working toward it — it
                # won't appear; the caller retries.
                return self._seg_exists(sid, index)
            await asyncio.sleep(0.25)
        return self._seg_exists(sid, index)

    # ── encoder / pacing helpers ───────────────────────────────────────────────

    async def _resolve_encoder(self) -> EncoderConfig:
        """Resolve VERTICAL_HWACCEL to a probed-working encoder.

        Delegates to the shared ``core.hw_encoder`` probe (cached after the
        startup probe, so per-session launches are cheap); the probe test-encodes
        a frame and blocks, so it's run off the event loop.
        """
        mode = str(getattr(config, "VERTICAL_HWACCEL", "auto")).lower()
        ffmpeg_bin = getattr(config, "FFMPEG_PATH", "ffmpeg")
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, resolve_h264_encoder, mode, ffmpeg_bin)

    @staticmethod
    def _pace_args() -> tuple:
        """Input pacing for the VOD subs: unlimited unless VERTICAL_READRATE caps it."""
        try:
            rr = float(getattr(config, "VERTICAL_READRATE", 0) or 0)
        except (TypeError, ValueError):
            rr = 0.0
        return ("-readrate", f"{rr:g}") if rr > 0 else ()

    # ── launch / run lifecycle ─────────────────────────────────────────────────

    async def _launch(self, sid: str, center_scene: dict, start_index: int) -> bool:
        """Initial session launch (lock held): cap + disk guard + side selection +
        session dir, then spawn the first run.  Returns False → single-video
        fallback.
        """
        if not _HAS_MKFIFO and not _IS_WINDOWS:
            logger.error(
                "Vertical FFmpeg: pipe-based playout requires os.mkfifo (Linux/macOS) "
                "or the Windows TCP-relay fallback — session cannot start here."
            )
            return False

        # Concurrency cap — count OTHER sessions with a LIVE encode.  Cached /
        # reaped sessions burn no CPU, so they don't hold a slot (otherwise
        # abandoned or paused sessions strand the cap until the TTL).
        max_sessions = int(getattr(config, "VERTICAL_MAX_SESSIONS", 2))
        others = [s for s in self._dirs if s != sid and self.is_alive(s)]
        vdebug(logger, f"Vertical: launching {sid!r} — {len(others)} other live encode(s), cap {max_sessions}")
        if len(others) >= max_sessions:
            logger.warning(
                f"Vertical: concurrency cap {max_sessions} reached (live encodes: {others}) "
                f"— refusing {sid!r}; client falls back to single video"
            )
            return False

        center_id = str(center_scene.get("id"))
        center_dur = float((center_scene.get("files") or [{}])[0].get("duration") or 0.0)
        if center_dur <= 0:
            logger.warning(
                f"Vertical: center {center_id} has no duration — single-video fallback (session {sid!r})"
            )
            return False

        # Disk guardrail — a session renders the full center clip.
        hls_base = getattr(config, "HLS_TEMP_DIR", None) or tempfile.gettempdir()
        try:
            free = shutil.disk_usage(hls_base).free
        except Exception:
            free = None
        if free is not None and free < _DISK_FREE_FLOOR_BYTES:
            logger.warning(
                f"Vertical: free disk {free / 1e9:.1f} GB on {hls_base} below "
                f"{_DISK_FREE_FLOOR_BYTES / 1e9:.1f} GB floor — refusing {sid!r}; single-video fallback"
            )
            return False

        # Side-clip selection — once per session (reused across every respin so the
        # sides stay stable for the whole play).
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
        if sid not in self._side_durs:
            self._side_durs[sid] = [await self._scene_duration(s) for s in sides]

        d = tempfile.mkdtemp(prefix=f"sjv_{sid[:8]}_", dir=(getattr(config, "HLS_TEMP_DIR", None) or None))
        self._dirs[sid] = d
        self._center_id[sid] = center_id
        self._center_dur[sid] = center_dur
        self._reaped[sid] = False
        self._stopped[sid] = False
        self._stderr[sid] = []
        self._stderr_fh[sid] = self._open_stderr_file(sid)

        total = total_segments_for(center_dur)
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        self._log_session(
            sid,
            f"\n===== FFmpeg session start {ts} | session={sid} center={center_id} "
            f"sides={sides} side_durs={self._side_durs[sid]} center_dur={center_dur:.1f}s "
            f"total_segments={total} ====="
        )

        # Spawn only; the readiness gate runs in ensure() outside the lock (a
        # short clip's final segment is finalized by the run monitor, which needs
        # the lock this call holds — waiting here would deadlock).
        ok = await self._start_run(sid, start_index, min_ready=0)
        if not ok:
            await self._destroy(sid, reason="initial launch failed (spawn)")
            return False
        self.touch(sid)
        return True

    async def _start_run(self, sid: str, start_index: int, min_ready: int) -> bool:
        """Spawn one FFmpeg trio (master + composite + audio) at `start_index`
        into the session's existing dir (lock held).  The session dir, sides, and
        durations must already be set.  `min_ready` > 0 gates on that many segment
        files (initial launch); 0 returns as soon as the trio is spawned (respin /
        backfill — the caller waits on the specific segment).
        """
        d = self._dirs[sid]
        center_id = self._center_id[sid]
        left_id, right_id = self._sides[sid][0], self._sides[sid][1]
        left_dur, right_dur = (self._side_durs.get(sid) or [0.0, 0.0])[:2]

        backend: _PipeBackend = _FifoPipeBackend(d) if _HAS_MKFIFO else _TcpRelayPipeBackend(d)
        try:
            await backend.start()
        except Exception as exc:
            logger.error(f"Vertical FFmpeg: pipe backend setup failed ({backend.kind}) — {exc}")
            return False
        self._backends[sid] = backend
        master_in_v, master_in_a = backend.master_inputs()
        sub_out_v, sub_out_a = backend.sub_outputs()
        vdebug(logger, f"Vertical FFmpeg: session {sid!r} pipe backend={backend.kind} video={master_in_v} audio={master_in_a}")

        enc = await self._resolve_encoder()
        ffmpeg_bin = getattr(config, "FFMPEG_PATH", "ffmpeg")
        seg_tmpl = os.path.join(d, "seg%05d.ts")
        manifest = os.path.join(d, "stream.m3u8")  # written by the hls muxer; we serve a synthetic playlist instead

        # Master: raw video + raw audio pipes → single H.264/AAC HLS ladder.  Same
        # two-input, probe-suppressed design as Live TV.  `-start_number` names the
        # first segment seg{start_index}, and force_key_frames pins keyframes to
        # the 4 s segment grid, so segment N always covers center time [N*4,N*4+4).
        # The muxer keeps all .ts files on disk (no delete flag); we ignore its own
        # playlist entirely.  `temp_file` is essential: the synthetic VOD playlist
        # lists every segment up front, so a client can request seg{N} the instant
        # it seeks there — without temp_file the muxer's seg{N}.ts exists (and is
        # served) while still being written (or is left partial when a respin/reap
        # SIGTERMs the master), which `_seg_exists`/`_await_segment` would trust as
        # complete and stream truncated.  temp_file writes to seg{N}.ts.tmp and
        # renames on finalize, so seg{N}.ts only appears once whole.
        # `enc` supplies the encoder/device/hwupload groups so decode + hstack stay
        # on CPU.
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
            "-hls_flags", "independent_segments+temp_file",
            "-hls_list_size", "0",
            "-start_number", str(start_index),
            "-hls_segment_filename", seg_tmpl,
            manifest,
        ]

        logger.info(
            f"Vertical FFmpeg: session {sid!r} run start_index={start_index} "
            f"(center time {start_index * SEG_DURATION:.0f}s) center={center_id} "
            f"sides={self._sides[sid]} backend={backend.kind} encoder={enc.codec} "
            f"(mode={getattr(config, 'VERTICAL_HWACCEL', 'auto')!r})"
        )
        vdebug(logger, f"Vertical FFmpeg master cmd: {redact_apikey(' '.join(master_cmd))}")
        self._log_session(sid, f"----- run start start_index={start_index} encoder={enc.codec} -----")
        self._log_session(sid, f"master cmd: {redact_apikey(' '.join(master_cmd))}")

        try:
            proc = await asyncio.create_subprocess_exec(
                *master_cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
            )
        except Exception as exc:
            logger.error(f"Vertical FFmpeg: master launch failed — {exc}")
            await self._teardown_run(sid, reason="master launch failed")
            return False
        self._procs[sid] = proc
        self._launch_info[sid] = {
            "start_index": start_index, "sides": self._sides[sid], "center": center_id,
            "pid": proc.pid, "start_ts": time.time(), "encoder": enc.codec,
        }
        logger.info(f"Vertical FFmpeg: session {sid!r} master pid={proc.pid}")
        asyncio.create_task(self._drain_stderr(proc, sid))

        v_ready = await backend.wait_master_video_attached(timeout=10.0)
        if not v_ready:
            logger.error(f"Vertical FFmpeg: master never attached to video endpoint for {sid!r} — aborting run")
            await self._teardown_run(sid, reason="master video-endpoint attach timeout")
            return False

        # Pacing: full-speed on the FIFO backend (kernel forwards the bytes), but
        # the TCP-relay backend forwards raw video through the single-threaded
        # asyncio loop and can't sustain multi-Gbps full-speed 1080p30 — that
        # saturation starves the master's audio-endpoint connect and aborts the run
        # ("master never attached to audio endpoint").  Cap it just above realtime
        # on TCP (Windows dev) so the encoder still outruns the client; full-speed
        # encode is a FIFO-backend (Linux) property.
        pace = ("-readrate", f"{_TCP_RELAY_READRATE:g}") if backend.kind == "tcp" else self._pace_args()
        seek_s = start_index * SEG_DURATION
        # Bound the subs to the remaining center length so they exit at center EOF
        # (`-shortest` alone is a no-op on the single-output composite — it would
        # otherwise hang, and the final segment would never finalize).
        remaining = max(0.1, self._center_dur.get(sid, 0.0) - seek_s)
        composite_cmd = build_composite_cmd(
            ffmpeg_bin, left_id, center_id, right_id, seek_s, sub_out_v,
            left_seek=self._phase(start_index, left_dur),
            right_seek=self._phase(start_index, right_dur),
            pace_args=pace, duration=remaining,
        )
        vdebug(logger, f"Vertical FFmpeg composite sub cmd: {redact_apikey(' '.join(composite_cmd))}")
        self._log_session(sid, f"composite cmd: {redact_apikey(' '.join(composite_cmd))}")
        try:
            sub_v = await asyncio.create_subprocess_exec(
                *composite_cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
            )
        except Exception as exc:
            logger.error(f"Vertical FFmpeg: composite sub spawn failed for {sid!r}: {exc}")
            await self._teardown_run(sid, reason="composite sub spawn failed")
            return False
        logger.info(f"Vertical FFmpeg: session {sid!r} composite pid={sub_v.pid}")
        asyncio.create_task(self._drain_sub_stderr(sub_v, sid, "composite"))

        a_ready = await backend.wait_master_audio_attached(timeout=15.0)
        if not a_ready:
            logger.error(f"Vertical FFmpeg: master never attached to audio endpoint for {sid!r} — aborting run")
            try: sub_v.terminate()
            except Exception: pass
            await self._teardown_run(sid, reason="master audio-endpoint attach timeout")
            return False

        audio_cmd = build_audio_cmd(ffmpeg_bin, center_id, seek_s, sub_out_a, pace_args=pace, duration=remaining)
        vdebug(logger, f"Vertical FFmpeg audio sub cmd: {redact_apikey(' '.join(audio_cmd))}")
        self._log_session(sid, f"audio cmd: {redact_apikey(' '.join(audio_cmd))}")
        try:
            sub_a = await asyncio.create_subprocess_exec(
                *audio_cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
            )
        except Exception as exc:
            logger.error(f"Vertical FFmpeg: audio sub spawn failed for {sid!r}: {exc}")
            try: sub_v.terminate()
            except Exception: pass
            await self._teardown_run(sid, reason="audio sub spawn failed")
            return False
        logger.info(f"Vertical FFmpeg: session {sid!r} audio pid={sub_a.pid}")
        asyncio.create_task(self._drain_sub_stderr(sub_a, sid, "audio"))

        # Silent-center safety net (mirrors the Live TV channel's _feed_one_vertical_round):
        # a center with no audio stream makes `-map 0:a:0?` map nothing, so the audio sub
        # exits immediately — and the master, still mapping `1:a:0`, then blocks forever
        # waiting for audio it never receives (the parent's keepalive FD means no EOF),
        # which backpressures and stalls the composite (no segments → readiness fails).
        # If the audio sub dies within 2 s, replace it with an lavfi silence generator so
        # the audio pipe is fed for the run's duration.
        try:
            rc_a_fast = await asyncio.wait_for(asyncio.shield(sub_a.wait()), timeout=2.0)
            if rc_a_fast != 0:
                silence_cmd = [
                    ffmpeg_bin, "-y", "-hide_banner", "-loglevel", "warning",
                    "-f", "lavfi", "-i", "aevalsrc=0:c=stereo:s=48000",
                    "-t", f"{remaining:.3f}",
                    "-ar", "48000", "-ac", "2", "-c:a", "pcm_s16le",
                    "-f", "s16le", sub_out_a,
                ]
                self._log_session(sid, f"audio silence-filler cmd: {' '.join(silence_cmd)}")
                sub_a = await asyncio.create_subprocess_exec(
                    *silence_cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
                )
                logger.info(
                    f"Vertical FFmpeg: session {sid!r} center {center_id} has no audio "
                    f"— spawned {remaining:.1f}s silence filler pid={sub_a.pid}"
                )
        except asyncio.TimeoutError:
            pass  # audio sub still running after 2 s — it has audio, proceed normally

        self._subs[sid] = [sub_v, sub_a]
        self._monitors[sid] = asyncio.create_task(self._monitor_run(sid, sub_v, sub_a))

        if min_ready > 0:
            ready = await self._await_ready(sid, proc, start_index, min_ready)
            if not ready:
                await self._teardown_run(sid, reason="readiness gate failed (master died or segment timeout)")
                return False
        return True

    async def _relaunch(self, sid: str, start_index: int) -> bool:
        """Respin the run at `start_index` into the same session dir (lock held).

        The single recovery path for seek-past-head / back-seek-into-a-gap /
        resume-after-reap.  Cancels any background backfill (real requests win),
        stops the current trio, and starts a fresh one — never counting against
        the concurrency cap (same session).
        """
        old = self._launch_info.get(sid, {}).get("start_index")
        if old is not None and start_index < old:
            trigger = "back-seek/gap"
        elif not self.is_alive(sid):
            trigger = "resume/gap"
        else:
            trigger = "seek-past-head"
        self._cancel_backfill(sid)
        self._reaped[sid] = False  # a respin revives a reaped session
        await self._teardown_run(sid, reason=f"relaunch → seg {start_index}")
        logger.info(
            f"Vertical: session {sid!r} relaunch [{trigger}] start_index {old} → {start_index} "
            f"(center {start_index * SEG_DURATION:.0f}s; produced ranges {self._range_summary(sid)})"
        )
        return await self._start_run(sid, start_index, min_ready=0)

    async def _monitor_run(self, sid: str, sub_v: asyncio.subprocess.Process,
                           sub_a: asyncio.subprocess.Process) -> None:
        """Await the run's subs.  Natural exit = the center reached its end for
        this run: finalize (stop the idle master + backend, keep the segments) and
        schedule gap backfill.  Cancellation = an external teardown already owns
        the cleanup, so do nothing.
        """
        try:
            rc_v, rc_a = await asyncio.gather(sub_v.wait(), sub_a.wait())
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.debug(f"Vertical FFmpeg: monitor for {sid!r} ended", exc_info=True)
            return
        logger.info(f"Vertical FFmpeg: session {sid!r} run subs finished (composite rc={rc_v}, audio rc={rc_a})")
        async with self._lock:
            if self._subs.get(sid) != [sub_v, sub_a]:
                return  # superseded by a newer run / already torn down
            await self._kill_procs(sid)  # subs already exited; stop the idle master + backend
            self._monitors.pop(sid, None)
            self._log_session(sid, "----- run end (center reached) -----")
        self._schedule_backfill(sid)

    # ── background gap backfill ─────────────────────────────────────────────────

    def _schedule_backfill(self, sid: str) -> None:
        """Kick a one-gap backfill run if the session is healthy and has gaps.

        Each backfill run fills from the first gap to the center end, and its own
        completion reschedules — so the chain terminates once no gap remains.
        Skipped after an explicit stop or a stage-1 reap.
        """
        if self._stopped.get(sid) or self._reaped.get(sid) or sid not in self._dirs:
            return
        t = self._backfill.get(sid)
        if t is not None and not t.done():
            return
        self._backfill[sid] = asyncio.create_task(self._run_backfill(sid))

    async def _run_backfill(self, sid: str) -> None:
        """Fill the earliest gap so backward seeks eventually cache-hit.  One
        relaunch at a time; yields to any live (user-driven) run.
        """
        try:
            async with self._lock:
                if self._stopped.get(sid) or self._reaped.get(sid) or sid not in self._dirs:
                    return
                if self.is_alive(sid):
                    return  # a user run is active — it reschedules backfill on completion
                gap = self._first_gap(sid)
                if gap is None:
                    vdebug(logger, f"Vertical: session {sid!r} backfill complete — no gaps remain")
                    return
                if self._backfill_last.get(sid) == gap:
                    # The previous backfill run at this gap made no progress (e.g. a
                    # stuck final partial segment) — stop rather than loop forever.
                    vdebug(logger, f"Vertical: session {sid!r} backfill stalled at seg {gap} — stopping")
                    return
                self._backfill_last[sid] = gap
                logger.info(
                    f"Vertical: session {sid!r} backfill start at seg {gap} "
                    f"(produced ranges {self._range_summary(sid)})"
                )
                ok = await self._start_run(sid, gap, min_ready=0)
                if not ok:
                    logger.warning(f"Vertical: session {sid!r} backfill relaunch at seg {gap} failed")
                    return
            # The backfill run's monitor finalizes it and reschedules backfill for
            # the next gap when it completes.
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.debug(f"Vertical: session {sid!r} backfill task errored", exc_info=True)

    def _cancel_backfill(self, sid: str) -> None:
        t = self._backfill.pop(sid, None)
        if t is not None and not t.done():
            t.cancel()
        self._backfill_last.pop(sid, None)

    # ── readiness / stderr draining ─────────────────────────────────────────────

    async def _await_ready(self, sid: str, proc: asyncio.subprocess.Process,
                           start_index: int, min_segments: int) -> bool:
        """Wait for the launch to produce `min_segments` finalized segments.

        Progress-aware rather than a fixed timeout: fail fast if the master dies,
        but otherwise keep waiting as long as segments keep appearing — a slow
        backend (e.g. the Windows TCP relay throttling raw video) can take tens of
        seconds per segment, and a hard cap would spuriously fail a working encode
        and drop it to single-video.  Only a genuine *stall* (no new segment for
        `_READY_STALL_SECS`, with none yet produced) counts as failure.
        """
        stall_deadline = time.time() + _READY_STALL_SECS
        have = 0
        while True:
            # Count segments BEFORE checking the master's exit: a short clip's
            # master exits (terminated at center EOF) right after finalizing its
            # last segment, so "enough segments" must win over "master exited".
            now_have = sum(1 for i in self._produced_indices(sid) if i >= start_index)
            if now_have >= min_segments:
                logger.info(f"Vertical FFmpeg: session {sid!r} ready ({now_have} segment(s) from seg {start_index})")
                return True
            if proc.returncode is not None:
                logger.error(
                    f"Vertical FFmpeg: session {sid!r} master exited (rc={proc.returncode}) with only "
                    f"{now_have}/{min_segments} segment(s)"
                )
                buf = self._stderr.get(sid, [])
                errs = [l for l in buf if not l.startswith(("ffmpeg version", "  built", "  config", "  lib"))]
                if errs:
                    logger.error("Vertical FFmpeg stderr (errors):\n" + "\n".join(errs[-30:]))
                return False
            if now_have > have:
                have = now_have
                stall_deadline = time.time() + _READY_STALL_SECS  # progress → extend
            elif time.time() > stall_deadline:
                logger.error(
                    f"Vertical FFmpeg: session {sid!r} stalled — no new segment in "
                    f"{_READY_STALL_SECS:.0f}s ({have}/{min_segments} from seg {start_index})"
                )
                return False
            await asyncio.sleep(0.5)

    async def _drain_sub_stderr(self, sub: asyncio.subprocess.Process, sid: str, label: str) -> None:
        buf = self._stderr.get(sid)
        fh = self._stderr_fh.get(sid)
        try:
            async for line in _iter_stderr_lines(sub.stderr):
                text = f"[{label}] {line.rstrip()}"
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
            async for line in _iter_stderr_lines(proc.stderr):
                text = line.rstrip()
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

    # ── teardown ────────────────────────────────────────────────────────────────

    async def _kill_procs(self, sid: str) -> None:
        """Terminate the current run's subs + master and close its backend.  Does
        not touch the monitor, dir, log file, or session state."""
        for sub in self._subs.pop(sid, []):
            if sub and sub.returncode is None:
                try: sub.terminate()
                except Exception: pass
                try:
                    await asyncio.wait_for(sub.wait(), timeout=3.0)
                except asyncio.TimeoutError:
                    try: sub.kill()
                    except Exception: pass
        # Close the backend first: with the subs gone, dropping the parent's
        # keepalive (FIFO) / relay master connection (TCP) hands the master EOF,
        # which makes it flush and finalize its last (partial) segment and exit on
        # its own.  We must WAIT for that graceful exit rather than terminate
        # immediately — `proc.terminate()` is a hard TerminateProcess() on Windows
        # (no clean shutdown), so terminating here would drop the final segment
        # (seg{N}.ts.tmp never renamed → a short 2-segment clip loses seg1 and
        # fails readiness).  Only hard-kill if EOF-driven shutdown stalls.
        backend = self._backends.pop(sid, None)
        if backend is not None:
            try: await backend.close()
            except Exception: pass
        proc = self._procs.pop(sid, None)
        if proc and proc.returncode is None:
            try:
                await asyncio.wait_for(proc.wait(), timeout=_MASTER_GRACE_SECS)
            except asyncio.TimeoutError:
                try: proc.terminate()
                except Exception: pass
                try:
                    await asyncio.wait_for(proc.wait(), timeout=5.0)
                except asyncio.TimeoutError:
                    proc.kill()

    async def _teardown_run(self, sid: str, reason: str) -> None:
        """Stop the current run (lock held): cancel the monitor, then kill the
        trio.  Keeps the session dir, cached segments, log file, and state."""
        monitor = self._monitors.pop(sid, None)
        if monitor and not monitor.done():
            monitor.cancel()
            try:
                await asyncio.wait_for(monitor, timeout=2.0)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass
        await self._kill_procs(sid)
        self._log_session(sid, f"----- run end ({reason}) -----")

    async def _reap(self, sid: str, reason: str) -> None:
        """Stage-1 teardown (lock held): reap the FFmpeg processes but keep the
        cached segments.  No-op (beyond marking) if the encode already finished."""
        if sid not in self._dirs or self._reaped.get(sid):
            return
        self._cancel_backfill(sid)
        if self.is_alive(sid):
            await self._teardown_run(sid, reason=reason)
            logger.info(f"Vertical: session {sid!r} stage-1 process reap ({reason}); segments kept on disk")
        else:
            logger.info(f"Vertical: session {sid!r} stage-1 reap no-op — encode already finished ({reason})")
        self._reaped[sid] = True

    async def _destroy(self, sid: str, reason: str) -> None:
        """Stage-2 teardown (lock held): stop the run, delete the temp dir, and
        drop all session state — freeing the concurrency slot."""
        if sid not in self._dirs and sid not in self._procs and sid not in self._backends:
            return
        self._stopped[sid] = True
        self._cancel_backfill(sid)
        await self._teardown_run(sid, reason=reason)

        fh = self._stderr_fh.pop(sid, None)
        if fh is not None:
            try:
                fh.write(f"===== FFmpeg session end ({reason}) =====\n")
                fh.close()
            except Exception:
                pass

        d = self._dirs.pop(sid, None)
        if d:
            shutil.rmtree(d, ignore_errors=True)
        for bag in (self._stderr, self._launch_info, self._last, self._sides,
                    self._side_durs, self._center_id, self._center_dur,
                    self._reaped, self._stopped, self._backfill_last):
            bag.pop(sid, None)
        logger.info(f"Vertical: session {sid!r} stage-2 destroy ({reason})")

    async def _idle_loop(self) -> None:
        while self._dirs:
            await asyncio.sleep(20)
            idle_secs = float(getattr(config, "VERTICAL_IDLE_TIMEOUT", 60))
            ttl_secs = float(getattr(config, "VERTICAL_SESSION_TTL", 1800))
            now = time.time()
            for sid in list(self._dirs.keys()):
                age = now - self._last.get(sid, now)
                if age > ttl_secs:
                    await self.stop(sid, reason=f"session TTL {ttl_secs:.0f}s (stage 2, no fetches)")
                elif age > idle_secs and not self._reaped.get(sid):
                    async with self._lock:
                        await self._reap(sid, reason=f"idle {idle_secs:.0f}s (stage 1, no fetches)")


_vertical_manager = _VerticalSessionManager()
