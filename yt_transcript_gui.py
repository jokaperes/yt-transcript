from __future__ import annotations

import faulthandler
import json
import logging
import os
import platform
import queue
import shutil
import subprocess
import sys
import threading
import time
import traceback
import tkinter as tk
import urllib.request
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Any

os.environ["TQDM_DISABLE"] = "1"
os.environ["TQDM_NO_RESIZE"] = "1"

import faulthandler
faulthandler.disable()

from transcribe_youtube import (
    DEFAULT_MODEL,
    OUTPUT_FORMATS,
    check_dependencies,
    download_audio,
    parse_urls,
    safe_name,
    setup_logging,
    transcribe_faster,
    write_json,
    write_md,
    write_srt,
    write_txt,
    write_vtt,
)

try:
    import pystray
    from PIL import Image, ImageDraw
    HAS_PYSTRAY = True
except ImportError:
    HAS_PYSTRAY = False

__version__ = "2.3.2"

REPO_OWNER = "jokaperes"
REPO_NAME = "yt-transcript"
RELEASE_ZIP_NAME = "yt-transcript-windows.zip"

SETTINGS_PATH = Path(sys.executable).with_name("settings.json") if getattr(sys, "frozen", False) else Path.cwd() / "settings.json"

log = logging.getLogger(__name__)


def load_settings() -> dict:
    try:
        return json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_settings(settings: dict) -> None:
    try:
        SETTINGS_PATH.write_text(json.dumps(settings, indent=2), encoding="utf-8")
    except Exception:
        pass


def install_crash_handlers() -> None:
    log_path = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path.cwd()
    fault_file = (log_path / "yt-transcript-crash.log").open("a", encoding="utf-8")
    faulthandler.enable(file=fault_file)

    def excepthook(exc_type: type[BaseException], exc: BaseException, tb: Any) -> None:
        log.exception("UNHANDLED EXCEPTION", exc_info=(exc_type, exc, tb))

    def threading_hook(args: threading.ExceptHookArgs) -> None:
        log.exception("UNHANDLED THREAD EXCEPTION in %s", args.thread.name, exc_info=(args.exc_type, args.exc_value, args.exc_traceback))

    sys.excepthook = excepthook
    threading.excepthook = threading_hook


def make_tray_icon(width: int = 64, height: int = 64, color: str = "#2563eb") -> Any:
    if not HAS_PYSTRAY:
        return None
    img = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    dc = ImageDraw.Draw(img)
    dc.rectangle([4, 4, width - 4, height - 4], fill=color, outline="white", width=2)
    try:
        # Default bitmap font (no FreeType / _imagingft dependency); skip if
        # the stripped build lacks font support rather than crashing the tray.
        dc.text((width // 4, height // 4), "YT", fill="white")
    except Exception:
        pass
    return img


class App(ttk.Frame):
    def __init__(self, root: tk.Tk) -> None:
        super().__init__(root, padding=16)
        self.root = root
        self.root.title(f"YT Transcript v{__version__}")
        self.root.geometry("860x740")
        self.root.minsize(760, 540)

        self.events: queue.Queue[tuple[str, Any]] = queue.Queue()
        self.worker: threading.Thread | None = None
        self.cancel_requested = threading.Event()

        saved = load_settings()
        self.output_dir = tk.StringVar(value=saved.get("output_dir", str(Path.cwd() / "transcripts")))
        self.cookies = tk.StringVar(value=saved.get("cookies", ""))
        self.model = tk.StringVar(value=saved.get("model", DEFAULT_MODEL))
        self.device = tk.StringVar(value=saved.get("device", "cuda"))
        self.compute_type = tk.StringVar(value=saved.get("compute_type", "float16"))
        self.language = tk.StringVar(value=saved.get("language", "pt"))
        self.format_vars: dict[str, tk.BooleanVar] = {}
        for fmt in OUTPUT_FORMATS:
            default = fmt in ("md", "txt")
            self.format_vars[fmt] = tk.BooleanVar(value=saved.get(f"fmt_{fmt}", default))
        self.status = tk.StringVar(value="Ready")
        self.queue_status = tk.StringVar(value="")
        self.elapsed = tk.StringVar(value="Elapsed: 00:00")
        self.eta = tk.StringVar(value="")
        self.detail = tk.StringVar(value="")
        self.phase_percent_text = tk.StringVar(value="Current step: --")
        self.total_percent_text = tk.StringVar(value="Overall: 0%")
        self.phase_progress = tk.DoubleVar(value=0)
        self.total_progress = tk.DoubleVar(value=0)
        self.started_at: float | None = None
        self.phase_indeterminate = False
        self.url_text: tk.Text | None = None

        self._tray_icon: Any = None
        self._tray_running = False
        self._tray_notify_text = ""

        self._build()
        self._setup_tray()
        log.info("GUI started v%s", __version__)
        self.root.after(100, self._drain_events)

    def _build(self) -> None:
        self.grid(row=0, column=0, sticky="nsew")
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(0, weight=1)
        self.columnconfigure(1, weight=1)
        self.rowconfigure(0, weight=1)
        self.rowconfigure(12, weight=1)

        url_frame = ttk.LabelFrame(self, text="YouTube URLs (one per line)")
        url_frame.grid(row=0, column=0, columnspan=3, sticky="nsew", pady=(0, 4))
        url_frame.columnconfigure(0, weight=1)
        url_frame.rowconfigure(0, weight=1)

        self.url_text = tk.Text(url_frame, height=4, wrap="word")
        self.url_text.grid(row=0, column=0, sticky="nsew", padx=(4, 0), pady=4)
        url_scroll = ttk.Scrollbar(url_frame, orient="vertical", command=self.url_text.yview)
        url_scroll.grid(row=0, column=1, sticky="ns", pady=4, padx=(0, 4))
        self.url_text.configure(yscrollcommand=url_scroll.set)

        paste_btn = ttk.Button(url_frame, text="Paste", command=self._paste_urls, width=6)
        paste_btn.grid(row=1, column=0, sticky="w", padx=4, pady=(0, 4))
        clear_urls_btn = ttk.Button(url_frame, text="Clear", command=self._clear_urls, width=6)
        clear_urls_btn.grid(row=1, column=1, sticky="w", padx=(0, 4), pady=(0, 4))

        url_hint = ttk.Label(self, text="Paste one or more YouTube URLs above. Lines starting with # are ignored.", foreground="gray")
        url_hint.grid(row=1, column=0, columnspan=3, sticky="w")

        ttk.Label(self, text="Output folder").grid(row=2, column=0, sticky="w", pady=4)
        ttk.Entry(self, textvariable=self.output_dir).grid(row=2, column=1, sticky="ew", pady=4)
        ttk.Button(self, text="Browse", command=self._choose_output).grid(row=2, column=2, sticky="ew", padx=(8, 0), pady=4)

        ttk.Label(self, text="Cookies file").grid(row=3, column=0, sticky="w", pady=4)
        ttk.Entry(self, textvariable=self.cookies).grid(row=3, column=1, sticky="ew", pady=4)
        ttk.Button(self, text="Browse", command=self._choose_cookies).grid(row=3, column=2, sticky="ew", padx=(8, 0), pady=4)

        options = ttk.Frame(self)
        options.grid(row=4, column=0, columnspan=3, sticky="ew", pady=4)
        for col in range(7):
            options.columnconfigure(col, weight=1)

        ttk.Label(options, text="Model").grid(row=0, column=0, sticky="w")
        ttk.Combobox(
            options,
            textvariable=self.model,
            values=("large-v3-turbo", "large-v3", "medium", "small", "base"),
        ).grid(row=1, column=0, sticky="ew", padx=(0, 4))

        ttk.Label(options, text="Device").grid(row=0, column=1, sticky="w")
        ttk.Combobox(options, textvariable=self.device, values=("cuda", "cpu")).grid(row=1, column=1, sticky="ew", padx=4)

        ttk.Label(options, text="Compute").grid(row=0, column=2, sticky="w")
        ttk.Combobox(
            options,
            textvariable=self.compute_type,
            values=("float16", "int8_float16", "int8"),
        ).grid(row=1, column=2, sticky="ew", padx=4)

        ttk.Label(options, text="Language").grid(row=0, column=3, sticky="w")
        ttk.Combobox(
            options,
            textvariable=self.language,
            values=("pt", "en", "es", "fr", "de", "it", "ja", "ko", "zh", "ru"),
            width=5,
        ).grid(row=1, column=3, sticky="ew", padx=4)

        fmt_frame = ttk.LabelFrame(self, text="Output formats")
        fmt_frame.grid(row=5, column=0, columnspan=3, sticky="ew", pady=4)
        for i, fmt in enumerate(OUTPUT_FORMATS):
            ttk.Checkbutton(fmt_frame, text=fmt.upper(), variable=self.format_vars[fmt]).grid(row=0, column=i, padx=8, sticky="w")

        buttons = ttk.Frame(self)
        buttons.grid(row=6, column=0, columnspan=3, sticky="ew", pady=(8, 4))
        buttons.columnconfigure(0, weight=1)
        buttons.columnconfigure(1, weight=1)
        self.start_button = ttk.Button(buttons, text="Transcribe", command=self._start)
        self.start_button.grid(row=0, column=0, sticky="ew", padx=(0, 4), ipady=6)
        self.cancel_button = ttk.Button(buttons, text="Cancel", command=self._cancel, state="disabled")
        self.cancel_button.grid(row=0, column=1, sticky="ew", padx=(4, 0), ipady=6)

        status_bar = ttk.Frame(self)
        status_bar.grid(row=7, column=0, columnspan=3, sticky="ew", pady=(0, 4))
        status_bar.columnconfigure(1, weight=1)
        ttk.Label(status_bar, textvariable=self.status).grid(row=0, column=0, sticky="w")
        ttk.Label(status_bar, textvariable=self.queue_status).grid(row=0, column=1, sticky="ew", padx=8)
        ttk.Label(status_bar, textvariable=self.eta).grid(row=0, column=2, sticky="e")
        ttk.Label(status_bar, textvariable=self.elapsed).grid(row=0, column=3, sticky="e", padx=(8, 0))

        ttk.Label(self, textvariable=self.detail).grid(row=8, column=0, columnspan=3, sticky="w", pady=(0, 2))

        ttk.Label(self, textvariable=self.phase_percent_text).grid(row=9, column=0, sticky="w")
        self.phase_bar = ttk.Progressbar(self, mode="determinate", variable=self.phase_progress, maximum=100)
        self.phase_bar.grid(row=9, column=1, columnspan=2, sticky="ew", pady=(0, 4))

        ttk.Label(self, textvariable=self.total_percent_text).grid(row=10, column=0, sticky="w")
        self.total_bar = ttk.Progressbar(self, mode="determinate", variable=self.total_progress, maximum=100)
        self.total_bar.grid(row=10, column=1, columnspan=2, sticky="ew", pady=(0, 4))

        self.log_text = tk.Text(self, height=14, wrap="word")
        self.log_text.grid(row=12, column=0, columnspan=3, sticky="nsew")
        self.log_text.configure(state="disabled")
        self.log_text.tag_configure("info", foreground="#1f2937")
        self.log_text.tag_configure("warning", foreground="#92400e")
        self.log_text.tag_configure("error", foreground="#991b1b")
        self.log_text.tag_configure("success", foreground="#166534")
        self.log_text.tag_configure("separator", foreground="#6b7280")

        self.log_menu = tk.Menu(self.root, tearoff=0)
        self.log_menu.add_command(label="Copy selected", command=self._copy_log_selection)
        self.log_menu.add_command(label="Select all", command=self._select_all_log)
        self.log_menu.add_separator()
        self.log_menu.add_command(label="Clear log", command=self._clear_log)
        self.log_text.bind("<Button-3>", self._show_log_menu)
        self.log_text.bind("<Control-c>", lambda e: self._copy_log_selection())

        actions = ttk.Frame(self)
        actions.grid(row=13, column=0, columnspan=3, sticky="ew", pady=(8, 0))
        actions.columnconfigure(0, weight=1)
        ttk.Button(actions, text="Open output folder", command=self._open_output).grid(row=0, column=1, sticky="e")
        self.update_button = ttk.Button(actions, text=f"Check for updates (v{__version__})", command=self._check_update)
        self.update_button.grid(row=0, column=2, sticky="e", padx=(8, 0))

    def _setup_tray(self) -> None:
        if not HAS_PYSTRAY:
            return
        try:
            icon_image = make_tray_icon()
            if icon_image is None:
                return
            menu = pystray.Menu(
                pystray.MenuItem("Show window", self._tray_show),
                pystray.MenuItem("Cancel", self._tray_cancel),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("Quit", self._tray_quit),
            )
            self._tray_icon = pystray.Icon("yt-transcript", icon_image, "YT Transcript", menu)
            self._tray_running = True
            threading.Thread(target=self._tray_icon.run, daemon=True).start()
            self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        except Exception as exc:
            log.warning("Tray icon setup failed: %s", exc)
            self._tray_icon = None

    def _update_tray_tooltip(self, text: str) -> None:
        if not self._tray_icon:
            return
        try:
            self._tray_icon.title = text
        except Exception:
            pass

    def _on_close(self) -> None:
        if self._tray_icon:
            self._tray_notify_text = "Minimized to tray"
            self.root.withdraw()
            self._update_tray_tooltip("YT Transcript — minimized")
            return
        self._quit_app()

    def _tray_show(self) -> None:
        self.root.after(0, self._restore_window)

    def _tray_cancel(self) -> None:
        self.root.after(0, self._cancel)

    def _tray_quit(self) -> None:
        self.root.after(0, self._quit_app)

    def _restore_window(self) -> None:
        self.root.deiconify()
        self.root.lift()

    def _quit_app(self) -> None:
        if self._tray_icon:
            try:
                self._tray_icon.stop()
            except Exception:
                pass
        self.root.quit()

    def _paste_urls(self) -> None:
        try:
            clip = self.root.clipboard_get()
            if self.url_text:
                self.url_text.insert("end", clip)
        except Exception:
            pass

    def _clear_urls(self) -> None:
        if self.url_text:
            self.url_text.delete("1.0", "end")

    def _show_log_menu(self, event: Any) -> None:
        self.log_menu.tk_popup(event.x_root, event.y_root)

    def _copy_log_selection(self) -> None:
        if self.log_text:
            try:
                selected = self.log_text.get("sel.first", "sel.last")
                self.root.clipboard_clear()
                self.root.clipboard_append(selected)
            except tk.TclError:
                pass

    def _select_all_log(self) -> None:
        if self.log_text:
            self.log_text.tag_add("sel", "1.0", "end")

    def _clear_log(self) -> None:
        if self.log_text:
            self.log_text.configure(state="normal")
            self.log_text.delete("1.0", "end")
            self.log_text.configure(state="disabled")

    def _choose_output(self) -> None:
        folder = filedialog.askdirectory(initialdir=self.output_dir.get() or str(Path.cwd()))
        if folder:
            self.output_dir.set(folder)

    def _choose_cookies(self) -> None:
        file_path = filedialog.askopenfilename(
            title="Select cookies.txt",
            filetypes=(("Cookies text file", "*.txt"), ("All files", "*.*")),
        )
        if file_path:
            self.cookies.set(file_path)

    def _append_log(self, message: str, level: str = "info") -> None:
        log.info("%s: %s", level.upper(), message)
        self.log_text.configure(state="normal")
        self.log_text.insert("end", message.rstrip() + "\n", level)
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _append_separator(self) -> None:
        self.log_text.configure(state="normal")
        self.log_text.insert("end", "─" * 60 + "\n", "separator")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _save_current_settings(self) -> None:
        settings = {
            "output_dir": self.output_dir.get(),
            "cookies": self.cookies.get(),
            "model": self.model.get(),
            "device": self.device.get(),
            "compute_type": self.compute_type.get(),
            "language": self.language.get(),
        }
        for fmt in OUTPUT_FORMATS:
            settings[f"fmt_{fmt}"] = self.format_vars[fmt].get()
        save_settings(settings)

    def _set_busy(self, busy: bool) -> None:
        self.start_button.configure(state="disabled" if busy else "normal")
        self.cancel_button.configure(state="normal" if busy else "disabled")
        url_state = "disabled" if busy else "normal"
        if self.url_text:
            self.url_text.configure(state=url_state)
        if not busy:
            self._set_phase_indeterminate(False)
            self.phase_progress.set(0)
            self.eta.set("")

    def _start(self) -> None:
        if self.worker and self.worker.is_alive():
            return
        raw_text = self.url_text.get("1.0", "end") if self.url_text else ""
        urls = parse_urls(raw_text)
        if not urls:
            messagebox.showerror("Missing URL", "Paste one or more YouTube URLs in the text box.")
            return
        issues = check_dependencies()
        if issues:
            messagebox.showerror("Missing dependency", "\n".join(issues))
            return
        cookies = self.cookies.get().strip()
        if cookies and not Path(cookies).exists():
            messagebox.showerror("Cookies not found", f"Cookies file does not exist:\n{cookies}")
            return
        if not any(self.format_vars[fmt].get() for fmt in OUTPUT_FORMATS):
            messagebox.showerror("No format selected", "Select at least one output format.")
            return
        if self.device.get().strip() == "cuda" and not shutil.which("nvidia-smi"):
            self._append_log("nvidia-smi was not found. Continuing anyway, but CUDA may fail if the NVIDIA driver is not installed.")
        if not shutil.which("node"):
            self._append_log("Node.js was not found. Some YouTube videos may fail player challenge solving without it.")
        self._save_current_settings()
        self.cancel_requested.clear()
        self.started_at = time.monotonic()
        self._video_start_time = time.monotonic()
        self.phase_progress.set(0)
        self.total_progress.set(0)
        self.phase_percent_text.set("Current step: --")
        self.total_percent_text.set("Overall: 0%")
        self.detail.set("Preparing")
        self.total_urls = len(urls)
        self.completed_urls = 0
        self.failed_urls = 0
        self._set_busy(True)
        self.status.set("Running")
        self.queue_status.set(f"Queue: 0/{self.total_urls}")
        self.eta.set("")
        self._append_log(f"Starting batch: {self.total_urls} URL(s)")
        self._append_separator()
        self._update_tray_tooltip(f"YT Transcript — Processing 0/{self.total_urls}")
        self.worker = threading.Thread(target=self._run_batch, args=(urls,), daemon=True)
        self.worker.start()

    def _cancel(self) -> None:
        self.cancel_requested.set()
        self.status.set("Cancelling")
        self._append_log("Cancel requested — will stop after current video.")
        self._update_tray_tooltip("YT Transcript — Cancelling...")

    def _update_eta(self, percent: float | None, phase: str) -> None:
        if self.started_at is None or percent is None or percent <= 0:
            return
        elapsed = time.monotonic() - self.started_at
        if elapsed < 2:
            return
        progress_frac = self.total_progress.get() / 100.0
        if progress_frac <= 0.01:
            return
        remaining_est = elapsed / progress_frac - elapsed
        if remaining_est < 0:
            return
        mins, secs = divmod(int(remaining_est), 60)
        hours, mins = divmod(mins, 60)
        if hours > 0:
            self.eta.set(f"ETA: {hours}h {mins}m")
        elif mins > 0:
            self.eta.set(f"ETA: {mins}m {secs}s")
        else:
            self.eta.set(f"ETA: {secs}s")

    def _run_batch(self, urls: list[str]) -> None:
        output_dir = Path(self.output_dir.get()).expanduser().resolve()
        cookies = self.cookies.get().strip() or None
        model = self.model.get().strip() or DEFAULT_MODEL
        device = self.device.get().strip() or "cuda"
        compute_type = self.compute_type.get().strip() or "float16"
        language = self.language.get().strip() or "pt"
        formats = [fmt for fmt in OUTPUT_FORMATS if self.format_vars[fmt].get()]

        effective_compute = compute_type
        if device == "cpu":
            effective_compute = "int8"

        all_output_files: list[Path] = []

        for i, url in enumerate(urls):
            if self.cancel_requested.is_set():
                self.events.put(("log", f"Cancelled. Processed {i}/{self.total_urls} URL(s)."))
                break

            self._video_start_time = time.monotonic()
            self.events.put(("queue_progress", (i + 1, self.total_urls)))
            self.events.put(("log", f"[{i + 1}/{self.total_urls}] {url}"))
            self.events.put(("progress", ("download", None, f"Downloading [{i + 1}/{self.total_urls}]")))
            self.events.put(("batch_phase", (i, self.total_urls)))
            self._update_tray_tooltip(f"YT Transcript — [{i + 1}/{self.total_urls}] Downloading")

            try:
                audio_path, info = download_audio(
                    url,
                    output_dir,
                    cookies,
                    None,
                    log=lambda message: self.events.put(("log", message)),
                    progress=lambda phase, percent, detail: self.events.put(("progress", (phase, percent, detail))),
                    stop_requested=self.cancel_requested.is_set,
                )
                self.events.put(("log", f"Audio saved: {audio_path}"))

                if self.cancel_requested.is_set():
                    self.events.put(("log", f"Cancelled during download of URL {i + 1}."))
                    break

                self.events.put(("progress", ("model", None, f"Loading model [{i + 1}/{self.total_urls}]")))
                self._update_tray_tooltip(f"YT Transcript — [{i + 1}/{self.total_urls}] Transcribing")

                text, segments = transcribe_faster(
                    audio_path,
                    model,
                    "transcribe",
                    language,
                    device,
                    effective_compute,
                    5,
                    log=lambda message: self.events.put(("log", message)),
                    progress=lambda phase, percent, detail: self.events.put(("progress", (phase, percent, detail))),
                    stop_requested=self.cancel_requested.is_set,
                )

                if self.cancel_requested.is_set():
                    self.events.put(("log", f"Cancelled after transcription of URL {i + 1}."))
                    break

                title = safe_name(info.get("title") or audio_path.stem)
                stem = output_dir / title
                self.events.put(("progress", ("write", 0.0, f"Writing files [{i + 1}/{self.total_urls}]")))

                for fmt in formats:
                    out_path = stem.with_suffix(f".{language}.{fmt}")
                    try:
                        if fmt == "md":
                            write_md(out_path, segments, info)
                        elif fmt == "txt":
                            write_txt(out_path, segments)
                        elif fmt == "srt":
                            write_srt(out_path, segments)
                        elif fmt == "vtt":
                            write_vtt(out_path, segments)
                        elif fmt == "json":
                            write_json(out_path, segments, info)
                        all_output_files.append(out_path)
                        self.events.put(("log", f"Wrote {fmt}: {out_path}"))
                    except Exception as exc:
                        self.events.put(("log", f"Failed to write {fmt}: {exc}"))

                try:
                    audio_path.unlink(missing_ok=True)
                except Exception:
                    pass

                self.completed_urls += 1
                self.events.put(("log", f"Done: {title}"))

            except BaseException as exc:
                self.failed_urls += 1
                err_msg = str(exc)
                if "cancelled" in err_msg.lower() or "cancel" in err_msg.lower():
                    self.events.put(("log", "Cancelled by user."))
                    break
                self.events.put(("log", f"Error processing {url}: {err_msg}"))
                self.events.put(("url_failed", url))
                self.events.put(("separator", None))
                continue

            self.events.put(("separator", None))

        self.events.put(("batch_done", (all_output_files, self.completed_urls, self.failed_urls, self.total_urls)))

    def _drain_events(self) -> None:
        try:
            while True:
                event, payload = self.events.get_nowait()
                if event == "log":
                    message = str(payload)
                    level = "info"
                    if "WARNING:" in message:
                        level = "warning"
                    if "ERROR:" in message or "failed" in message.lower():
                        level = "error"
                    self._append_log(message, level)
                elif event == "progress":
                    phase, percent, detail = payload
                    self._update_progress(str(phase), percent, str(detail))
                    self._update_eta(percent, str(phase))
                elif event == "separator":
                    self._append_separator()
                elif event == "queue_progress":
                    current, total = payload
                    self.queue_status.set(f"Queue: {current}/{total}")
                elif event == "batch_phase":
                    pass
                elif event == "url_failed":
                    pass
                elif event == "done":
                    self._set_busy(False)
                    self.status.set("Done")
                    self.phase_progress.set(100)
                    self.total_progress.set(100)
                    self.detail.set("Finished")
                    self.eta.set("")
                    self._append_log("Finished", "success")
                    for path in payload:
                        self._append_log(str(path), "success")
                    messagebox.showinfo("Done", "Transcript files were created.")
                elif event == "batch_done":
                    all_files, completed, failed, total = payload
                    self._set_busy(False)
                    self.phase_progress.set(100)
                    self.total_progress.set(100)
                    self.eta.set("")
                    if failed == 0:
                        self.status.set("Done")
                        self.detail.set(f"All {completed} video(s) completed")
                    else:
                        self.status.set("Done (with errors)")
                        self.detail.set(f"{completed} done, {failed} failed out of {total}")
                    self._append_separator()
                    if failed == 0:
                        self._append_log(f"Batch complete: {completed}/{total} succeeded.", "success")
                    else:
                        self._append_log(f"Batch complete: {completed} succeeded, {failed} failed out of {total}.", "warning")
                    for path in all_files:
                        self._append_log(str(path), "success")
                    self.queue_status.set(f"Done: {completed}/{total}")
                    self._update_tray_tooltip(f"YT Transcript — Done {completed}/{total}")
                    if failed == 0:
                        if self.root.state() != "normal":
                            self.root.after(0, self._restore_window)
                        messagebox.showinfo("Done", f"All {completed} transcript(s) created successfully.")
                    else:
                        if self.root.state() != "normal":
                            self.root.after(0, self._restore_window)
                        messagebox.showwarning(
                            "Batch finished with errors",
                            f"{completed} of {total} succeeded.\n{failed} video(s) failed — check the log for details.",
                        )
                elif event == "error":
                    self._set_busy(False)
                    self.status.set("Error")
                    self.eta.set("")
                    self._append_log(str(payload), "error")
                    messagebox.showerror("Error", str(payload))
                elif event == "update_result":
                    self._handle_update_result(payload)
        except queue.Empty:
            pass
        self._update_elapsed()
        self.root.after(100, self._drain_events)

    def _update_progress(self, phase: str, percent: Any, detail: str) -> None:
        weights = {
            "download": (0, 30),
            "extract": (25, 10),
            "model": (35, 20),
            "transcribe": (55, 40),
            "write": (95, 5),
        }
        labels = {
            "download": "Downloading audio",
            "extract": "Extracting audio",
            "model": "Loading Whisper model",
            "transcribe": "Transcribing",
            "write": "Writing files",
        }
        self.status.set(labels.get(phase, phase.title()))
        if percent is None:
            self._set_phase_indeterminate(True)
            start, _ = weights.get(phase, (0, 0))
            self.total_progress.set(max(self.total_progress.get(), float(start)))
            self.total_percent_text.set(f"Overall: {self.total_progress.get():.0f}%")
            self.phase_percent_text.set("Current step: working")
            self.detail.set(detail)
            return
        self._set_phase_indeterminate(False)
        value = max(0.0, min(100.0, float(percent)))
        self.phase_progress.set(value)
        self.phase_percent_text.set(f"Current step: {value:.1f}%")
        start, weight = weights.get(phase, (0, 0))
        self.total_progress.set(max(self.total_progress.get(), min(100.0, start + weight * (value / 100.0))))
        self.total_percent_text.set(f"Overall: {self.total_progress.get():.0f}%")
        self.detail.set(f"{detail} ({value:.1f}%)")

    def _set_phase_indeterminate(self, enabled: bool) -> None:
        if enabled == self.phase_indeterminate:
            return
        self.phase_indeterminate = enabled
        if enabled:
            self.phase_bar.configure(mode="indeterminate")
            self.phase_bar.start(10)
        else:
            self.phase_bar.stop()
            self.phase_bar.configure(mode="determinate")

    def _update_elapsed(self) -> None:
        if self.started_at is None:
            return
        seconds = int(time.monotonic() - self.started_at)
        minutes, secs = divmod(seconds, 60)
        hours, minutes = divmod(minutes, 60)
        if hours:
            self.elapsed.set(f"Elapsed: {hours}h {minutes:02}m")
        elif minutes:
            self.elapsed.set(f"Elapsed: {minutes}:{secs:02}")
        else:
            self.elapsed.set(f"Elapsed: {secs}s")

    def _open_output(self) -> None:
        folder = Path(self.output_dir.get()).expanduser().resolve()
        folder.mkdir(parents=True, exist_ok=True)
        import os
        if platform.system() == "Windows":
            os.startfile(folder)
        elif platform.system() == "Darwin":
            subprocess.Popen(["open", str(folder)])
        else:
            subprocess.Popen(["xdg-open", str(folder)])

    def _check_update(self) -> None:
        self.update_button.configure(state="disabled")
        self.status.set("Checking for updates...")
        self._append_log("Checking for updates...", "info")

        def worker() -> None:
            try:
                api_url = f"https://api.github.com/repos/{REPO_OWNER}/{REPO_NAME}/releases/latest"
                req = urllib.request.Request(api_url, headers={"User-Agent": "yt-transcript"})
                with urllib.request.urlopen(req, timeout=10) as resp:
                    release = json.loads(resp.read())

                latest_tag = release.get("tag_name", "").lstrip("v")
                current_tag = __version__

                def _parse_version(v: str) -> tuple[int, ...]:
                    parts = []
                    for p in v.split("."):
                        try:
                            parts.append(int(p))
                        except ValueError:
                            parts.append(0)
                    return tuple(parts)

                if _parse_version(latest_tag) <= _parse_version(current_tag):
                    self.events.put(("log", f"Already up to date (v{current_tag})."))
                    self.events.put(("update_result", ("up_to_date", current_tag, latest_tag)))
                    return

                zip_url = None
                for asset in release.get("assets", []):
                    if asset.get("name") == RELEASE_ZIP_NAME:
                        zip_url = asset.get("browser_download_url")
                        break

                if not zip_url:
                    self.events.put(("log", "No Windows zip found in release."))
                    self.events.put(("update_result", ("no_zip", current_tag, latest_tag)))
                    return

                self.events.put(("update_result", ("available", current_tag, latest_tag, zip_url)))

            except Exception as exc:
                self.events.put(("log", f"Update check failed: {exc}"))
                self.events.put(("update_result", ("error", str(exc))))

        threading.Thread(target=worker, daemon=True).start()

    def _apply_update(self, zip_url: str, new_version: str) -> None:
        import tempfile

        self.status.set("Downloading update...")
        self._append_log(f"Downloading v{new_version}...", "info")

        def worker() -> None:
            try:
                temp_dir = Path(tempfile.mkdtemp(prefix="yt-transcript-update-"))
                zip_path = temp_dir / RELEASE_ZIP_NAME

                self.events.put(("log", f"Downloading to {zip_path}..."))
                req = urllib.request.Request(zip_url, headers={"User-Agent": "yt-transcript"})
                with urllib.request.urlopen(req, timeout=120) as resp:
                    with zip_path.open("wb") as f:
                        while True:
                            chunk = resp.read(65536)
                            if not chunk:
                                break
                            f.write(chunk)

                self.events.put(("log", "Download complete. Creating updater..."))

                exe_dir = Path(sys.executable).parent
                if platform.system() == "Windows":
                    bat_path = exe_dir / "update-inplace.bat"
                    bat_content = (
                        "@echo off\r\n"
                        "cd /d \"%~dp0\"\r\n"
                        "echo Waiting for app to close...\r\n"
                        "powershell -Command \"Start-Sleep -Seconds 3\"\r\n"
                        f"echo Extracting v{new_version}...\r\n"
                        f"powershell -Command \"Expand-Archive -Path '{zip_path}' -DestinationPath '.' -Force\"\r\n"
                        "echo Done! Restart the app.\r\n"
                        "pause\r\n"
                        f"del \"{bat_path}\"\r\n"
                    )
                    bat_path.write_text(bat_content, encoding="utf-8")
                    subprocess.Popen(
                        ["cmd", "/c", "start", "", str(bat_path)],
                        cwd=exe_dir,
                        shell=True,
                    )
                    self.root.after(500, self._quit_app)
                else:
                    import zipfile
                    with zipfile.ZipFile(zip_path, "r") as zf:
                        zf.extractall(exe_dir)
                    self.events.put(("log", f"Updated to v{new_version}. Restart the app."))
                    self._set_busy(False)
                    self.status.set("Update applied")

            except Exception as exc:
                self.events.put(("log", f"Update failed: {exc}"))
                self._set_busy(False)
                self.status.set("Update failed")

        self._set_busy(True)
        self.status.set("Applying update...")
        threading.Thread(target=worker, daemon=True).start()

    def _open_release_page(self) -> None:
        import webbrowser
        webbrowser.open(f"https://github.com/{REPO_OWNER}/{REPO_NAME}/releases/latest")

    def _handle_update_result(self, result: tuple) -> None:
        self.update_button.configure(state="normal")
        kind = result[0]
        if kind == "up_to_date":
            self._append_log(f"Already up to date (v{result[1]}).", "success")
            self.status.set("Up to date")
            messagebox.showinfo("Up to date", f"You are running the latest version (v{result[1]}).")
        elif kind == "no_zip":
            self._append_log(f"v{result[2]} is available but no zip was found.", "warning")
            self.status.set("Update available")
            if messagebox.askyesno("Update available", f"v{result[2]} is available (you have v{result[1]}).\n\nOpen download page?"):
                self._open_release_page()
        elif kind == "available":
            _, current, latest, zip_url = result
            self._append_log(f"v{latest} available (you have v{current}).", "info")
            self.status.set("Update available")
            if messagebox.askyesno("Update available", f"v{latest} is available (you have v{current}).\n\nDownload and install automatically?"):
                self._apply_update(zip_url, latest)
        elif kind == "error":
            self._append_log(f"Update check error: {result[1]}", "error")
            self.status.set("Update check failed")


def main() -> None:
    setup_logging()
    install_crash_handlers()
    try:
        root = tk.Tk()
        root.report_callback_exception = lambda exc, val, tb: (
            log.exception("TK CALLBACK ERROR", exc_info=(exc, val, tb)),
            messagebox.showerror("Error", str(val)),
        )
        App(root)
        root.mainloop()
    except BaseException:
        log.exception("FATAL GUI ERROR")
        raise
    finally:
        if HAS_PYSTRAY:
            pass


if __name__ == "__main__":
    # The bundled build re-invokes this same exe as the yt-dlp runner (the
    # yt_dlp module is bundled, so we don't ship a separate ~18 MB yt-dlp.exe).
    if len(sys.argv) >= 2 and sys.argv[1] == "--run-ytdlp":
        # In a --windowed frozen build sys.stdout/stderr are None; reattach them
        # to the inherited OS pipe handles so the parent can read yt-dlp output.
        _pipe = open(1, "w", encoding="utf-8", errors="replace", buffering=1, closefd=False)
        sys.stdout = _pipe
        sys.stderr = _pipe
        import yt_dlp
        sys.argv = ["yt-dlp", *sys.argv[2:]]
        sys.exit(yt_dlp.main())
    main()