#!/usr/bin/env python3
"""subvid-gui — Desktop front-end for subvid_batch.py (tkinter, no extra deps).

Reads defaults from config.toml, builds the equivalent command line, streams
the CLI output into a log panel, and can kill the whole process tree with the
Stop button.

Run:  python subvid_gui.py
"""

from __future__ import annotations

import os
import queue
import subprocess
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from subvid_batch import load_config

SCRIPT = Path(__file__).with_name("subvid_batch.py")

MODELS = ["large-v3-turbo", "large-v3", "medium", "small", "base", "tiny"]
LANGUAGES = ["auto", "es", "en", "fr", "de", "pt", "it", "nl", "ru", "ja", "ko", "zh", "ar", "hi", "pl", "tr"]
MODES = ["both", "mux", "sidecar"]
CONTAINERS = ["auto", "mkv", "mp4"]
TASKS = {"Idioma original": "transcribe", "Traducir a inglés": "translate"}


class App:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        root.title("subvid — subtítulos por lotes")
        root.minsize(720, 560)

        cfg = load_config()
        general = cfg.get("general", {})
        vad = cfg.get("vad", {})

        self.process: subprocess.Popen | None = None
        self.log_queue: queue.Queue[str] = queue.Queue()

        main = ttk.Frame(root, padding=10)
        main.pack(fill="both", expand=True)
        main.columnconfigure(0, weight=1)
        main.rowconfigure(2, weight=1)

        # ── Inputs ──
        inputs_frame = ttk.LabelFrame(main, text="Entradas (archivos o carpetas)", padding=8)
        inputs_frame.grid(row=0, column=0, sticky="ew")
        inputs_frame.columnconfigure(0, weight=1)

        self.inputs_list = tk.Listbox(inputs_frame, height=4, selectmode="extended")
        self.inputs_list.grid(row=0, column=0, rowspan=3, sticky="ew", padx=(0, 8))
        ttk.Button(inputs_frame, text="Añadir archivos…", command=self.add_files).grid(row=0, column=1, sticky="ew")
        ttk.Button(inputs_frame, text="Añadir carpeta…", command=self.add_folder).grid(row=1, column=1, sticky="ew")
        ttk.Button(inputs_frame, text="Quitar selección", command=self.remove_selected).grid(row=2, column=1, sticky="ew")

        # ── Options ──
        opts = ttk.LabelFrame(main, text="Opciones", padding=8)
        opts.grid(row=1, column=0, sticky="ew", pady=(8, 0))
        for col in (1, 3):
            opts.columnconfigure(col, weight=1)

        def row(r: int, c: int, label: str, widget: tk.Widget) -> None:
            ttk.Label(opts, text=label).grid(row=r, column=c, sticky="w", padx=(0, 6), pady=2)
            widget.grid(row=r, column=c + 1, sticky="ew", padx=(0, 12), pady=2)

        self.var_output = tk.StringVar(value=general.get("output_dir", ""))
        out_frame = ttk.Frame(opts)
        out_frame.columnconfigure(0, weight=1)
        ttk.Entry(out_frame, textvariable=self.var_output).grid(row=0, column=0, sticky="ew")
        ttk.Button(out_frame, text="…", width=3, command=self.pick_output).grid(row=0, column=1, padx=(4, 0))
        ttk.Label(opts, text="Salida (vacío = junto a cada vídeo)").grid(row=0, column=0, sticky="w", padx=(0, 6), pady=2)
        out_frame.grid(row=0, column=1, columnspan=3, sticky="ew", pady=2)

        self.var_mode = tk.StringVar(value=general.get("mode", "both"))
        row(1, 0, "Modo", ttk.Combobox(opts, textvariable=self.var_mode, values=MODES, state="readonly"))
        self.var_model = tk.StringVar(value=general.get("model", "large-v3-turbo"))
        row(1, 2, "Modelo", ttk.Combobox(opts, textvariable=self.var_model, values=MODELS))

        self.var_language = tk.StringVar(value=general.get("language") or "auto")
        row(2, 0, "Idioma del audio", ttk.Combobox(opts, textvariable=self.var_language, values=LANGUAGES))
        task_value = general.get("task", "transcribe")
        task_label = next((k for k, v in TASKS.items() if v == task_value), "Idioma original")
        self.var_task = tk.StringVar(value=task_label)
        row(2, 2, "Idioma de los subtítulos", ttk.Combobox(opts, textvariable=self.var_task, values=list(TASKS), state="readonly"))

        self.var_container = tk.StringVar(value=general.get("container", "auto"))
        row(3, 0, "Contenedor", ttk.Combobox(opts, textvariable=self.var_container, values=CONTAINERS, state="readonly"))
        self.var_jobs = tk.IntVar(value=int(general.get("jobs", 2)))
        row(3, 2, "Archivos en paralelo", ttk.Spinbox(opts, from_=1, to=16, textvariable=self.var_jobs, width=5))

        tx_langs = cfg.get("translation", {}).get("output_langs", [])
        self.var_to = tk.StringVar(value=",".join(tx_langs) if isinstance(tx_langs, list) else str(tx_langs))
        row(4, 0, "Idiomas extra (ej: es,en)", ttk.Entry(opts, textvariable=self.var_to))

        checks = ttk.Frame(opts)
        checks.grid(row=5, column=0, columnspan=4, sticky="w", pady=(6, 0))
        self.var_recursive = tk.BooleanVar(value=bool(general.get("recursive", True)))
        self.var_overwrite = tk.BooleanVar(value=bool(general.get("overwrite", False)))
        self.var_default_track = tk.BooleanVar(value=bool(general.get("default_track", False)))
        self.var_vad = tk.BooleanVar(value=bool(vad.get("enabled", True)))
        ttk.Checkbutton(checks, text="Recursivo", variable=self.var_recursive).pack(side="left", padx=(0, 12))
        ttk.Checkbutton(checks, text="Sobrescribir", variable=self.var_overwrite).pack(side="left", padx=(0, 12))
        ttk.Checkbutton(checks, text="Pista activa por defecto", variable=self.var_default_track).pack(side="left", padx=(0, 12))
        ttk.Checkbutton(checks, text="Filtro de voz (VAD)", variable=self.var_vad, command=self.sync_vad).pack(side="left")

        vad_frame = ttk.Frame(opts)
        vad_frame.grid(row=6, column=0, columnspan=4, sticky="ew", pady=(6, 0))
        vad_frame.columnconfigure(1, weight=1)
        ttk.Label(vad_frame, text="Umbral VAD (más bajo = más sensible)").grid(row=0, column=0, padx=(0, 8))
        self.var_vad_threshold = tk.DoubleVar(value=float(vad.get("threshold", 0.5)))
        self.vad_scale = ttk.Scale(
            vad_frame, from_=0.1, to=0.9, variable=self.var_vad_threshold,
            command=lambda _v: self.vad_value_label.config(text=f"{self.var_vad_threshold.get():.2f}"),
        )
        self.vad_scale.grid(row=0, column=1, sticky="ew")
        self.vad_value_label = ttk.Label(vad_frame, text=f"{self.var_vad_threshold.get():.2f}", width=5)
        self.vad_value_label.grid(row=0, column=2, padx=(8, 0))

        # ── Log + actions ──
        self.log = tk.Text(main, height=14, state="disabled", wrap="word", font=("Consolas", 9))
        self.log.grid(row=2, column=0, sticky="nsew", pady=(8, 0))

        actions = ttk.Frame(main)
        actions.grid(row=3, column=0, sticky="ew", pady=(8, 0))
        actions.columnconfigure(0, weight=1)
        self.status = ttk.Label(actions, text="Listo.")
        self.status.grid(row=0, column=0, sticky="w")
        self.start_btn = ttk.Button(actions, text="▶ Iniciar", command=self.start)
        self.start_btn.grid(row=0, column=1, padx=(0, 6))
        self.stop_btn = ttk.Button(actions, text="■ Detener", command=self.stop, state="disabled")
        self.stop_btn.grid(row=0, column=2)

        root.protocol("WM_DELETE_WINDOW", self.on_close)
        root.after(100, self.drain_log)

    # ── Input pickers ──
    def add_files(self) -> None:
        for f in filedialog.askopenfilenames(title="Elige vídeos o audios"):
            self.inputs_list.insert("end", f)

    def add_folder(self) -> None:
        folder = filedialog.askdirectory(title="Elige una carpeta")
        if folder:
            self.inputs_list.insert("end", folder)

    def remove_selected(self) -> None:
        for index in reversed(self.inputs_list.curselection()):
            self.inputs_list.delete(index)

    def pick_output(self) -> None:
        folder = filedialog.askdirectory(title="Carpeta de salida")
        if folder:
            self.var_output.set(folder)

    def sync_vad(self) -> None:
        state = "normal" if self.var_vad.get() else "disabled"
        self.vad_scale.config(state=state)

    # ── Run control ──
    def build_command(self) -> list[str]:
        cmd = [sys.executable, "-u", str(SCRIPT), *self.inputs_list.get(0, "end")]
        cmd += ["--mode", self.var_mode.get(), "-m", self.var_model.get().strip()]
        cmd += ["--container", self.var_container.get(), "-j", str(self.var_jobs.get())]
        cmd += ["--task", TASKS[self.var_task.get()]]
        language = self.var_language.get().strip()
        if language and language != "auto":
            cmd += ["-l", language]
        to_langs = self.var_to.get().strip()
        if to_langs:
            cmd += ["--to", to_langs]
        if self.var_output.get().strip():
            cmd += ["-o", self.var_output.get().strip()]
        if not self.var_recursive.get():
            cmd.append("--no-recursive")
        if self.var_overwrite.get():
            cmd.append("--overwrite")
        if self.var_default_track.get():
            cmd.append("--default-track")
        if self.var_vad.get():
            cmd += ["--vad-threshold", f"{self.var_vad_threshold.get():.2f}"]
        else:
            cmd.append("--no-vad")
        return cmd

    def start(self) -> None:
        if not self.inputs_list.size():
            messagebox.showwarning("subvid", "Añade al menos un archivo o carpeta.")
            return
        cmd = self.build_command()
        self.append_log("$ " + " ".join(cmd[2:]) + "\n\n")
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
        try:
            self.process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                creationflags=creationflags,
            )
        except OSError as e:
            messagebox.showerror("subvid", f"No se pudo lanzar el proceso: {e}")
            return
        self.start_btn.config(state="disabled")
        self.stop_btn.config(state="normal")
        self.status.config(text="Procesando…")
        threading.Thread(target=self.read_output, daemon=True).start()

    def read_output(self) -> None:
        proc = self.process
        if not proc or not proc.stdout:
            return
        for line in proc.stdout:
            self.log_queue.put(line)
        code = proc.wait()
        self.log_queue.put(f"\n[proceso terminado con código {code}]\n")
        self.log_queue.put("__DONE__")

    def stop(self) -> None:
        proc = self.process
        if not proc or proc.poll() is not None:
            return
        self.append_log("\n[deteniendo el proceso…]\n")
        if os.name == "nt":
            # Kill the whole tree: the CLI spawns ffmpeg children and native
            # transcription threads that don't react to a polite terminate.
            subprocess.run(
                ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                capture_output=True,
            )
        else:
            proc.kill()

    def drain_log(self) -> None:
        try:
            while True:
                line = self.log_queue.get_nowait()
                if line == "__DONE__":
                    self.start_btn.config(state="normal")
                    self.stop_btn.config(state="disabled")
                    self.status.config(text="Listo.")
                else:
                    self.append_log(line)
        except queue.Empty:
            pass
        self.root.after(100, self.drain_log)

    def append_log(self, text: str) -> None:
        self.log.config(state="normal")
        self.log.insert("end", text)
        self.log.see("end")
        self.log.config(state="disabled")

    def on_close(self) -> None:
        if self.process and self.process.poll() is None:
            if not messagebox.askyesno("subvid", "Hay un proceso en marcha. ¿Detenerlo y salir?"):
                return
            self.stop()
        self.root.destroy()


def main() -> None:
    root = tk.Tk()
    try:
        root.call("tk", "scaling", 1.25)
    except tk.TclError:
        pass
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
