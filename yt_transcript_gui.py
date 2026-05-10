from __future__ import annotations

import json
import queue
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Any

from transcribe_youtube import (
    DEFAULT_MODEL,
    download_audio,
    safe_name,
    transcribe_faster,
    transcribe_openai,
    write_srt,
    write_txt,
    write_vtt,
)


class App(ttk.Frame):
    def __init__(self, root: tk.Tk) -> None:
        super().__init__(root, padding=16)
        self.root = root
        self.root.title("YT Transcript")
        self.root.geometry("860x620")
        self.root.minsize(760, 540)

        self.events: queue.Queue[tuple[str, Any]] = queue.Queue()
        self.worker: threading.Thread | None = None

        self.url = tk.StringVar()
        self.output_dir = tk.StringVar(value=str(Path.cwd() / "transcripts"))
        self.cookies = tk.StringVar()
        self.model = tk.StringVar(value=DEFAULT_MODEL)
        self.device = tk.StringVar(value="cuda")
        self.compute_type = tk.StringVar(value="float16")
        self.status = tk.StringVar(value="Ready")

        self._build()
        self.root.after(100, self._drain_events)

    def _build(self) -> None:
        self.grid(row=0, column=0, sticky="nsew")
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(0, weight=1)
        self.columnconfigure(1, weight=1)
        self.rowconfigure(7, weight=1)

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

        self.start_button = ttk.Button(self, text="Transcribe", command=self._start)
        self.start_button.grid(row=4, column=0, columnspan=3, sticky="ew", pady=(12, 8), ipady=6)

        ttk.Label(self, textvariable=self.status).grid(row=5, column=0, columnspan=3, sticky="w", pady=(0, 8))

        self.progress = ttk.Progressbar(self, mode="indeterminate")
        self.progress.grid(row=6, column=0, columnspan=3, sticky="ew", pady=(0, 8))

        self.log = tk.Text(self, height=18, wrap="word")
        self.log.grid(row=7, column=0, columnspan=3, sticky="nsew")
        self.log.configure(state="disabled")

        actions = ttk.Frame(self)
        actions.grid(row=8, column=0, columnspan=3, sticky="ew", pady=(8, 0))
        actions.columnconfigure(0, weight=1)
        ttk.Button(actions, text="Open output folder", command=self._open_output).grid(row=0, column=1, sticky="e")

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

    def _append_log(self, message: str) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", message.rstrip() + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _set_busy(self, busy: bool) -> None:
        self.start_button.configure(state="disabled" if busy else "normal")
        if busy:
            self.progress.start(10)
        else:
            self.progress.stop()

    def _start(self) -> None:
        if self.worker and self.worker.is_alive():
            return
        if not self.url.get().strip():
            messagebox.showerror("Missing URL", "Paste a YouTube URL first.")
            return
        self._set_busy(True)
        self.status.set("Running")
        self._append_log("Starting transcription")
        self.worker = threading.Thread(target=self._run_job, daemon=True)
        self.worker.start()

    def _run_job(self) -> None:
        try:
            url = self.url.get().strip()
            output_dir = Path(self.output_dir.get()).expanduser().resolve()
            cookies = self.cookies.get().strip() or None
            model = self.model.get().strip() or DEFAULT_MODEL
            device = self.device.get().strip() or "cuda"
            compute_type = self.compute_type.get().strip() or "float16"

            self.events.put(("log", f"Downloading audio: {url}"))
            audio_path, info = download_audio(url, output_dir, cookies, None)
            self.events.put(("log", f"Audio saved: {audio_path}"))
            self.events.put(("log", f"Loading model: {model} on {device} ({compute_type})"))

            if device == "cpu":
                text, segments = transcribe_faster(audio_path, model, "transcribe", device, "int8", 5)
            else:
                text, segments = transcribe_faster(audio_path, model, "transcribe", device, compute_type, 5)

            title = safe_name(info.get("title") or audio_path.stem)
            stem = output_dir / title
            write_txt(stem.with_suffix(".pt.txt"), text)
            write_srt(stem.with_suffix(".pt.srt"), segments)
            write_vtt(stem.with_suffix(".pt.vtt"), segments)
            stem.with_suffix(".pt.json").write_text(
                json.dumps(
                    {
                        "source_url": url,
                        "title": info.get("title"),
                        "audio": str(audio_path),
                        "language": "pt",
                        "model": model,
                        "backend": "faster-whisper",
                        "device": device,
                        "compute_type": "int8" if device == "cpu" else compute_type,
                        "text": text,
                        "segments": segments,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            self.events.put(("done", [stem.with_suffix(".pt.txt"), stem.with_suffix(".pt.srt"), stem.with_suffix(".pt.vtt")]))
        except Exception as exc:
            self.events.put(("error", str(exc)))

    def _drain_events(self) -> None:
        try:
            while True:
                event, payload = self.events.get_nowait()
                if event == "log":
                    self._append_log(str(payload))
                elif event == "done":
                    self._set_busy(False)
                    self.status.set("Done")
                    self._append_log("Finished")
                    for path in payload:
                        self._append_log(str(path))
                    messagebox.showinfo("Done", "Transcript files were created.")
                elif event == "error":
                    self._set_busy(False)
                    self.status.set("Error")
                    self._append_log(str(payload))
                    messagebox.showerror("Error", str(payload))
        except queue.Empty:
            pass
        self.root.after(100, self._drain_events)

    def _open_output(self) -> None:
        folder = Path(self.output_dir.get()).expanduser().resolve()
        folder.mkdir(parents=True, exist_ok=True)
        import os

        os.startfile(folder)  # type: ignore[attr-defined]


def main() -> None:
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
