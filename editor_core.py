"""
Auto Editor core - turns a raw screen recording into a punchy edited video.

Pipeline
  1. read  <name>.events.json (cursor / click / key log from the Recorder)
  2. find "dead air" (no clicks, keys or voice) and speed it up
  3. plan a smooth virtual camera: EMA cursor follow + click punch-ins
  4. build the audio: voice + synthetic click/key/whoosh SFX (ducked under voice)
  5. optional captions: faster-whisper -> bold 3-word .ass subtitles
  6. render: frames are piped OpenCV -> ffmpeg (libx264), no big temp files

Works without events.json too: the video is fitted onto the canvas on a blurred
background, dead air is found from motion + audio, captions/SFX still apply.
"""
from __future__ import annotations

import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import wave
from dataclasses import dataclass, fields
from pathlib import Path

# Ensure FFmpeg and user Python scripts are in PATH
user_path = os.environ.get("PATH", "")
extra_paths = [
    str(Path.home() / "AppData" / "Local" / "Microsoft" / "WinGet" / "Packages" / "Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe" / "ffmpeg-9.0.2-full_build" / "bin"),
    str(Path.home() / "AppData" / "Local" / "Packages" / "PythonSoftwareFoundation.Python.3.13_qbz5n2kfra8p0" / "LocalCache" / "local-packages" / "Python313" / "Scripts"),
    r"C:\ffmpeg\bin",
    r"C:\Program Files\ffmpeg\bin",
]
winget_pkgs = Path.home() / "AppData" / "Local" / "Microsoft" / "WinGet" / "Packages"
if winget_pkgs.is_dir():
    try:
        for ffmpeg_bin in winget_pkgs.glob("Gyan.FFmpeg*/**/bin"):
            if ffmpeg_bin.is_dir():
                extra_paths.append(str(ffmpeg_bin))
    except Exception:
        pass

store_pkgs = Path.home() / "AppData" / "Local" / "Packages"
if store_pkgs.is_dir():
    try:
        for py_scripts in store_pkgs.glob("PythonSoftwareFoundation.Python*/**/Scripts"):
            if py_scripts.is_dir():
                extra_paths.append(str(py_scripts))
    except Exception:
        pass

for p in extra_paths:
    if os.path.exists(p) and p not in user_path:
        os.environ["PATH"] = p + os.pathsep + os.environ["PATH"]

import cv2
import numpy as np

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v"}
SR = 44100
ACCENT_ASS = "&H00D4FF&"  # yellow (BGR)

ASPECTS = {
    "9:16 vertical (1080x1920)": (1080, 1920),
    "16:9 landscape (1920x1080)": (1920, 1080),
    "1:1 square (1080x1080)": (1080, 1080),
    "Match source": None,
}


class Cancelled(Exception):
    pass


@dataclass
class Settings:
    aspect: str = "9:16 vertical (1080x1920)"
    fps: int = 30
    follow_cursor: bool = True
    base_zoom: float = 1.15
    click_zoom: float = 1.6
    ema_tau: float = 0.35        # camera smoothing in seconds (bigger = floatier)
    lookahead: float = 0.15      # camera leads the cursor by this much
    speed_ramp: bool = True
    idle_seconds: float = 1.3    # dead air longer than this gets sped up
    idle_speed: float = 3.0
    soft_cuts: bool = True       # smooth de-clicking crossfades & ambient audio
    sfx: bool = True
    sfx_volume: float = 0.8
    captions: bool = False
    whisper_model: str = "base"
    caption_words: int = 3
    sync_offset: float = 0.0     # shift cursor data relative to video (seconds)
    crf: int = 18

    @classmethod
    def from_dict(cls, d):
        names = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in (d or {}).items() if k in names})


# --------------------------------------------------------------------------
# ffmpeg helpers
# --------------------------------------------------------------------------
def _run(cmd):
    return subprocess.run(cmd, capture_output=True, creationflags=NO_WINDOW)


def probe(path):
    r = _run(["ffprobe", "-v", "error", "-print_format", "json",
              "-show_streams", "-show_format", str(path)])
    info = json.loads(r.stdout.decode("utf-8", "replace") or "{}")
    streams = info.get("streams", [])
    v = next((s for s in streams if s.get("codec_type") == "video"), None)
    if v is None:
        raise RuntimeError("No video stream found")
    w, h = int(v["width"]), int(v["height"])
    rot = 0
    try:
        for sd in v.get("side_data_list") or []:
            if "rotation" in sd:
                rot = int(float(sd["rotation"]))
        if "rotate" in (v.get("tags") or {}):
            rot = int(v["tags"]["rotate"])
    except (TypeError, ValueError):
        pass
    if abs(rot) % 180 == 90:
        w, h = h, w
    dur = float((info.get("format") or {}).get("duration") or v.get("duration") or 0)
    if dur <= 0:
        raise RuntimeError("Could not read the video duration")
    return {"w": w, "h": h, "duration": dur,
            "has_audio": any(s.get("codec_type") == "audio" for s in streams)}


def decode_audio(path, rate):
    r = _run(["ffmpeg", "-v", "error", "-i", str(path), "-vn", "-ac", "1",
              "-ar", str(rate), "-f", "f32le", "-"])
    if r.returncode != 0 or not r.stdout:
        return np.zeros(0, np.float32)
    return np.frombuffer(r.stdout, dtype=np.float32).copy()


def even(v):
    return max(2, int(v) // 2 * 2)


# --------------------------------------------------------------------------
# activity analysis
# --------------------------------------------------------------------------
def voice_mask(a16k, n10):
    """Energy and zero-crossing rate based speech activity detection with hysteresis at 10 Hz."""
    mask = np.zeros(n10, bool)
    hop = 1600
    nfr = len(a16k) // hop
    if nfr == 0:
        return mask

    frames = a16k[:nfr * hop].reshape(nfr, hop)
    rms = np.sqrt(np.mean(frames ** 2, axis=1))

    # Zero-crossing rate to retain unvoiced soft consonants (s, sh, f, th, t, k)
    signs = np.sign(frames)
    zcr = np.mean(np.abs(np.diff(signs, axis=1)) > 0, axis=1)

    # Dynamic noise floor estimation
    noise_floor = float(np.percentile(rms, 15))
    thr_high = max(noise_floor * 2.4, 0.006)
    thr_low = max(noise_floor * 1.3, 0.0025)

    # Dual-threshold hysteresis state machine
    act = np.zeros(nfr, bool)
    is_active = False
    hangover_frames = 4  # 400ms hangover to protect natural speech endings and breath
    hangover = 0

    for i in range(nfr):
        is_speech_frame = (rms[i] > thr_high) or (zcr[i] > 0.12 and rms[i] > thr_low * 0.8)
        if is_speech_frame:
            is_active = True
            hangover = hangover_frames
            act[i] = True
        elif is_active:
            if rms[i] > thr_low:
                hangover = hangover_frames
                act[i] = True
            elif hangover > 0:
                hangover -= 1
                act[i] = True
            else:
                is_active = False

    mask[:min(n10, nfr)] = act[:n10]
    return mask


def motion_signal(path, info, cancel):
    """Mean frame-to-frame change at 10 fps (used only when there is no cursor log)."""
    tw = 160
    th = even(round(tw * info["h"] / info["w"]))
    cmd = ["ffmpeg", "-v", "error", "-i", str(path), "-an", "-vf",
           f"fps=10,scale={tw}:{th},format=gray", "-f", "rawvideo", "-"]
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                         creationflags=NO_WINDOW)
    fs, prev, vals = tw * th, None, []
    try:
        while True:
            if cancel():
                raise Cancelled()
            b = p.stdout.read(fs)
            if len(b) < fs:
                break
            f = np.frombuffer(b, np.uint8).astype(np.int16)
            vals.append(0.0 if prev is None else float(np.abs(f - prev).mean()))
            prev = f
    finally:
        p.kill()
    return np.array(vals, np.float32)


def dilate(mask, before, after):
    """Spread every True sample `before` s earlier and `after` s later (10 Hz)."""
    b, a = int(round(before * 10)), int(round(after * 10))
    c = np.convolve(mask.astype(np.int32), np.ones(a + b + 1, np.int32))
    return c[b:b + len(mask)] > 0


# --------------------------------------------------------------------------
# cursor / input events
# --------------------------------------------------------------------------
@dataclass
class Events:
    screen: tuple
    mt: np.ndarray
    mx: np.ndarray
    my: np.ndarray
    clicks: list      # (t, x, y)
    keys: np.ndarray
    hotkeys: np.ndarray
    scrolls: np.ndarray


def find_events(video):
    p = Path(video).with_suffix(".events.json")
    return p if p.exists() else None


def load_events(path, offset=0.0):
    d = json.loads(Path(path).read_text(encoding="utf-8"))
    scr = d.get("screen") or {}
    screen = (float(scr.get("w", 1920)), float(scr.get("h", 1080)))
    pts, clicks, keys, hot, scr_t = [], [], [], [], []
    for e in sorted(d.get("events", []), key=lambda e: e.get("t", 0)):
        t = float(e.get("t", 0)) + offset
        typ = e.get("type", "move")
        x, y = e.get("x"), e.get("y")
        if x is not None and y is not None:
            pts.append((t, float(x), float(y)))
        if typ in ("left_click", "right_click"):
            clicks.append((t, float(x or 0), float(y or 0)))
        elif typ == "key":
            keys.append(t)
        elif typ == "hotkey":
            hot.append(t)
        elif typ == "scroll":
            scr_t.append(t)
    pts.sort()
    arr = np.array(pts, np.float64).reshape(-1, 3)
    return Events(screen, arr[:, 0], arr[:, 1], arr[:, 2], clicks,
                  np.array(keys), np.array(hot), np.array(scr_t))


# --------------------------------------------------------------------------
# timeline (speed ramps)
# --------------------------------------------------------------------------
class Timeline:
    def __init__(self, src, out, speeds):
        self.src, self.out, self.speeds = np.array(src), np.array(out), list(speeds)

    @property
    def out_len(self):
        return float(self.out[-1])

    def to_out(self, t):
        return np.interp(t, self.src, self.out)

    def to_src(self, t):
        return np.interp(t, self.out, self.src)

    def is_fast(self, t):
        k = int(np.searchsorted(self.src, t, side="right")) - 1
        return self.speeds[min(max(k, 0), len(self.speeds) - 1)] > 1.0


def build_timeline(dur, active, st):
    if not st.speed_ramp:
        return Timeline([0, dur], [0, dur], [1.0])
    runs, i, n = [], 0, len(active)
    while i < n:
        if active[i]:
            i += 1
            continue
        j = i
        while j < n and not active[j]:
            j += 1
        s, e = i / 10.0, min(j / 10.0, dur)
        # Give 0.40s cushion on each side of the pause so speed ramps begin and end softly
        cushion = 0.40
        if e - s >= st.idle_seconds and (e - cushion) - (s + cushion) >= 0.4:
            runs.append((s + cushion, e - cushion))
        i = j
    src, out, speeds, cur, o = [0.0], [0.0], [], 0.0, 0.0
    for fs, fe in runs:
        if fs > cur:
            speeds.append(1.0)
            o += fs - cur
            src.append(fs)
            out.append(o)
        speeds.append(st.idle_speed)
        o += (fe - fs) / st.idle_speed
        src.append(fe)
        out.append(o)
        cur = fe
    if dur > cur:
        speeds.append(1.0)
        o += dur - cur
        src.append(dur)
        out.append(o)
    return Timeline(src, out, speeds)


# --------------------------------------------------------------------------
# virtual camera
# --------------------------------------------------------------------------
def smoothstep(x):
    """Quintic C2-smoothstep curve (continuous acceleration, zero jerk at endpoints)."""
    x = np.clip(x, 0.0, 1.0)
    return x * x * x * (x * (x * 6 - 15) + 10)


def click_envelope(t, click_times, lead=0.25, ramp=0.35, hold=0.85, out=0.5):
    env = np.zeros_like(t)
    for tc in click_times:
        start = tc - lead
        up_end = start + ramp
        hold_end = up_end + hold
        end = hold_end + out
        lo, hi = np.searchsorted(t, start), np.searchsorted(t, end)
        if hi <= lo:
            continue
        seg = t[lo:hi]
        e = np.where(seg < up_end, smoothstep((seg - start) / ramp),
                     np.where(seg < hold_end, 1.0, smoothstep(1 - (seg - hold_end) / out)))
        env[lo:hi] = np.maximum(env[lo:hi], e)
    return env


def soft_boundary(val, min_val, max_val, margin):
    """Soft deceleration curve as camera approaches screen boundaries."""
    low_zone = min_val + margin
    high_zone = max_val - margin
    res = val.copy()
    mask_low = res < low_zone
    if np.any(mask_low):
        d = (low_zone - res[mask_low]) / max(margin, 1.0)
        res[mask_low] = low_zone - margin * np.tanh(d)
    mask_high = res > high_zone
    if np.any(mask_high):
        d = (res[mask_high] - high_zone) / max(margin, 1.0)
        res[mask_high] = high_zone + margin * np.tanh(d)
    return np.clip(res, min_val, max_val)


def plan_camera(t_src, ev, st, W, H, outW, outH, fps):
    sx, sy = W / ev.screen[0], H / ev.screen[1]
    raw_tx = np.interp(t_src + st.lookahead, ev.mt, ev.mx) * sx
    raw_ty = np.interp(t_src + st.lookahead, ev.mt, ev.my) * sy

    # Deadzone filter to suppress micro-tremor and sensor noise
    tx = np.empty_like(raw_tx)
    ty = np.empty_like(raw_ty)
    cur_x, cur_y = raw_tx[0], raw_ty[0]
    deadzone_sq = 16.0 ** 2
    for i in range(len(raw_tx)):
        dx = raw_tx[i] - cur_x
        dy = raw_ty[i] - cur_y
        if dx * dx + dy * dy > deadzone_sq:
            cur_x += dx * 0.4
            cur_y += dy * 0.4
        tx[i], ty[i] = cur_x, cur_y

    env = click_envelope(t_src, [c[0] for c in ev.clicks])
    base = max(1.0, st.base_zoom)
    z = base + (max(base, st.click_zoom) - base) * env
    a = outW / outH
    bw, bh = (H * a, H) if W / H > a else (W, W / a)
    cw, ch = bw / z, bh / z

    # 2nd-order critically damped filter (smooth operator tracking)
    omega = 2.0 / max(st.ema_tau, 0.08)
    dt = 1.0 / fps
    cx = np.empty(len(t_src), dtype=np.float64)
    cy = np.empty(len(t_src), dtype=np.float64)
    pos_x, pos_y = float(tx[0]), float(ty[0])
    vel_x, vel_y = 0.0, 0.0

    for i in range(len(t_src)):
        target_x, target_y = float(tx[i]), float(ty[i])
        acc_x = (omega * omega) * (target_x - pos_x) - (2.0 * omega) * vel_x
        acc_y = (omega * omega) * (target_y - pos_y) - (2.0 * omega) * vel_y
        vel_x += acc_x * dt
        vel_y += acc_y * dt
        pos_x += vel_x * dt
        pos_y += vel_y * dt
        cx[i] = pos_x
        cy[i] = pos_y

    margin_x = float(cw.mean() * 0.10)
    margin_y = float(ch.mean() * 0.10)
    cx = soft_boundary(cx, cw / 2, W - cw / 2, margin_x)
    cy = soft_boundary(cy, ch / 2, H - ch / 2, margin_y)
    return cx, cy, cw, ch


def whoosh_times(cx, cy, cw, fps, thr=1.0, gap=1.0):
    if len(cx) < 2:
        return []
    v = np.hypot(np.diff(cx), np.diff(cy)) * fps / cw[1:]
    times, last = [], -9.0
    for i in np.flatnonzero(v > thr):
        t = i / fps
        if t - last >= gap:
            times.append(t)
            last = t
    return times


# --------------------------------------------------------------------------
# frame compositing
# --------------------------------------------------------------------------
def frame_camera(frame, cx, cy, cw, ch, outW, outH):
    s = outW / cw
    if s < 0.8:  # big source, shrinking: integer crop + area filter looks cleaner
        x0, y0 = int(round(cx - cw / 2)), int(round(cy - ch / 2))
        x1, y1 = x0 + int(round(cw)), y0 + int(round(ch))
        H, W = frame.shape[:2]
        x0, y0, x1, y1 = max(0, x0), max(0, y0), min(W, x1), min(H, y1)
        return cv2.resize(frame[y0:y1, x0:x1], (outW, outH), interpolation=cv2.INTER_AREA)
    m = np.float32([[s, 0, -(cx - cw / 2) * s], [0, s, -(cy - ch / 2) * s]])
    return cv2.warpAffine(frame, m, (outW, outH), flags=cv2.INTER_CUBIC,
                          borderMode=cv2.BORDER_REFLECT_101)


def frame_fit(frame, outW, outH):
    H, W = frame.shape[:2]
    sc = min(outW / W, outH / H)
    nw, nh = min(outW, int(round(W * sc))), min(outH, int(round(H * sc)))
    fg = cv2.resize(frame, (nw, nh),
                    interpolation=cv2.INTER_AREA if sc < 1 else cv2.INTER_CUBIC)
    if nw == outW and nh == outH:
        return fg
    a = outW / outH
    cw, ch = (H * a, H) if W / H > a else (W, W / a)
    x0, y0 = int((W - cw) / 2), int((H - ch) / 2)
    crop = frame[y0:y0 + int(ch), x0:x0 + int(cw)]
    small = cv2.resize(crop, (max(2, outW // 10), max(2, outH // 10)),
                       interpolation=cv2.INTER_AREA)
    small = cv2.GaussianBlur(small, (0, 0), 4)
    bg = cv2.resize(small, (outW, outH), interpolation=cv2.INTER_LINEAR)
    bg = (bg * 0.40).astype(np.uint8)
    ox, oy = (outW - nw) // 2, (outH - nh) // 2
    bg[oy:oy + nh, ox:ox + nw] = fg
    return bg


# --------------------------------------------------------------------------
# sound effects (synthetic, replaceable by your own .wav files)
# --------------------------------------------------------------------------
def synth_click():
    n = int(SR * 0.05)
    t = np.arange(n) / SR
    rng = np.random.default_rng(1)
    s = np.sin(2 * np.pi * 1800 * t) * np.exp(-t * 120) * 0.6 \
        + rng.standard_normal(n) * np.exp(-t * 220) * 0.25
    return s.astype(np.float32)


def synth_key():
    n = int(SR * 0.035)
    t = np.arange(n) / SR
    rng = np.random.default_rng(2)
    s = np.sin(2 * np.pi * 700 * t) * np.exp(-t * 160) * 0.5 \
        + rng.standard_normal(n) * np.exp(-t * 300) * 0.2
    return s.astype(np.float32)


def synth_whoosh():
    dur = 0.55
    n = int(SR * dur)
    t = np.arange(n) / SR
    noise = np.random.default_rng(3).standard_normal(n)

    def box(x, k):
        return np.convolve(x, np.ones(k) / k, mode="same")
    p = t / dur
    s = (box(noise, 64) * (1 - p) + box(noise, 12) * p) * np.sin(np.pi * p) ** 2
    return (s / (np.abs(s).max() + 1e-9) * 0.7).astype(np.float32)


def write_wav(path, a):
    a16 = (np.clip(a, -1, 1) * 32767).astype(np.int16)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(a16.tobytes())


def load_wav(path):
    with wave.open(str(path), "rb") as w:
        ch, sw, rate, n = w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes()
        raw = w.readframes(n)
    if sw != 2:
        raise ValueError("only 16-bit wav supported")
    a = np.frombuffer(raw, np.int16).astype(np.float32) / 32768
    if ch > 1:
        a = a.reshape(-1, ch).mean(axis=1)
    if rate != SR:
        a = np.interp(np.linspace(0, len(a) - 1, int(len(a) * SR / rate)),
                      np.arange(len(a)), a).astype(np.float32)
    return a


def load_sfx(sfx_dir):
    sfx_dir = Path(sfx_dir)
    sfx_dir.mkdir(parents=True, exist_ok=True)
    out = {}
    for name, fn in (("click", synth_click), ("keystroke", synth_key), ("whoosh", synth_whoosh)):
        p = sfx_dir / f"{name}.wav"
        if not p.exists():
            write_wav(p, fn())
        try:
            out[name] = load_wav(p)
        except Exception:  # noqa: BLE001
            out[name] = fn()
    return out


def soft_limiter(x, threshold=0.90):
    """Soft-knee saturation limiter ensuring no clipping while preserving natural voice dynamics."""
    max_val = float(np.max(np.abs(x))) if len(x) else 0.0
    if max_val <= threshold:
        return x
    abs_x = np.abs(x)
    sign = np.sign(x)
    over = abs_x > threshold
    ratio = (abs_x - threshold) / max(max_val - threshold, 1e-6)
    compressed = np.where(over, threshold + (0.98 - threshold) * np.tanh(ratio * 1.5), abs_x)
    return (sign * compressed).astype(np.float32)


def soft_fade_chunk(chunk, fade_len):
    """Raised cosine fade in/out on audio chunk to eliminate digital click/pop artifacts."""
    if len(chunk) <= fade_len * 2:
        fl = len(chunk) // 2
        if fl < 2:
            return chunk
        w_in = 0.5 * (1 - np.cos(np.linspace(0, np.pi, fl, dtype=np.float32)))
        w_out = 0.5 * (1 + np.cos(np.linspace(0, np.pi, fl, dtype=np.float32)))
        chunk = chunk.copy()
        chunk[:fl] *= w_in
        chunk[-fl:] *= w_out
        return chunk
    chunk = chunk.copy()
    w_in = 0.5 * (1 - np.cos(np.linspace(0, np.pi, fade_len, dtype=np.float32)))
    w_out = 0.5 * (1 + np.cos(np.linspace(0, np.pi, fade_len, dtype=np.float32)))
    chunk[:fade_len] *= w_in
    chunk[-fade_len:] *= w_out
    return chunk


def build_audio(path, info, tl, ev, voice10, st, whooshes, sfx_dir, t_src_of_out, n_out, fps):
    total = int(round(n_out / fps * SR))
    base = np.zeros(total, np.float32)
    fade_len = int(SR * 0.025)  # 25ms raised-cosine de-click window

    if info["has_audio"]:
        a = decode_audio(path, SR)
        ambient_scale = 0.01
        if len(a) > SR:
            p10 = float(np.percentile(np.abs(a[:min(len(a), SR * 30)]), 10))
            ambient_scale = min(0.04, max(0.004, p10))

        for k, sp in enumerate(tl.speeds):
            s0 = int(round(tl.src[k] * SR))
            o0 = int(round(tl.out[k] * SR))
            n = int(round((tl.out[k + 1] - tl.out[k]) * SR))
            if sp == 1.0:
                chunk = a[s0:s0 + n]
                chunk = chunk[:max(0, total - o0)]
                if len(chunk) > 0:
                    chunk = soft_fade_chunk(chunk, fade_len)
                    base[o0:o0 + len(chunk)] = chunk
            elif getattr(st, "soft_cuts", True):
                # Gentle ambient presence during speed ramp to eliminate dead vacuum silence
                out_chunk_len = min(n, max(0, total - o0))
                if out_chunk_len > 0 and len(a) > 0:
                    subtle_ambient = np.random.default_rng(k).normal(0, ambient_scale * 0.25, out_chunk_len).astype(np.float32)
                    subtle_ambient = soft_fade_chunk(subtle_ambient, fade_len)
                    base[o0:o0 + out_chunk_len] = subtle_ambient

    if not st.sfx:
        return soft_limiter(base)

    sfx = load_sfx(sfx_dir)
    vol = float(st.sfx_volume) * 0.55
    mix = np.zeros(total, np.float32)

    def place(wave_, t_out, t_src):
        v_idx = min(len(voice10) - 1, max(0, int(t_src * 10)))
        duck = 0.35 if voice10[v_idx] else 1.0
        i = int(t_out * SR)
        if 0 <= i < total:
            seg = wave_[:total - i]
            mix[i:i + len(seg)] += seg * vol * duck

    def mapped(times, gap, name):
        last = -9.0
        for t in times:
            if t - last < gap or t < 0 or t > tl.src[-1] or tl.is_fast(t):
                continue
            last = t
            place(sfx[name], float(tl.to_out(t)), t)

    if ev is not None:
        mapped([c[0] for c in ev.clicks], 0.06, "click")
        mapped(np.sort(np.concatenate([ev.keys, ev.hotkeys])), 0.08, "keystroke")
    for t_out in whooshes:
        i = min(len(t_src_of_out) - 1, int(t_out * fps))
        place(sfx["whoosh"], t_out, float(t_src_of_out[i]))

    out = base + mix
    return soft_limiter(out)


# --------------------------------------------------------------------------
# captions
# --------------------------------------------------------------------------
def transcribe_words(a16, model_name, log):
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        log("Installing faster-whisper (one time)...")
        subprocess.run([sys.executable, "-m", "pip", "install", "faster-whisper"],
                       capture_output=True, creationflags=NO_WINDOW)
        from faster_whisper import WhisperModel
    log(f"Transcribing with Whisper '{model_name}' (first run downloads the model)...")
    model = WhisperModel(model_name, compute_type="int8")
    segs, _ = model.transcribe(a16, word_timestamps=True, vad_filter=True)
    words = []
    for s in segs:
        for w in (s.words or []):
            txt = re.sub(r"[.,!?;:\"“”]", "", w.word).strip().upper()
            if txt:
                words.append((float(w.start), float(w.end), txt))
    return words


def _ass_time(t):
    t = max(0.0, t)
    cs = int(round(t * 100))
    h, cs = divmod(cs, 360000)
    m, cs = divmod(cs, 6000)
    s, cs = divmod(cs, 100)
    return f"{h}:{m:02}:{s:02}.{cs:02}"


def build_ass(words_out, outW, outH, n=3):
    is_vertical = outH > outW
    font_size = max(26, min(round(min(outW, outH) * 0.052), 52 if is_vertical else 46))
    margin_v = max(60, round(outH * 0.13)) if is_vertical else max(40, round(outH * 0.08))
    margin_h = max(40, round(outW * 0.06))
    outline = max(3, round(font_size * 0.09))
    shadow = max(1, round(font_size * 0.03))

    head = (
        "[Script Info]\nScriptType: v4.00+\n"
        f"PlayResX: {outW}\nPlayResY: {outH}\nWrapStyle: 0\n\n"
        "[V4+ Styles]\n"
        "Format: Name,Fontname,Fontsize,PrimaryColour,SecondaryColour,OutlineColour,"
        "BackColour,Bold,Italic,Underline,StrikeOut,ScaleX,ScaleY,Spacing,Angle,"
        "BorderStyle,Outline,Shadow,Alignment,MarginL,MarginR,MarginV,Encoding\n"
        f"Style: Default,Arial,{font_size},&H00FFFFFF,&H000000FF,&H00000000,&H80000000,"
        f"-1,0,0,0,100,100,0,0,1,{outline},{shadow},2,{margin_h},{margin_h},{margin_v},1\n\n"
        "[Events]\nFormat: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,Text\n")
    lines = []
    chunks = [words_out[i:i + n] for i in range(0, len(words_out), n)]
    for ci, ch in enumerate(chunks):
        nxt = chunks[ci + 1][0][0] if ci + 1 < len(chunks) else ch[-1][1] + 0.5
        for wi, (s, e, _) in enumerate(ch):
            end = ch[wi + 1][0] if wi + 1 < len(ch) else min(ch[-1][1] + 0.25, nxt)
            end = max(end, s + 0.05)
            text = " ".join(
                (f"{{\\c{ACCENT_ASS}}}{w}{{\\c&HFFFFFF&}}" if j == wi else w)
                for j, (_, _, w) in enumerate(ch))
            lines.append(f"Dialogue: 0,{_ass_time(s)},{_ass_time(end)},Default,,0,0,0,,{text}")
    return head + "\n".join(lines) + "\n"


# --------------------------------------------------------------------------
# rendering
# --------------------------------------------------------------------------
def _read_exact(stream, buf):
    mv, got = memoryview(buf), 0
    while got < len(buf):
        r = stream.readinto(mv[got:])
        if not r:
            return False
        got += r
    return True


def edit_video(src, st, log=print, progress=lambda f, t: None,
               cancel=lambda: False, sfx_dir=None):
    src = Path(src)
    sfx_dir = sfx_dir or (Path(__file__).parent / "sfx")
    final = src.with_name(src.stem + "_edited.mp4")
    partial = src.with_name(src.stem + "_edited.partial.mp4")
    tmp = Path(tempfile.mkdtemp(prefix="autoedit_"))
    try:
        info = probe(src)
        dur = info["duration"]
        W0, H0 = info["w"], info["h"]
        sc = min(1.0, 2560 / max(W0, H0))
        W, H = even(W0 * sc), even(H0 * sc)           # decoded size
        size = ASPECTS.get(st.aspect)
        outW, outH = size if size else (even(W0), even(H0))
        fps = int(st.fps)
        n10 = int(math.ceil(dur * 10)) + 1

        progress(0.01, "Reading input")
        ev_path = find_events(src)
        ev = load_events(ev_path, st.sync_offset) if ev_path else None
        has_ptr = ev is not None and len(ev.mt) > 0
        use_camera = bool(st.follow_cursor and has_ptr)
        log(f"Cursor data: {'found' if ev else 'none'}  |  "
            f"{'smart camera' if use_camera else 'fit on blurred background'}")

        # ---- voice + activity -------------------------------------------------
        a16 = decode_audio(src, 16000) if info["has_audio"] else np.zeros(0, np.float32)
        voice = voice_mask(a16, n10)
        active = dilate(voice, 0.35, 0.45)
        if ev is not None:
            def mark(times, pre, post):
                m = np.zeros(n10, bool)
                for t in times:
                    k = int(t * 10)
                    if 0 <= k < n10:
                        m[k] = True
                return dilate(m, pre, post)
            active |= mark([c[0] for c in ev.clicks], 0.6, 1.4)
            active |= mark(np.concatenate([ev.keys, ev.hotkeys, ev.scrolls]), 0.35, 1.1)
        elif st.speed_ramp:
            progress(0.02, "Analysing motion")
            mo = motion_signal(src, info, cancel)
            m = np.zeros(n10, bool)
            m[:min(n10, len(mo))] = mo[:n10] > 0.8
            active |= dilate(m, 0.35, 0.35)

        tl = build_timeline(dur, active, st)
        n_out = max(1, int(tl.out_len * fps))
        t_out = np.arange(n_out) / fps
        t_src = tl.to_src(t_out)
        log(f"Length {dur:.1f}s -> {tl.out_len:.1f}s "
            f"({dur - tl.out_len:.1f}s of dead air removed)")
        progress(0.08, "Planning camera")

        # ---- camera -------------------------------------------------------------
        cam = None
        whooshes = []
        if use_camera:
            cam = plan_camera(t_src, ev, st, W, H, outW, outH, fps)
            if st.sfx:
                whooshes = whoosh_times(cam[0], cam[1], cam[2], fps)

        # ---- audio --------------------------------------------------------------
        progress(0.12, "Building audio")
        audio = build_audio(src, info, tl, ev, voice, st, whooshes, sfx_dir, t_src, n_out, fps)
        wav_path = tmp / "audio.wav"
        write_wav(wav_path, audio)

        # ---- captions -----------------------------------------------------------
        vf = []
        if st.captions and a16.size:
            progress(0.15, "Transcribing")
            try:
                words = transcribe_words(a16, st.whisper_model, log)
                words_out = [(float(tl.to_out(s)), float(tl.to_out(e)), w)
                             for s, e, w in words if not tl.is_fast(s)]
                if words_out:
                    (tmp / "captions.ass").write_text(
                        build_ass(words_out, outW, outH, st.caption_words), encoding="utf-8")
                    vf = ["-vf", "ass=captions.ass"]
                    log(f"Captions: {len(words_out)} words")
                else:
                    log("No speech found, captions skipped")
            except Cancelled:
                raise
            except Exception as e:  # noqa: BLE001
                log(f"Captions skipped: {e}")
        elif st.captions:
            log("Captions skipped: the video has no audio")

        # ---- render ---------------------------------------------------------------
        dec = subprocess.Popen(
            ["ffmpeg", "-v", "error", "-i", str(src), "-an", "-vf",
             f"fps={fps},scale={W}:{H}", "-f", "rawvideo", "-pix_fmt", "bgr24", "-"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, creationflags=NO_WINDOW,
            bufsize=W * H * 3 * 2)
        errlog = open(tmp / "enc.log", "wb")
        enc = subprocess.Popen(
            ["ffmpeg", "-y", "-v", "error", "-f", "rawvideo", "-pix_fmt", "bgr24",
             "-s", f"{outW}x{outH}", "-r", str(fps), "-i", "-", "-i", str(wav_path),
             *vf, "-c:v", "libx264", "-preset", "veryfast", "-crf", str(st.crf),
             "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k", "-shortest",
             "-movflags", "+faststart", str(partial)],
            stdin=subprocess.PIPE, stderr=errlog, cwd=str(tmp), creationflags=NO_WINDOW)

        buf = np.empty(W * H * 3, np.uint8)
        frame, cur_k = None, -1
        base_p = 0.2 if st.captions else 0.15
        try:
            for i in range(n_out):
                if cancel():
                    raise Cancelled()
                k = int(round(t_src[i] * fps))
                while cur_k < k:
                    if not _read_exact(dec.stdout, buf):
                        break
                    cur_k += 1
                    frame = buf.reshape(H, W, 3)
                if frame is None:
                    break
                if cam is not None:
                    out = frame_camera(frame, cam[0][i], cam[1][i], cam[2][i], cam[3][i],
                                       outW, outH)
                elif (W, H) == (outW, outH):
                    out = frame
                else:
                    out = frame_fit(frame, outW, outH)
                enc.stdin.write(np.ascontiguousarray(out).tobytes())
                if i % 10 == 0:
                    progress(base_p + (1 - base_p) * i / n_out,
                             f"Rendering {i}/{n_out} frames")
            enc.stdin.close()
            enc.wait()
        except BaseException:
            enc.kill()
            raise
        finally:
            dec.kill()
            errlog.close()
        if enc.returncode != 0:
            tail = (tmp / "enc.log").read_bytes()[-800:].decode("utf-8", "replace")
            raise RuntimeError(f"ffmpeg failed: {tail}")
        os.replace(partial, final)
        progress(1.0, "Done")
        return final
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        if partial.exists():
            try:
                partial.unlink()
            except OSError:
                pass
