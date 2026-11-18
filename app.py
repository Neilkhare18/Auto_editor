"""Auto Editor - GUI. Run:  python app.py"""
import json
import os
import queue
import subprocess
import sys
import threading
import time
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from shutil import which

import customtkinter as ctk
import tkinter as tk
from tkinter import filedialog, messagebox

import editor_core as core

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

APP_NAME = "Auto Editor"
CONFIG_PATH = Path.home() / ".autoeditor.json"
ACCENT, BG, CARD, CARD2 = "#FFD400", "#0d0e11", "#17181d", "#1f2026"
MUTED, RED, GREEN, AMBER = "#8b8f98", "#ff4d4f", "#3ddc84", "#f5a623"


def open_path(p):
    p = str(p)
    if sys.platform.startswith("win"):
        os.startfile(p)  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.Popen(["open", p])
    else:
        subprocess.Popen(["xdg-open", p])


class App(ctk.CTk):
    def __init__(self):
        ctk.set_appearance_mode("dark")
        super().__init__(fg_color=BG)
        self.title(APP_NAME)
        self.geometry("1100x780")
        self.minsize(980, 700)

        cfg = {}
        if CONFIG_PATH.exists():
            try:
                cfg = json.loads(CONFIG_PATH.read_text())
            except (OSError, ValueError):
                pass
        self.cfg = cfg
        self.st0 = core.Settings.from_dict(cfg.get("settings"))

        self.q = queue.Queue()          # worker -> UI
        self.jobs = queue.Queue()       # UI -> worker
        self.cancel = threading.Event()
        self.jobstate = {}                 # filename -> status text
        self.row_widgets = {}
        self.recorder = None
        self.rec_started = 0.0
        self.sizes = {}

        self.columnconfigure(1, weight=1)
        self.rowconfigure(0, weight=1)
        self._sidebar()
        self._main()
        threading.Thread(target=self._worker, daemon=True).start()
        self._rescan()
        self.after(200, self._pump)
        self.after(3000, self._watch_tick)
        self.protocol("WM_DELETE_WINDOW", self._close)

    # =============================================================== sidebar
    def _head(self, parent, text):
        ctk.CTkLabel(parent, text=text, text_color=MUTED,
                     font=ctk.CTkFont(size=11, weight="bold")).pack(anchor="w", padx=6, pady=(14, 2))

    def _slider(self, parent, text, lo, hi, steps, value, fmt):
        var = tk.DoubleVar(value=value)
        row = ctk.CTkFrame(parent, fg_color="transparent")
        row.pack(fill="x", padx=6, pady=(4, 0))
        ctk.CTkLabel(row, text=text, font=ctk.CTkFont(size=12)).pack(side="left")
        val = ctk.CTkLabel(row, text=fmt(value), text_color=ACCENT,
                           font=ctk.CTkFont(size=12, weight="bold"))
        val.pack(side="right")
        ctk.CTkSlider(parent, from_=lo, to=hi, number_of_steps=steps, variable=var,
                      button_color=ACCENT, button_hover_color="#ffe14d", progress_color=ACCENT,
                      command=lambda v: val.configure(text=fmt(v))).pack(fill="x", padx=6, pady=(2, 2))
        return var

    def _switch(self, parent, text, value):
        var = tk.BooleanVar(value=value)
        ctk.CTkSwitch(parent, text=text, variable=var, progress_color=ACCENT,
                      font=ctk.CTkFont(size=12)).pack(anchor="w", padx=6, pady=(8, 0))
        return var

    def _sidebar(self):
        s = self.st0
        outer = ctk.CTkFrame(self, width=330, corner_radius=0, fg_color=CARD)
        outer.grid(row=0, column=0, sticky="nsew")
        outer.grid_propagate(False)
        ctk.CTkLabel(outer, text="AUTO EDITOR", text_color=ACCENT,
                     font=ctk.CTkFont(size=22, weight="bold")).pack(anchor="w", padx=18, pady=(22, 0))
        ctk.CTkLabel(outer, text="Zooms, speed ramps, SFX and captions - automatic",
                     text_color=MUTED, font=ctk.CTkFont(size=12)).pack(anchor="w", padx=18)
        sb = ctk.CTkScrollableFrame(outer, fg_color="transparent")
        sb.pack(fill="both", expand=True, padx=8, pady=(4, 8))

        self._head(sb, "OUTPUT")
        self.v_aspect = tk.StringVar(value=s.aspect)
        ctk.CTkOptionMenu(sb, variable=self.v_aspect, values=list(core.ASPECTS), height=32,
                          fg_color=CARD2, button_color=CARD2,
                          button_hover_color="#2a2c34").pack(fill="x", padx=6)
        self.v_fps = tk.StringVar(value=str(s.fps))
        ctk.CTkSegmentedButton(sb, values=["30", "60"], variable=self.v_fps,
                               selected_color=ACCENT, selected_hover_color="#ffe14d",
                               unselected_color=CARD2, text_color="#000000").pack(fill="x", padx=6, pady=(8, 0))

        self._head(sb, "CAMERA  (needs cursor data from the Recorder)")
        self.v_follow = self._switch(sb, "Follow cursor + click punch-ins", s.follow_cursor)
        self.v_base = self._slider(sb, "Base zoom", 1.0, 2.0, 20, s.base_zoom, lambda v: f"{float(v):.2f}x")
        self.v_click = self._slider(sb, "Click zoom", 1.0, 3.0, 40, s.click_zoom, lambda v: f"{float(v):.2f}x")
        self.v_tau = self._slider(sb, "Smoothness", 0.05, 1.0, 19, s.ema_tau, lambda v: f"{float(v):.2f}s")

        self._head(sb, "DEAD AIR")
        self.v_ramp = self._switch(sb, "Speed up dead air", s.speed_ramp)
        self.v_soft = self._switch(sb, "Soft cuts & de-clicking", s.soft_cuts)
        self.v_idle = self._slider(sb, "Idle longer than", 0.6, 4.0, 34, s.idle_seconds, lambda v: f"{float(v):.1f}s")
        self.v_speed = self._slider(sb, "Speed", 1.5, 8.0, 13, s.idle_speed, lambda v: f"{float(v):.1f}x")

        self._head(sb, "SOUND")
        self.v_sfx = self._switch(sb, "Click / key / whoosh SFX", s.sfx)
        self.v_sfxvol = self._slider(sb, "SFX volume", 0.0, 1.5, 15, s.sfx_volume, lambda v: f"{float(v):.1f}")

        self._head(sb, "CAPTIONS")
        self.v_caps = self._switch(sb, "Auto captions (Whisper)", s.captions)
        self.v_model = tk.StringVar(value=s.whisper_model)
        ctk.CTkOptionMenu(sb, variable=self.v_model, values=["tiny", "base", "small"], height=30,
                          fg_color=CARD2, button_color=CARD2,
                          button_hover_color="#2a2c34").pack(fill="x", padx=6, pady=(8, 0))
        self.v_words = self._slider(sb, "Words per caption", 1, 6, 5, s.caption_words, lambda v: f"{int(float(v))}")

        self._head(sb, "ADVANCED")
        self.v_sync = self._slider(sb, "Cursor sync offset", -1.0, 1.0, 40, s.sync_offset, lambda v: f"{float(v):+.2f}s")
        self.v_crf = self._slider(sb, "Quality (lower = better, bigger)", 14, 28, 14, s.crf, lambda v: f"CRF {int(float(v))}")

    def _settings(self):
        return core.Settings(
            aspect=self.v_aspect.get(), fps=int(self.v_fps.get()),
            follow_cursor=bool(self.v_follow.get()), base_zoom=round(float(self.v_base.get()), 2),
            click_zoom=round(float(self.v_click.get()), 2), ema_tau=round(float(self.v_tau.get()), 2),
            speed_ramp=bool(self.v_ramp.get()), idle_seconds=round(float(self.v_idle.get()), 1),
            idle_speed=round(float(self.v_speed.get()), 1), soft_cuts=bool(self.v_soft.get()),
            sfx=bool(self.v_sfx.get()), sfx_volume=round(float(self.v_sfxvol.get()), 2),
            captions=bool(self.v_caps.get()), whisper_model=self.v_model.get(),
            caption_words=int(float(self.v_words.get())), sync_offset=round(float(self.v_sync.get()), 2),
            crf=int(float(self.v_crf.get())))

    # ================================================================== main
    def _main(self):
        main = ctk.CTkFrame(self, fg_color="transparent")
        main.grid(row=0, column=1, sticky="nsew", padx=16, pady=16)
        main.rowconfigure(1, weight=1)
        main.columnconfigure(0, weight=1)

        top = ctk.CTkFrame(main, fg_color=CARD, corner_radius=14)
        top.grid(row=0, column=0, sticky="ew")
        top.columnconfigure(1, weight=1)
        ctk.CTkLabel(top, text="VIDEO FOLDER", text_color=MUTED,
                     font=ctk.CTkFont(size=11, weight="bold")).grid(row=0, column=0, padx=16, pady=(12, 0), sticky="w")
        self.v_folder = tk.StringVar(value=self.cfg.get("folder", str(Path.home() / "Videos")))
        ctk.CTkEntry(top, textvariable=self.v_folder, height=36, fg_color=CARD2,
                     border_width=0).grid(row=1, column=0, columnspan=2, sticky="ew", padx=(16, 6), pady=(2, 14))
        bf = ctk.CTkFrame(top, fg_color="transparent")
        bf.grid(row=1, column=2, padx=(0, 16), pady=(2, 14))
        for text, cmd in (("Browse", self._browse), ("Open", lambda: open_path(self.v_folder.get())),
                          ("Rescan", self._rescan)):
            ctk.CTkButton(bf, text=text, width=70, height=36, fg_color=CARD2, hover_color="#2a2c34",
                          command=cmd).pack(side="left", padx=3)

        self.tabs = ctk.CTkTabview(main, fg_color=CARD, corner_radius=14, segmented_button_selected_color=ACCENT,
                                   segmented_button_selected_hover_color="#ffe14d",
                                   segmented_button_unselected_color=CARD2, text_color="#000000")
        self.tabs.grid(row=1, column=0, sticky="nsew", pady=12)
        self._edit_tab(self.tabs.add("Edit"))
        self._record_tab(self.tabs.add("Record"))

        self.log = ctk.CTkTextbox(main, height=100, fg_color=CARD, corner_radius=14,
                                  font=ctk.CTkFont(family="Consolas", size=11), text_color=MUTED)
        self.log.grid(row=2, column=0, sticky="ew")
        self.log.configure(state="disabled")

    # ---------------------------------------------------------------- edit tab
    def _edit_tab(self, tab):
        tab.columnconfigure(0, weight=1)
        tab.rowconfigure(2, weight=1)
        bar = ctk.CTkFrame(tab, fg_color="transparent")
        bar.grid(row=0, column=0, sticky="ew", pady=(4, 8))
        self.btn_all = ctk.CTkButton(bar, text="EDIT ALL NEW VIDEOS", height=42, fg_color=ACCENT,
                                     hover_color="#ffe14d", text_color="#000000",
                                     font=ctk.CTkFont(size=14, weight="bold"), command=self._edit_all)
        self.btn_all.pack(side="left")
        self.btn_cancel = ctk.CTkButton(bar, text="Cancel", width=80, height=42, fg_color=RED,
                                        hover_color="#ff7375", command=self.cancel.set)
        self.v_watch = tk.BooleanVar(value=bool(self.cfg.get("watch", False)))
        ctk.CTkSwitch(bar, text="Watch folder (auto-edit new videos)", variable=self.v_watch,
                      progress_color=ACCENT, font=ctk.CTkFont(size=12)).pack(side="right")

        pf = ctk.CTkFrame(tab, fg_color="transparent")
        pf.grid(row=1, column=0, sticky="ew")
        pf.columnconfigure(0, weight=1)
        self.prog_lbl = ctk.CTkLabel(pf, text="Idle", text_color=MUTED, anchor="w")
        self.prog_lbl.grid(row=0, column=0, sticky="w")
        self.prog = ctk.CTkProgressBar(pf, progress_color=ACCENT, fg_color=CARD2, height=8)
        self.prog.set(0)
        self.prog.grid(row=1, column=0, sticky="ew", pady=(2, 8))

        self.list = ctk.CTkScrollableFrame(tab, fg_color="transparent")
        self.list.grid(row=2, column=0, sticky="nsew")

    # -------------------------------------------------------------- record tab
    def _record_tab(self, tab):
        ctk.CTkLabel(tab, text="Record your screen + inputs", font=ctk.CTkFont(size=18, weight="bold")
                     ).pack(anchor="w", padx=12, pady=(10, 0))
        ctk.CTkLabel(tab, justify="left", text_color=MUTED, text=(
            "Records the primary monitor at 60 fps and logs cursor, clicks and key presses\n"
            "(not which keys) next to the video. That log is what powers the smart camera.\n"
            "The window minimises while recording. Press F9 to stop."),
        ).pack(anchor="w", padx=12, pady=(2, 10))

        self.v_mic_on = tk.BooleanVar(value=bool(self.cfg.get("mic_on", False)))
        ctk.CTkSwitch(tab, text="Record microphone (voiceover)", variable=self.v_mic_on,
                      progress_color=ACCENT).pack(anchor="w", padx=12)
        row = ctk.CTkFrame(tab, fg_color="transparent")
        row.pack(fill="x", padx=12, pady=6)
        self.v_mic = tk.StringVar(value="")
        self.mic_menu = ctk.CTkOptionMenu(row, variable=self.v_mic, values=["(click Refresh)"], width=360,
                                          fg_color=CARD2, button_color=CARD2, button_hover_color="#2a2c34")
        self.mic_menu.pack(side="left")
        ctk.CTkButton(row, text="Refresh", width=80, fg_color=CARD2, hover_color="#2a2c34",
                      command=self._refresh_mics).pack(side="left", padx=8)
        self.v_autoedit = tk.BooleanVar(value=bool(self.cfg.get("autoedit", True)))
        ctk.CTkSwitch(tab, text="Edit automatically when I stop", variable=self.v_autoedit,
                      progress_color=ACCENT).pack(anchor="w", padx=12, pady=(6, 14))

        self.btn_rec = ctk.CTkButton(tab, text="●  START RECORDING", height=56, fg_color=RED,
                                     hover_color="#ff7375", font=ctk.CTkFont(size=16, weight="bold"),
                                     command=self._toggle_record)
        self.btn_rec.pack(fill="x", padx=12)
        self.rec_lbl = ctk.CTkLabel(tab, text="Not recording", text_color=MUTED,
                                    font=ctk.CTkFont(size=14))
        self.rec_lbl.pack(pady=10)
        if not sys.platform.startswith("win"):
            ctk.CTkLabel(tab, text="Recording is Windows-only (uses gdigrab).",
                         text_color=AMBER).pack()

    # ============================================================== file list
    def _folder(self):
        return Path(self.v_folder.get().strip() or ".")

    def _browse(self):
        d = filedialog.askdirectory(initialdir=str(self._folder()))
        if d:
            self.v_folder.set(d)
            self._rescan()

    def _videos(self):
        f = self._folder()
        if not f.is_dir():
            return []
        return sorted((p for p in f.iterdir()
                       if p.suffix.lower() in core.VIDEO_EXTS and "_edited" not in p.stem
                       and not p.stem.startswith(".")), key=lambda p: p.stat().st_mtime, reverse=True)

    @staticmethod
    def _edited_of(p):
        return p.with_name(p.stem + "_edited.mp4")

    def _rescan(self):
        for w in self.list.winfo_children():
            w.destroy()
        self.row_widgets = {}
        vids = self._videos()
        if not vids:
            ctk.CTkLabel(self.list, text="No videos in this folder yet.\nDrop recordings in it "
                         "or use the Record tab.", text_color=MUTED).pack(pady=30)
            return
        for p in vids:
            row = ctk.CTkFrame(self.list, fg_color=CARD2, corner_radius=8)
            row.pack(fill="x", pady=3, padx=2)
            ctk.CTkLabel(row, text=p.name, anchor="w", font=ctk.CTkFont(size=12, weight="bold")
                         ).pack(side="left", padx=12, pady=9)
            if core.find_events(p):
                ctk.CTkLabel(row, text="cursor data", text_color=ACCENT, font=ctk.CTkFont(size=11)
                             ).pack(side="left", padx=(0, 8))
            btn = ctk.CTkButton(row, text="Edit", width=64, height=26, fg_color=ACCENT, text_color="#000000",
                                hover_color="#ffe14d", command=lambda q=p: self._enqueue(q, force=True))
            btn.pack(side="right", padx=10)
            lbl = ctk.CTkLabel(row, text="", font=ctk.CTkFont(size=11))
            lbl.pack(side="right", padx=8)
            self.row_widgets[p.name] = (lbl, btn)
            self._show_state(p)

    def _show_state(self, p):
        lbl_btn = self.row_widgets.get(p.name)
        if not lbl_btn:
            return
        lbl, btn = lbl_btn
        text = self.jobstate.get(p.name)
        done = self._edited_of(p).exists()
        if text is None:
            text = "Done" if done else "Ready"
        color = (GREEN if text.startswith("Done") else RED if text.startswith("Failed")
                 else AMBER if text.startswith(("Editing", "Queued")) else MUTED)
        lbl.configure(text=text, text_color=color)
        btn.configure(text="Re-edit" if done else "Edit")

    # ================================================================== jobs
    def _need_tools(self):
        missing = [t for t in ("ffmpeg", "ffprobe") if not which(t)]
        if missing:
            messagebox.showerror(APP_NAME, "Not found on PATH: " + ", ".join(missing) +
                                 "\n\nSee README.md (ffmpeg install).")
            return False
        return True

    def _enqueue(self, p, force=False):
        p = Path(p)
        if not self._need_tools():
            return
        if self.jobstate.get(p.name, "").startswith(("Queued", "Editing")):
            return
        if not force and self._edited_of(p).exists():
            return
        self.jobstate[p.name] = "Queued"
        self._show_state(p)
        self.jobs.put((p, self._settings()))
        self._save_cfg()

    def _edit_all(self):
        for p in self._videos():
            self._enqueue(p)

    def _worker(self):
        while True:
            p, st = self.jobs.get()
            self.cancel.clear()
            self.q.put(("state", p, "Editing 0%"))
            self.q.put(("busy", True))
            try:
                def prog(f, t, p=p):
                    self.q.put(("progress", p, f, t))
                out = core.edit_video(p, st, log=lambda m: self.q.put(("log", m)),
                                      progress=prog, cancel=self.cancel.is_set)
                self.q.put(("state", p, f"Done  ->  {out.name}"))
                self.q.put(("log", f"Saved {out}"))
            except core.Cancelled:
                self.q.put(("state", p, "Cancelled"))
            except Exception as e:  # noqa: BLE001
                self.q.put(("state", p, "Failed"))
                self.q.put(("log", f"Failed {p.name}: {e}"))
            self.q.put(("busy", self.jobs.qsize() > 0))

    def _watch_tick(self):
        if self.v_watch.get():
            for p in self._videos():
                try:
                    size = p.stat().st_size
                except OSError:
                    continue
                prev = self.sizes.get(p.name)
                self.sizes[p.name] = size
                if (prev == size and size > 0 and p.name not in self.jobstate
                        and not self._edited_of(p).exists()):
                    self._enqueue(p)
        self.after(3000, self._watch_tick)

    # ============================================================== recording
    def _refresh_mics(self):
        mics = __import__("recorder").list_mics()
        self.mic_menu.configure(values=mics or ["(no microphone found)"])
        self.v_mic.set(mics[0] if mics else "(no microphone found)")

    def _toggle_record(self):
        if self.recorder is not None:
            self._stop_record()
            return
        if not self._need_tools():
            return
        try:
            import pynput  # noqa: F401
        except ImportError:
            messagebox.showerror(APP_NAME, "Recording needs pynput:\n\npip install pynput")
            return
        self.btn_rec.configure(state="disabled", text="Starting...")
        self.iconify()
        self.after(1200, self._start_record)

    def _start_record(self):
        import recorder
        mic = self.v_mic.get() if self.v_mic_on.get() and "(" not in self.v_mic.get()[:1] else None
        try:
            self.recorder = recorder.Recorder(
                self._folder(), fps=60, mic=mic, log=lambda m: self.q.put(("log", m)),
                on_stop_hotkey=lambda: self.q.put(("hotkey",)))
            self.recorder.start()
        except Exception as e:  # noqa: BLE001
            self.recorder = None
            self.deiconify()
            self.btn_rec.configure(state="normal", text="●  START RECORDING")
            messagebox.showerror(APP_NAME, str(e))
            return
        self.rec_started = time.time()
        self.btn_rec.configure(state="normal", text="■  STOP  (F9)", fg_color="#333640")

    def _stop_record(self):
        rec, self.recorder = self.recorder, None
        self.btn_rec.configure(state="disabled", text="Saving...")
        self.deiconify()

        def finish():
            try:
                video = rec.stop()
                self.q.put(("recorded", video))
            except Exception as e:  # noqa: BLE001
                self.q.put(("log", f"Recording error: {e}"))
                self.q.put(("recorded", None))
        threading.Thread(target=finish, daemon=True).start()

    # ================================================================== pump
    def _log(self, text):
        self.log.configure(state="normal")
        self.log.insert("end", f"{datetime.now():%H:%M:%S}  {text}\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _pump(self):
        try:
            while True:
                m = self.q.get_nowait()
                k = m[0]
                if k == "log":
                    self._log(m[1])
                elif k == "state":
                    self.jobstate[m[1].name] = m[2]
                    self._show_state(m[1])
                elif k == "progress":
                    _, p, f, t = m
                    self.prog.set(f)
                    self.prog_lbl.configure(text=f"{p.name}: {t}", text_color="#ffffff")
                    self.jobstate[p.name] = f"Editing {int(f * 100)}%"
                    self._show_state(p)
                elif k == "busy":
                    if m[1]:
                        self.btn_cancel.pack(side="left", padx=8)
                    else:
                        self.btn_cancel.pack_forget()
                        self.prog.set(0)
                        self.prog_lbl.configure(text="Idle", text_color=MUTED)
                elif k == "hotkey":
                    if self.recorder is not None:
                        self._stop_record()
                elif k == "recorded":
                    self.btn_rec.configure(state="normal", text="●  START RECORDING", fg_color=RED)
                    self.rec_lbl.configure(text="Not recording", text_color=MUTED)
                    self._rescan()
                    if m[1] and self.v_autoedit.get():
                        self._enqueue(m[1], force=True)
        except queue.Empty:
            pass
        if self.recorder is not None and self.rec_started:
            s = int(time.time() - self.rec_started)
            self.rec_lbl.configure(text=f"●  Recording  {s // 60:02}:{s % 60:02}", text_color=RED)
        self.after(200, self._pump)

    def _save_cfg(self):
        try:
            CONFIG_PATH.write_text(json.dumps({
                "folder": self.v_folder.get(), "watch": bool(self.v_watch.get()),
                "mic_on": bool(self.v_mic_on.get()), "autoedit": bool(self.v_autoedit.get()),
                "settings": asdict(self._settings())}, indent=2))
        except OSError:
            pass

    def _close(self):
        self._save_cfg()
        if self.recorder is not None:
            try:
                self.recorder.stop()
            except Exception:  # noqa: BLE001
                pass
        self.cancel.set()
        self.destroy()


if __name__ == "__main__":
    App().mainloop()
