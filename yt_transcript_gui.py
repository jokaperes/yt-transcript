from __future__ import annotations

import datetime as dt
import faulthandler
import json
import os
import queue
import re
import shutil
import sys
import threading
import time
import traceback
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Any

os.environ["TQDM_DISABLE"] = "1"
os.environ["TQDM_NO_RESIZE"] = "1"
os.environ["PYTHONUNBUFFERED"] = "0"

import faulthandler
faulthandler.disable()

from transcribe_youtube import (
    DEFAULT_MODEL,
    check_dependencies,
    download_audio,
    safe_name,
    transcribe_faster,
    write_srt,
    write_txt,
    write_vtt,
)


LOG_PATH = Path.cwd() / "yt-transcript.log"


def file_log(message: str) -> None:
    stamp = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with LOG_PATH.open("a", encoding="utf-8") as handle:
            handle.write(f"[{stamp}] {message.rstrip()}\n")
    except Exception:
        pass


def install_crash_handlers() -> None:
    try:
        fault_file = LOG_PATH.open("a", encoding="utf-8")
        faulthandler.enable(file=fault_file)
    except Exception:
        pass

    def excepthook(exc_type: type[BaseException], exc: BaseException, tb: Any) -> None:
        file_log("UNHANDLED EXCEPTION")
        file_log("".join(traceback.format_exception(exc_type, exc, tb)))

    def threading_hook(args: threading.ExceptHookArgs) -> None:
        file_log(f"UNHANDLED THREAD EXCEPTION in {args.thread.name}")
        file_log("".join(traceback.format_exception(args.exc_type, args.exc_value, args.exc_traceback)))

    sys.excepthook = excepthook
    threading.excepthook = threading_hook


class App(ttk.Frame):
    def __init__(self, root: tk.Tk) -> None:
        super().__init__(root, padding=16)
        self.root = root
        self.root.title("YT Transcript")
        self.root.geometry("860x620")
        self.root.minsize(760, 540)

        self.events: queue.Queue[tuple[str, Any]] = queue.Queue()
        self.worker: threading.Thread | None = None
        self.cancel_requested = threading.Event()

        self.url = tk.StringVar()
        self.output_dir = tk.StringVar(value=str(Path.cwd() / "transcripts"))
        self.cookies = tk.StringVar()
        self.model = tk.StringVar(value=DEFAULT_MODEL)
        self.device = tk.StringVar(value="cuda")
        self.compute_type = tk.StringVar(value="float16")
        self.status = tk.StringVar(value="Ready")
        self.elapsed = tk.StringVar(value="Elapsed: 00:00")
        self.detail = tk.StringVar(value="")
        self.phase_percent_text = tk.StringVar(value="Current step: --")
        self.total_percent_text = tk.StringVar(value="Overall: 0%")
        self.phase_progress = tk.DoubleVar(value=0)
        self.total_progress = tk.DoubleVar(value=0)
        self.started_at: float | None = None
        self.phase_indeterminate = False

        self._build()
        file_log("GUI started")
        file_log(f"Crash log path: {LOG_PATH}")
        self.root.after(100, self._drain_events)

    def _build(self) -> None:
        self.grid(row=0, column=0, sticky="nsew")
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(0, weight=1)
        self.columnconfigure(1, weight=1)
        self.rowconfigure(9, weight=1)

        ttk.Label(self, text="YouTube URL").grid(row=0, column=0, sticky="w", pady=(0, 8))
        ttk.Entry(self, textvariable=self.url).grid(row=0, column=1, columnspan=2, sticky="ew", pady=(0, 8))

        ttk.Label(self, text="Output folder").grid(row=1, column=0, sticky="w", pady=8)
        ttk.Entry(self, textvariable=self.output_dir).grid(row=1, column=1, sticky="ew", pady=8)
        ttk.Button(self, text="Browse", command=self._choose_output).grid(row=1, column=2, sticky="ew", padx=(8, 0), pady=8)

        ttk.Label(self, text="Cookies file").grid(row=2, column=0, sticky="w", pady=8)
        ttk.Entry(self, textvariable=self.cookies).grid(row=2, column=1, sticky="ew", pady=8)
        ttk.Button(self, text="Browse", command=self._choose_cookies).grid(row=2, column=2, sticky="ew", padx=(8, 0), pady=8)

        options = ttk.Frame(self)
        options.grid(row=3, column=0, columnspan=3, sticky="ew", pady=8)
        for col in range(6):
            options.columnconfigure(col, weight=1)

        ttk.Label(options, text="Model").grid(row=0, column=0, sticky="w")
        ttk.Combobox(
            options,
            textvariable=self.model,
            values=("large-v3-turbo", "large-v3", "medium", "small", "base"),
        ).grid(row=1, column=0, sticky="ew", padx=(0, 8))

        ttk.Label(options, text="Device").grid(row=0, column=1, sticky="w")
        ttk.Combobox(options, textvariable=self.device, values=("cuda", "cpu")).grid(row=1, column=1, sticky="ew", padx=8)

        ttk.Label(options, text="Compute").grid(row=0, column=2, sticky="w")
        ttk.Combobox(
            options,
            textvariable=self.compute_type,
            values=("float16", "int8_float16", "int8"),
        ).grid(row=1, column=2, sticky="ew", padx=8)

        buttons = ttk.Frame(self)
        buttons.grid(row=4, column=0, columnspan=3, sticky="ew", pady=(12, 8))
        buttons.columnconfigure(0, weight=1)
        buttons.columnconfigure(1, weight=1)
        self.start_button = ttk.Button(buttons, text="Transcribe", command=self._start)
        self.start_button.grid(row=0, column=0, sticky="ew", padx=(0, 4), ipady=6)
        self.cancel_button = ttk.Button(buttons, text="Cancel", command=self._cancel, state="disabled")
        self.cancel_button.grid(row=0, column=1, sticky="ew", padx=(4, 0), ipady=6)

        status_bar = ttk.Frame(self)
        status_bar.grid(row=5, column=0, columnspan=3, sticky="ew", pady=(0, 8))
        status_bar.columnconfigure(0, weight=1)
        ttk.Label(status_bar, textvariable=self.status).grid(row=0, column=0, sticky="w")
        ttk.Label(status_bar, textvariable=self.elapsed).grid(row=0, column=1, sticky="e")

        ttk.Label(self, textvariable=self.detail).grid(row=6, column=0, columnspan=3, sticky="w", pady=(0, 4))

        ttk.Label(self, textvariable=self.phase_percent_text).grid(row=7, column=0, sticky="w")
        self.phase_bar = ttk.Progressbar(self, mode="determinate", variable=self.phase_progress, maximum=100)
        self.phase_bar.grid(row=7, column=1, columnspan=2, sticky="ew", pady=(0, 4))

        ttk.Label(self, textvariable=self.total_percent_text).grid(row=8, column=0, sticky="w")
        self.total_bar = ttk.Progressbar(self, mode="determinate", variable=self.total_progress, maximum=100)
        self.total_bar.grid(row=8, column=1, columnspan=2, sticky="ew", pady=(0, 8))

        self.log = tk.Text(self, height=18, wrap="word")
        self.log.grid(row=9, column=0, columnspan=3, sticky="nsew")
        self.log.configure(state="disabled")
        self.log.tag_configure("info", foreground="#1f2937")
        self.log.tag_configure("warning", foreground="#92400e")
        self.log.tag_configure("error", foreground="#991b1b")
        self.log.tag_configure("success", foreground="#166534")

        actions = ttk.Frame(self)
        actions.grid(row=10, column=0, columnspan=3, sticky="ew", pady=(8, 0))
        actions.columnconfigure(0, weight=1)
        ttk.Button(actions, text="Open output folder", command=self._open_output).grid(row=0, column=1, sticky="e")
        self.update_button = ttk.Button(actions, text="Check for updates", command=self._check_update)
        self.update_button.grid(row=0, column=2, sticky="e", padx=(8, 0))

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
        file_log(f"{level.upper()}: {message}")
        self.log.configure(state="normal")
        self.log.insert("end", message.rstrip() + "\n", level)
        self.log.see("end")
        self.log.configure(state="disabled")

    def _set_busy(self, busy: bool) -> None:
        self.start_button.configure(state="disabled" if busy else "normal")
        self.cancel_button.configure(state="normal" if busy else "disabled")
        if not busy:
            self._set_phase_indeterminate(False)
            self.phase_progress.set(0)

    def _start(self) -> None:
        if self.worker and self.worker.is_alive():
            return
        if not self.url.get().strip():
            messagebox.showerror("Missing URL", "Paste a YouTube URL first.")
            return
        issues = check_dependencies()
        if issues:
            messagebox.showerror("Missing dependency", "\n".join(issues))
            return
        cookies = self.cookies.get().strip()
        if cookies and not Path(cookies).exists():
            messagebox.showerror("Cookies not found", f"Cookies file does not exist:\n{cookies}")
            return
        if self.device.get().strip() == "cuda" and not shutil.which("nvidia-smi"):
            self._append_log("nvidia-smi was not found. Continuing anyway, but CUDA may fail if the NVIDIA driver is not installed.")
        if not shutil.which("node"):
            self._append_log("Node.js was not found. Some YouTube videos may fail player challenge solving without it.")
        self.cancel_requested.clear()
        self.started_at = time.monotonic()
        self.phase_progress.set(0)
        self.total_progress.set(0)
        self.phase_percent_text.set("Current step: --")
        self.total_percent_text.set("Overall: 0%")
        self.detail.set("Preparing")
        self._set_busy(True)
        self.status.set("Running")
        self._append_log("Starting transcription")
        self.worker = threading.Thread(target=self._run_job, daemon=True)
        self.worker.start()

    def _cancel(self) -> None:
        self.cancel_requested.set()
        self.status.set("Cancelling")
        self._append_log("Cancel requested")

    def _run_job(self) -> None:
        try:
            file_log("Worker started")
            url = self.url.get().strip()
            output_dir = Path(self.output_dir.get()).expanduser().resolve()
            cookies = self.cookies.get().strip() or None
            model = self.model.get().strip() or DEFAULT_MODEL
            device = self.device.get().strip() or "cuda"
            compute_type = self.compute_type.get().strip() or "float16"

            self.events.put(("log", f"Downloading audio: {url}"))
            self.events.put(("progress", ("download", None, "Starting download")))
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
            self.events.put(("progress", ("download", 100.0, "Audio saved")))
            self.events.put(("log", f"Loading model: {model} on {device} ({compute_type})"))
            self.events.put(("progress", ("model", None, f"Preparing Whisper model {model} on {device}")))

            if device == "cpu":
                text, segments = transcribe_faster(
                    audio_path,
                    model,
                    "transcribe",
                    device,
                    "int8",
                    5,
                    log=lambda message: self.events.put(("log", message)),
                    progress=lambda phase, percent, detail: self.events.put(("progress", (phase, percent, detail))),
                    stop_requested=self.cancel_requested.is_set,
                )
            else:
                text, segments = transcribe_faster(
                    audio_path,
                    model,
                    "transcribe",
                    device,
                    compute_type,
                    5,
                    log=lambda message: self.events.put(("log", message)),
                    progress=lambda phase, percent, detail: self.events.put(("progress", (phase, percent, detail))),
                    stop_requested=self.cancel_requested.is_set,
                )
            if self.cancel_requested.is_set():
                self.events.put(("error", "Cancelled after transcription step."))
                return

            title = safe_name(info.get("title") or audio_path.stem)
            stem = output_dir / title
            self.events.put(("progress", ("write", 0.0, "Writing transcript file")))

            clean_text = " ".join(segment["text"].strip() for segment in segments).strip()
            paragraphs = [p.strip() for p in re.split(r'\n\n+', clean_text) if p.strip()]
            formatted = "\n\n".join(f"## Segment {i+1}\n\n{p}" for i, p in enumerate(paragraphs))
            (stem.with_suffix(".pt.txt")).write_text(formatted, encoding="utf-8")

            try:
                audio_path.unlink(missing_ok=True)
            except Exception:
                pass

            self.events.put(("progress", ("write", 100.0, "Done")))
            self.events.put(("done", [stem.with_suffix(".pt.txt")]))
        except BaseException as exc:
            file_log("WORKER ERROR")
            file_log(traceback.format_exc())
            self.events.put(("error", str(exc)))

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
                elif event == "done":
                    self._set_busy(False)
                    self.status.set("Done")
                    self.phase_progress.set(100)
                    self.total_progress.set(100)
                    self.detail.set("Finished")
                    self._append_log("Finished", "success")
                    for path in payload:
                        self._append_log(str(path), "success")
                    messagebox.showinfo("Done", "Transcript files were created.")
                elif event == "error":
                    self._set_busy(False)
                    self.status.set("Error")
                    self._append_log(str(payload), "error")
                    messagebox.showerror("Error", str(payload))
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
            self.elapsed.set(f"Elapsed: {hours:02}:{minutes:02}:{secs:02}")
        else:
            self.elapsed.set(f"Elapsed: {minutes:02}:{secs:02}")

    def _open_output(self) -> None:
        folder = Path(self.output_dir.get()).expanduser().resolve()
        folder.mkdir(parents=True, exist_ok=True)
        import os

        os.startfile(folder)  # type: ignore[attr-defined]

    def _check_update(self) -> None:
        import subprocess

        self.status.set("Checking for updates...")
        self._append_log("Checking for updates...", "info")

        exe_path = Path(sys.executable).resolve()
        batch_path = exe_path.with_name("update-inplace.bat")

        UPDATE_URL = "https://github.com/jokaperes/yt-transcript/releases/download/v1.4.4/yt-transcript-windows.zip"
        current_tag = "v1.4.4"

        try:
            self.status.set(f"Downloading {current_tag}...")
            self._append_log(f"Downloading {current_tag}...", "info")

            batch_lines = [
                "@echo off",
                'cd /d "%~dp0"',
                "echo Waiting for app to close...",
                'powershell -Command "Start-Sleep -Seconds 3"',
                "echo Downloading update...",
                'powershell -Command "Invoke-WebRequest -Uri \\"%s\\" -OutFile \\"yt-transcript-windows.zip\\""' % UPDATE_URL,
                "echo Extracting...",
                'powershell -Command "Expand-Archive -Path \\"yt-transcript-windows.zip\\" -DestinationPath \\".\\" -Force"',
                "del yt-transcript-windows.zip",
                "echo Done! Restart the app.",
                "pause",
            ]
            batch_content = "\n".join(batch_lines) + "\n"
            batch_path.write_text(batch_content, encoding="utf-8")

            subprocess.Popen(
                ["cmd", "/c", "start", "", str(batch_path)],
                cwd=exe_path.parent,
                shell=True,
            )
            self.root.quit()
            return

        except Exception as exc:
            self.status.set("Update check failed")
            self._append_log(f"Update error: {exc}", "error")


def main() -> None:
    install_crash_handlers()
    try:
        root = tk.Tk()
        root.report_callback_exception = lambda exc, val, tb: (
            file_log("TK CALLBACK ERROR"),
            file_log("".join(traceback.format_exception(exc, val, tb))),
            messagebox.showerror("Error", str(val)),
        )
        App(root)
        root.mainloop()
    except BaseException:
        file_log("FATAL GUI ERROR")
        file_log(traceback.format_exc())
        raise


if __name__ == "__main__":
    main()
