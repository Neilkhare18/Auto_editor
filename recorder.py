"""
Screen + input recorder (Windows).

Records the primary monitor at 60 fps with ffmpeg (gdigrab) and, at the same
time, logs mouse moves / clicks / scrolls / key presses to <name>.events.json.
The Auto Editor reads that file to drive its camera, zooms and sound effects.

Privacy: key presses are logged only as "a key was pressed" (or "a hotkey
combo"), never which key.
"""
import json
import re
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def screen_size():
    """Primary monitor size in real pixels (also makes the process DPI aware)."""
    if sys.platform.startswith("win"):
        import ctypes
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:  # noqa: BLE001
            try:
                ctypes.windll.user32.SetProcessDPIAware()
            except Exception:  # noqa: BLE001
                pass
        u = ctypes.windll.user32
        return int(u.GetSystemMetrics(0)), int(u.GetSystemMetrics(1))
    return 1920, 1080


def list_mics():
    """DirectShow audio devices, e.g. 'Microphone (Realtek Audio)'."""
    try:
        r = subprocess.run(["ffmpeg", "-hide_banner", "-list_devices", "true",
                            "-f", "dshow", "-i", "dummy"],
                           capture_output=True, text=True, creationflags=NO_WINDOW)
    except OSError:
        return []
    txt = (r.stderr or "") + (r.stdout or "")
    mics, in_audio = [], False
    for line in txt.splitlines():
        if "DirectShow audio devices" in line:
            in_audio = True
            continue
        if "DirectShow video devices" in line:
            in_audio = False
            continue
        m = re.search(r'"([^"]+)"', line)
        if not m or "Alternative name" in line:
            continue
        if "(audio)" in line or (in_audio and "(video)" not in line):
            mics.append(m.group(1))
    return mics


class Recorder:
    def __init__(self, folder, fps=60, mic=None, log=print, on_stop_hotkey=None):
        self.folder = Path(folder)
        self.fps, self.mic, self.log = fps, mic, log
        self.on_stop_hotkey = on_stop_hotkey
        self.proc = None
        self.events = []
        self.t0 = None
        self.video = None
        self._ready = threading.Event()
        self._err = b""
        self._listeners = []
        self._mods = set()
        self._last_move = 0.0

    # ------------------------------------------------------------------ start
    def start(self):
        if not sys.platform.startswith("win"):
            raise RuntimeError("Recording uses Windows screen capture (gdigrab).")
        from pynput import keyboard, mouse  # imported here so the editor works without it

        self.folder.mkdir(parents=True, exist_ok=True)
        self.W, self.H = screen_size()
        w, h = self.W // 2 * 2, self.H // 2 * 2
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.video = self.folder / f"recording_{stamp}.mp4"

        cmd = ["ffmpeg", "-y", "-hide_banner", "-f", "gdigrab", "-framerate", str(self.fps),
               "-draw_mouse", "1", "-offset_x", "0", "-offset_y", "0",
               "-video_size", f"{w}x{h}", "-i", "desktop"]
        if self.mic:
            cmd += ["-f", "dshow", "-i", f"audio={self.mic}"]
        cmd += ["-c:v", "libx264", "-preset", "ultrafast", "-crf", "20", "-pix_fmt", "yuv420p"]
        if self.mic:
            cmd += ["-c:a", "aac", "-b:a", "160k"]
        cmd.append(str(self.video))

        spawn = time.time()
        self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE,
                                     creationflags=NO_WINDOW)
        threading.Thread(target=self._read_stderr, daemon=True).start()
        if not self._ready.wait(10):
            if self.proc.poll() is not None:
                tail = self._err[-600:].decode("utf-8", "replace")
                raise RuntimeError("ffmpeg could not start recording:\n" + tail)
            self.t0 = spawn + 0.5       # fallback if the marker line never showed
        self.log(f"Recording {w}x{h} @ {self.fps} fps -> {self.video.name}")

        self._mouse = mouse.Listener(on_move=self._on_move, on_click=self._on_click,
                                     on_scroll=self._on_scroll)
        self._kb = keyboard.Listener(on_press=self._on_press, on_release=self._on_release)
        self._keyboard = keyboard
        self._listeners = [self._mouse, self._kb]
        for l in self._listeners:
            l.start()

    def _read_stderr(self):
        while True:
            b = self.proc.stderr.read(1)
            if not b:
                return
            self._err = (self._err + b)[-4000:]
            if not self._ready.is_set() and b"Press [q] to stop" in self._err:
                self.t0 = time.time()
                self._ready.set()

    # ----------------------------------------------------------------- events
    def _add(self, typ, x=None, y=None):
        if self.t0 is None:
            return
        e = {"t": round(time.time() - self.t0, 3), "type": typ}
        if x is not None:
            e["x"], e["y"] = int(x), int(y)
        self.events.append(e)

    def _on_move(self, x, y):
        now = time.time()
        if now - self._last_move >= 1 / 60:
            self._last_move = now
            self._add("move", x, y)

    def _on_click(self, x, y, button, pressed):
        if pressed:
            name = getattr(button, "name", "left")
            self._add("right_click" if name == "right" else "left_click", x, y)

    def _on_scroll(self, x, y, dx, dy):
        self._add("scroll", x, y)

    def _is_mod(self, key):
        k = self._keyboard.Key
        return key in (k.ctrl, k.ctrl_l, k.ctrl_r, k.alt, k.alt_l, k.alt_r, k.alt_gr,
                       k.shift, k.shift_l, k.shift_r, k.cmd, k.cmd_l, k.cmd_r)

    def _on_press(self, key):
        k = self._keyboard.Key
        if key == k.f9:
            if self.on_stop_hotkey:
                self.on_stop_hotkey()
            return
        if self._is_mod(key):
            self._mods.add(key)
            return
        held = {m for m in self._mods
                if m in (k.ctrl, k.ctrl_l, k.ctrl_r, k.alt, k.alt_l, k.alt_r, k.cmd,
                         k.cmd_l, k.cmd_r)}
        self._add("hotkey" if held else "key")

    def _on_release(self, key):
        self._mods.discard(key)

    # ------------------------------------------------------------------- stop
    def stop(self):
        for l in self._listeners:
            try:
                l.stop()
            except Exception:  # noqa: BLE001
                pass
        try:
            self.proc.stdin.write(b"q")
            self.proc.stdin.flush()
        except Exception:  # noqa: BLE001
            pass
        try:
            self.proc.wait(30)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        ev_path = self.video.with_suffix(".events.json")
        ev_path.write_text(json.dumps({
            "version": 1, "screen": {"w": self.W, "h": self.H}, "events": self.events,
        }), encoding="utf-8")
        self.log(f"Saved {self.video.name} and {ev_path.name} ({len(self.events)} events)")
        return self.video
