#!/usr/bin/env python3
"""Aplikasi GUI untuk memotong dan menggabungkan video menggunakan FFmpeg."""

import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext, ttk

from ffmpeg_setup import install_ffmpeg, resolve_ffmpeg


def format_duration(seconds: float) -> str:
    """Konversi detik ke format HH:MM:SS."""
    total = max(0, int(round(seconds)))
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


MAP_STREAM_OPTIONS: list[dict[str, object]] = [
    {
        "label": "Semua stream (disarankan)",
        "maps": ["0"],
        "desc": "Mengambil video, audio, subtitle, dan stream lain dari file. Pilihan paling aman untuk kebanyakan video.",
    },
    {
        "label": "Video + audio pertama",
        "maps": ["0:v:0", "0:a:0"],
        "desc": "Hanya video dan trek audio pertama. Cocok jika file punya banyak audio dan Anda hanya butuh satu.",
    },
    {
        "label": "Video + semua audio",
        "maps": ["0:v", "0:a"],
        "desc": "Video beserta semua trek audio, tanpa subtitle dan stream lain.",
    },
    {
        "label": "Video + audio + subtitle",
        "maps": ["0:v", "0:a", "0:s"],
        "desc": "Video, semua audio, dan subtitle. Tanpa metadata atau stream tambahan lainnya.",
    },
    {
        "label": "Hanya video",
        "maps": ["0:v"],
        "desc": "Hanya gambar video, tanpa suara dan tanpa subtitle. Hasil file akan bisu.",
    },
    {
        "label": "Hanya audio pertama",
        "maps": ["0:a:0"],
        "desc": "Hanya suara dari trek audio pertama. Berguna jika ingin ekstrak audio saja.",
    },
    {
        "label": "Semua audio",
        "maps": ["0:a"],
        "desc": "Semua trek audio dari file, tanpa video. Cocok untuk ekstrak semua versi audio.",
    },
]


def parse_duration(text: str) -> float | None:
    """Parse durasi dari detik atau HH:MM:SS."""
    text = text.strip()
    if not text:
        return None

    if re.fullmatch(r"\d+(\.\d+)?", text):
        return float(text)

    match = re.fullmatch(r"(\d{1,2}):(\d{2}):(\d{2})", text)
    if match:
        h, m, s = map(int, match.groups())
        if m >= 60 or s >= 60:
            return None
        return h * 3600 + m * 60 + s

    return None


def natural_sort_key(name: str) -> list[object]:
    return [int(part) if part.isdigit() else part.lower() for part in re.split(r"(\d+)", name)]


def escape_concat_path(path: str) -> str:
    return os.path.abspath(path).replace("\\", "/").replace("'", "'\\''")


def collect_video_files(folder: str, pattern: str) -> list[str]:
    matched = [
        os.path.join(folder, name)
        for name in os.listdir(folder)
        if os.path.isfile(os.path.join(folder, name)) and _match_pattern(name, pattern)
    ]
    matched.sort(key=lambda path: natural_sort_key(os.path.basename(path)))
    return matched


def _match_pattern(filename: str, pattern: str) -> bool:
    return re.fullmatch(pattern.replace(".", r"\.").replace("*", ".*").replace("?", "."), filename, re.IGNORECASE) is not None


class VideoSplitterApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("Aplikasi Potong & Gabung Video")
        self.root.minsize(520, 480)

        self.process: subprocess.Popen | None = None
        self.install_thread: threading.Thread | None = None
        self._operation_mode = "split"
        self._merge_files: list[str] = []
        self._concat_list_path = ""
        self._wrap_labels: list[tk.Widget] = []
        self._last_wrap_width = 0

        self._build_ui()
        self._setup_responsive_layout()
        self._check_ffmpeg()

    def _setup_responsive_layout(self) -> None:
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(0, weight=1)
        self.main_frame.columnconfigure(0, weight=1)
        self.main_frame.rowconfigure(0, weight=1)
        self.main_frame.rowconfigure(1, weight=2)
        self.content_canvas.bind("<Configure>", self._on_canvas_configure)
        self.scrollable_frame.bind("<Configure>", self._on_scrollable_configure)
        self.content_canvas.bind("<Enter>", self._bind_mousewheel)
        self.content_canvas.bind("<Leave>", self._unbind_mousewheel)
        self.root.bind("<Configure>", self._on_root_configure)
        self._fit_window_to_screen()

    def _fit_window_to_screen(self) -> None:
        self.root.update_idletasks()
        screen_w = self.root.winfo_screenwidth()
        screen_h = self.root.winfo_screenheight()
        width = min(920, max(560, int(screen_w * 0.82)))
        height = min(820, max(520, int(screen_h * 0.82)))
        pos_x = max(0, (screen_w - width) // 2)
        pos_y = max(0, (screen_h - height) // 2)
        self.root.geometry(f"{width}x{height}+{pos_x}+{pos_y}")

    def _bind_mousewheel(self, _event: object = None) -> None:
        self.root.bind_all("<MouseWheel>", self._on_mousewheel)

    def _unbind_mousewheel(self, _event: object = None) -> None:
        self.root.unbind_all("<MouseWheel>")

    def _on_mousewheel(self, event: tk.Event) -> None:
        if self.content_canvas.winfo_height() < self.scrollable_frame.winfo_reqheight():
            self.content_canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

    def _on_canvas_configure(self, event: tk.Event) -> None:
        self.content_canvas.itemconfigure(self.scrollable_window, width=event.width)

    def _on_scrollable_configure(self, _event: object = None) -> None:
        self.content_canvas.configure(scrollregion=self.content_canvas.bbox("all"))

    def _on_root_configure(self, event: tk.Event) -> None:
        if event.widget is not self.root:
            return
        wrap_width = max(240, event.width - 72)
        if abs(wrap_width - self._last_wrap_width) < 12:
            return
        self._last_wrap_width = wrap_width
        for widget in self._wrap_labels:
            widget.configure(wraplength=wrap_width)

    def _add_wrapped_label(self, parent: tk.Misc, **kwargs: object) -> ttk.Label:
        label = ttk.Label(parent, **kwargs)
        self._wrap_labels.append(label)
        return label

    def _build_ui(self) -> None:
        self.main_frame = ttk.Frame(self.root, padding=10)
        self.main_frame.grid(row=0, column=0, sticky="nsew")

        content_outer = ttk.Frame(self.main_frame)
        content_outer.grid(row=0, column=0, sticky="nsew")
        content_outer.columnconfigure(0, weight=1)
        content_outer.rowconfigure(0, weight=1)

        self.content_canvas = tk.Canvas(content_outer, highlightthickness=0, borderwidth=0)
        content_scroll = ttk.Scrollbar(content_outer, orient=tk.VERTICAL, command=self.content_canvas.yview)
        self.content_canvas.configure(yscrollcommand=content_scroll.set)
        self.content_canvas.grid(row=0, column=0, sticky="nsew")
        content_scroll.grid(row=0, column=1, sticky="ns")

        self.scrollable_frame = ttk.Frame(self.content_canvas)
        self.scrollable_window = self.content_canvas.create_window(
            (0, 0), window=self.scrollable_frame, anchor="nw"
        )

        main = self.scrollable_frame
        main.columnconfigure(0, weight=1)

        # --- FFmpeg ---
        ffmpeg_frame = ttk.LabelFrame(main, text="FFmpeg", padding=8)
        ffmpeg_frame.pack(fill=tk.X, pady=(0, 8))
        ffmpeg_frame.columnconfigure(0, weight=1)

        self.ffmpeg_status_var = tk.StringVar()
        self._add_wrapped_label(ffmpeg_frame, textvariable=self.ffmpeg_status_var).grid(
            row=0, column=0, sticky="ew"
        )

        ffmpeg_action = ttk.Frame(ffmpeg_frame)
        ffmpeg_action.grid(row=1, column=0, sticky="ew", pady=(6, 0))
        ffmpeg_action.columnconfigure(0, weight=1)

        self.install_ffmpeg_btn = ttk.Button(
            ffmpeg_action,
            text="Unduh & Pasang FFmpeg Otomatis",
            command=self._offer_install_ffmpeg,
        )
        self.install_ffmpeg_btn.grid(row=0, column=0, sticky="w")

        self.ffmpeg_progress = ttk.Progressbar(ffmpeg_frame, mode="determinate", maximum=100)
        self.ffmpeg_progress.grid(row=2, column=0, sticky="ew", pady=(8, 0))

        notebook = ttk.Notebook(main)
        notebook.pack(fill=tk.X, pady=(0, 4))

        split_tab = ttk.Frame(notebook, padding=4)
        merge_tab = ttk.Frame(notebook, padding=4)
        notebook.add(split_tab, text="Potong Video")
        notebook.add(merge_tab, text="Gabung Video")

        self._build_split_tab(split_tab)
        self._build_merge_tab(merge_tab)

        # --- Log ---
        log_frame = ttk.LabelFrame(self.main_frame, text="Log / Perintah FFmpeg", padding=8)
        log_frame.grid(row=1, column=0, sticky="nsew", pady=(8, 0))
        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(0, weight=1)

        self.log_text = scrolledtext.ScrolledText(log_frame, height=8, state=tk.DISABLED, wrap=tk.WORD)
        self.log_text.grid(row=0, column=0, sticky="nsew")

    def _build_split_tab(self, main: ttk.Frame) -> None:
        main.columnconfigure(0, weight=1)

        # --- Input ---
        input_frame = ttk.LabelFrame(main, text="File Video", padding=8)
        input_frame.pack(fill=tk.X, pady=(0, 8))
        input_frame.columnconfigure(0, weight=1)

        self.input_var = tk.StringVar()
        ttk.Entry(input_frame, textvariable=self.input_var).grid(row=0, column=0, sticky="ew", padx=(0, 8))
        ttk.Button(input_frame, text="Pilih File", command=self._pick_input).grid(row=0, column=1, sticky="e")

        # --- Output ---
        output_frame = ttk.LabelFrame(main, text="Folder Output", padding=8)
        output_frame.pack(fill=tk.X, pady=(0, 8))
        output_frame.columnconfigure(0, weight=1)

        self.output_dir_var = tk.StringVar()
        ttk.Entry(output_frame, textvariable=self.output_dir_var).grid(
            row=0, column=0, sticky="ew", padx=(0, 8)
        )
        ttk.Button(output_frame, text="Pilih Folder", command=self._pick_output).grid(
            row=0, column=1, sticky="e"
        )

        # --- Pengaturan ---
        settings = ttk.LabelFrame(main, text="Pengaturan Potong", padding=8)
        settings.pack(fill=tk.X, pady=(0, 8))
        settings.columnconfigure(0, weight=1)

        grid = ttk.Frame(settings)
        grid.pack(fill=tk.X)
        grid.columnconfigure(0, weight=0, minsize=118)
        grid.columnconfigure(1, weight=1)

        ttk.Label(grid, text="Durasi per segmen:").grid(row=0, column=0, sticky=tk.W, pady=3)
        duration_frame = ttk.Frame(grid)
        duration_frame.grid(row=0, column=1, sticky="ew", pady=3, padx=(8, 0))
        duration_frame.columnconfigure(1, weight=1)

        self.duration_var = tk.StringVar(value="3")
        ttk.Entry(duration_frame, textvariable=self.duration_var, width=8).grid(row=0, column=0, sticky=tk.W)
        ttk.Label(duration_frame, text="detik (atau HH:MM:SS)").grid(row=0, column=1, sticky=tk.W, padx=(6, 0))

        ttk.Label(grid, text="Nama file output:").grid(row=1, column=0, sticky=tk.W, pady=3)
        self.pattern_var = tk.StringVar(value="output%03d.mp4")
        ttk.Entry(grid, textvariable=self.pattern_var).grid(
            row=1, column=1, sticky="ew", pady=3, padx=(8, 0)
        )

        ttk.Label(grid, text="Mode codec:").grid(row=2, column=0, sticky=tk.NW, pady=3)
        codec_frame = ttk.Frame(grid)
        codec_frame.grid(row=2, column=1, sticky="ew", pady=3, padx=(8, 0))

        self.codec_mode_var = tk.StringVar(value="copy")
        ttk.Radiobutton(
            codec_frame, text="Copy (cepat, tanpa re-encode)", value="copy",
            variable=self.codec_mode_var, command=self._toggle_codec_fields,
        ).pack(anchor=tk.W)
        ttk.Radiobutton(
            codec_frame, text="Re-encode", value="reencode",
            variable=self.codec_mode_var, command=self._toggle_codec_fields,
        ).pack(anchor=tk.W)

        ttk.Label(grid, text="Video codec:").grid(row=3, column=0, sticky=tk.W, pady=3)
        self.video_codec_var = tk.StringVar(value="libx264")
        self.video_codec_entry = ttk.Entry(grid, textvariable=self.video_codec_var)
        self.video_codec_entry.grid(row=3, column=1, sticky="ew", pady=3, padx=(8, 0))

        ttk.Label(grid, text="Audio codec:").grid(row=4, column=0, sticky=tk.W, pady=3)
        self.audio_codec_var = tk.StringVar(value="aac")
        self.audio_codec_entry = ttk.Entry(grid, textvariable=self.audio_codec_var)
        self.audio_codec_entry.grid(row=4, column=1, sticky="ew", pady=3, padx=(8, 0))

        ttk.Label(grid, text="Map stream:").grid(row=5, column=0, sticky=tk.NW, pady=3)

        map_frame = ttk.Frame(grid)
        map_frame.grid(row=5, column=1, sticky="ew", pady=3, padx=(8, 0))
        map_frame.columnconfigure(0, weight=1)

        map_labels = [opt["label"] for opt in MAP_STREAM_OPTIONS]
        self.map_choice_var = tk.StringVar(value=map_labels[0])
        self.map_combo = ttk.Combobox(
            map_frame,
            textvariable=self.map_choice_var,
            values=map_labels,
            state="readonly",
        )
        self.map_combo.grid(row=0, column=0, sticky="ew")
        self.map_combo.bind("<<ComboboxSelected>>", self._on_map_changed)

        self.map_desc_var = tk.StringVar()
        self._add_wrapped_label(
            map_frame,
            textvariable=self.map_desc_var,
            foreground="#444444",
        ).grid(row=1, column=0, sticky="ew", pady=(4, 0))
        self._on_map_changed()

        options_frame = ttk.Frame(settings)
        options_frame.pack(fill=tk.X, pady=(6, 0))

        self.reset_timestamps_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            options_frame,
            text="Reset timestamps (reset_timestamps 1)",
            variable=self.reset_timestamps_var,
        ).pack(anchor=tk.W)

        self.segment_at_clock_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            options_frame,
            text="Potong tepat di batas waktu (segment_atclocktime 1)",
            variable=self.segment_at_clock_var,
        ).pack(anchor=tk.W)

        ttk.Label(settings, text="Opsi FFmpeg tambahan (opsional):").pack(anchor=tk.W, pady=(6, 2))
        self.extra_args_var = tk.StringVar()
        ttk.Entry(settings, textvariable=self.extra_args_var).pack(fill=tk.X)

        self._toggle_codec_fields()

        action_frame = ttk.Frame(main)
        action_frame.pack(fill=tk.X, pady=(0, 4))
        action_frame.columnconfigure(2, weight=1)

        self.start_btn = ttk.Button(action_frame, text="Mulai Potong", command=self._start_split)
        self.start_btn.grid(row=0, column=0, sticky="w")

        self.stop_btn = ttk.Button(
            action_frame, text="Batalkan", command=self._stop, state=tk.DISABLED
        )
        self.stop_btn.grid(row=0, column=1, sticky="w", padx=(8, 0))

        self.status_var = tk.StringVar(value="Siap")
        self._add_wrapped_label(action_frame, textvariable=self.status_var).grid(
            row=0, column=2, sticky="w", padx=(12, 0)
        )

    def _build_merge_tab(self, main: ttk.Frame) -> None:
        main.columnconfigure(0, weight=1)

        source_frame = ttk.LabelFrame(main, text="Sumber Segmen Video", padding=8)
        source_frame.pack(fill=tk.X, pady=(0, 8))
        source_frame.columnconfigure(0, weight=1)

        self.merge_folder_var = tk.StringVar()
        ttk.Entry(source_frame, textvariable=self.merge_folder_var).grid(
            row=0, column=0, sticky="ew", padx=(0, 8)
        )
        folder_btns = ttk.Frame(source_frame)
        folder_btns.grid(row=0, column=1, sticky="e")
        ttk.Button(folder_btns, text="Pilih Folder", command=self._pick_merge_folder).grid(row=0, column=0)
        ttk.Button(folder_btns, text="Dari Output Potong", command=self._use_split_output_folder).grid(
            row=0, column=1, padx=(6, 0)
        )

        filter_frame = ttk.Frame(source_frame)
        filter_frame.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        filter_frame.columnconfigure(1, weight=1)

        ttk.Label(filter_frame, text="Filter nama file:").grid(row=0, column=0, sticky=tk.W)
        self.merge_pattern_var = tk.StringVar(value="output*.mp4")
        ttk.Entry(filter_frame, textvariable=self.merge_pattern_var).grid(
            row=0, column=1, sticky="ew", padx=(8, 8)
        )
        ttk.Button(filter_frame, text="Scan File", command=self._scan_merge_files).grid(row=0, column=2)

        self.merge_count_var = tk.StringVar(value="Belum ada file segmen dipilih.")
        self._add_wrapped_label(
            source_frame,
            textvariable=self.merge_count_var,
            foreground="#444444",
        ).grid(row=2, column=0, columnspan=2, sticky="ew", pady=(8, 0))

        output_frame = ttk.LabelFrame(main, text="File Output Gabungan", padding=8)
        output_frame.pack(fill=tk.X, pady=(0, 8))
        output_frame.columnconfigure(0, weight=1)

        self.merge_output_var = tk.StringVar(value="gabung.mp4")
        ttk.Entry(output_frame, textvariable=self.merge_output_var).grid(
            row=0, column=0, sticky="ew", padx=(0, 8)
        )
        ttk.Button(output_frame, text="Simpan Sebagai", command=self._pick_merge_output).grid(
            row=0, column=1, sticky="e"
        )

        settings = ttk.LabelFrame(main, text="Pengaturan Gabung", padding=8)
        settings.pack(fill=tk.X, pady=(0, 8))
        settings.columnconfigure(1, weight=1)

        ttk.Label(settings, text="Mode codec:").grid(row=0, column=0, sticky=tk.NW, pady=3)
        merge_codec_frame = ttk.Frame(settings)
        merge_codec_frame.grid(row=0, column=1, sticky="ew", pady=3, padx=(8, 0))

        self.merge_codec_mode_var = tk.StringVar(value="copy")
        ttk.Radiobutton(
            merge_codec_frame,
            text="Copy (cepat, disarankan untuk segmen hasil potong)",
            value="copy",
            variable=self.merge_codec_mode_var,
            command=self._toggle_merge_codec_fields,
        ).pack(anchor=tk.W)
        ttk.Radiobutton(
            merge_codec_frame,
            text="Re-encode",
            value="reencode",
            variable=self.merge_codec_mode_var,
            command=self._toggle_merge_codec_fields,
        ).pack(anchor=tk.W)

        ttk.Label(settings, text="Video codec:").grid(row=1, column=0, sticky=tk.W, pady=3)
        self.merge_video_codec_var = tk.StringVar(value="libx264")
        self.merge_video_codec_entry = ttk.Entry(settings, textvariable=self.merge_video_codec_var)
        self.merge_video_codec_entry.grid(row=1, column=1, sticky="ew", pady=3, padx=(8, 0))

        ttk.Label(settings, text="Audio codec:").grid(row=2, column=0, sticky=tk.W, pady=3)
        self.merge_audio_codec_var = tk.StringVar(value="aac")
        self.merge_audio_codec_entry = ttk.Entry(settings, textvariable=self.merge_audio_codec_var)
        self.merge_audio_codec_entry.grid(row=2, column=1, sticky="ew", pady=3, padx=(8, 0))

        self._add_wrapped_label(
            settings,
            text="Segmen akan digabung sesuai urutan nama file (001, 002, 003, ...).",
            foreground="#444444",
        ).grid(row=3, column=0, columnspan=2, sticky="ew", pady=(6, 0))

        self._toggle_merge_codec_fields()

        action_frame = ttk.Frame(main)
        action_frame.pack(fill=tk.X, pady=(0, 4))

        self.merge_btn = ttk.Button(action_frame, text="Gabung Video", command=self._start_merge)
        self.merge_btn.pack(side=tk.LEFT)

    def _toggle_codec_fields(self) -> None:
        state = tk.NORMAL if self.codec_mode_var.get() == "reencode" else tk.DISABLED
        self.video_codec_entry.configure(state=state)
        self.audio_codec_entry.configure(state=state)

    def _toggle_merge_codec_fields(self) -> None:
        state = tk.NORMAL if self.merge_codec_mode_var.get() == "reencode" else tk.DISABLED
        self.merge_video_codec_entry.configure(state=state)
        self.merge_audio_codec_entry.configure(state=state)

    def _get_map_option(self) -> dict[str, object]:
        label = self.map_choice_var.get()
        for option in MAP_STREAM_OPTIONS:
            if option["label"] == label:
                return option
        return MAP_STREAM_OPTIONS[0]

    def _on_map_changed(self, _event: object = None) -> None:
        option = self._get_map_option()
        maps = option["maps"]
        map_cmd = " ".join(f"-map {m}" for m in maps)
        self.map_desc_var.set(f"{option['desc']}  (FFmpeg: {map_cmd})")

    def _check_ffmpeg(self) -> None:
        ffmpeg_path = resolve_ffmpeg()
        if ffmpeg_path:
            self.ffmpeg_status_var.set(f"FFmpeg siap: {ffmpeg_path}")
            self.install_ffmpeg_btn.configure(state=tk.DISABLED)
            self._log("FFmpeg ditemukan.\n")
            return

        self.ffmpeg_status_var.set(
            "FFmpeg belum terpasang. Klik tombol di bawah untuk unduh otomatis ke C:\\ffmpeg "
            "dan menambahkan C:\\ffmpeg\\bin ke PATH."
        )
        self._log("PERINGATAN: FFmpeg tidak ditemukan di PATH maupun C:\\ffmpeg\\bin.\n")
        if messagebox.askyesno(
            "FFmpeg Tidak Ditemukan",
            "FFmpeg belum terpasang di komputer ini.\n\n"
            "Ingin unduh dan pasang otomatis?\n\n"
            "• Lokasi: C:\\ffmpeg\n"
            "• PATH ditambah: ;C:\\ffmpeg\\bin\n"
            "• Ukuran unduhan: ~80 MB",
        ):
            self._start_install_ffmpeg()

    def _offer_install_ffmpeg(self) -> None:
        if resolve_ffmpeg():
            messagebox.showinfo("FFmpeg", "FFmpeg sudah terpasang.")
            self._check_ffmpeg()
            return

        if messagebox.askyesno(
            "Pasang FFmpeg",
            "Aplikasi akan:\n"
            "1. Mengunduh FFmpeg untuk Windows\n"
            "2. Memasang ke C:\\ffmpeg\n"
            "3. Menambahkan C:\\ffmpeg\\bin ke PATH Environment Variables\n\n"
            "Lanjutkan?",
        ):
            self._start_install_ffmpeg()

    def _start_install_ffmpeg(self) -> None:
        if self.install_thread and self.install_thread.is_alive():
            return

        self.install_ffmpeg_btn.configure(state=tk.DISABLED)
        self.start_btn.configure(state=tk.DISABLED)
        self.merge_btn.configure(state=tk.DISABLED)
        self.ffmpeg_progress.configure(value=0)
        self.status_var.set("Mengunduh FFmpeg...")
        self._log("\nMemulai instalasi FFmpeg otomatis...\n")

        self.install_thread = threading.Thread(target=self._run_install_ffmpeg, daemon=True)
        self.install_thread.start()

    def _run_install_ffmpeg(self) -> None:
        try:
            install_ffmpeg(
                log=lambda text: self.root.after(0, self._log, text),
                progress=lambda done, total: self.root.after(0, self._update_download_progress, done, total),
            )
            self.root.after(0, self._on_install_success)
        except Exception as exc:
            self.root.after(0, self._on_install_error, str(exc))

    def _update_download_progress(self, downloaded: int, total: int) -> None:
        if total <= 0:
            self.ffmpeg_progress.configure(mode="indeterminate")
            self.ffmpeg_progress.start(10)
            return

        self.ffmpeg_progress.stop()
        self.ffmpeg_progress.configure(mode="determinate", maximum=total, value=downloaded)

    def _on_install_success(self) -> None:
        self.ffmpeg_progress.stop()
        self.ffmpeg_progress.configure(mode="determinate", value=self.ffmpeg_progress["maximum"])
        self.start_btn.configure(state=tk.NORMAL)
        self.merge_btn.configure(state=tk.NORMAL)
        self._check_ffmpeg()
        self.status_var.set("Siap")
        messagebox.showinfo(
            "FFmpeg Terpasang",
            "FFmpeg berhasil dipasang di C:\\ffmpeg\n"
            "dan C:\\ffmpeg\\bin sudah ditambahkan ke PATH.\n\n"
            "Jika terminal/aplikasi lain masih belum mengenali FFmpeg, "
            "tutup lalu buka kembali aplikasi tersebut.",
        )

    def _on_install_error(self, message: str) -> None:
        self.ffmpeg_progress.stop()
        self.ffmpeg_progress.configure(value=0)
        self.install_ffmpeg_btn.configure(state=tk.NORMAL)
        self.start_btn.configure(state=tk.NORMAL)
        self.merge_btn.configure(state=tk.NORMAL)
        self.status_var.set("Siap")
        self._log(f"\nInstalasi FFmpeg gagal: {message}\n")
        messagebox.showerror(
            "Instalasi Gagal",
            f"Gagal memasang FFmpeg:\n{message}\n\n"
            "Jika gagal menulis ke C:\\ffmpeg, coba jalankan aplikasi "
            "klik kanan -> Run as administrator.",
        )

    def _pick_input(self) -> None:
        path = filedialog.askopenfilename(
            title="Pilih File Video",
            filetypes=[
                ("Video", "*.mp4 *.mkv *.avi *.mov *.wmv *.flv *.webm *.m4v *.ts"),
                ("Semua file", "*.*"),
            ],
        )
        if path:
            self.input_var.set(path)
            if not self.output_dir_var.get():
                self.output_dir_var.set(os.path.dirname(path))

    def _pick_output(self) -> None:
        path = filedialog.askdirectory(title="Pilih Folder Output")
        if path:
            self.output_dir_var.set(path)

    def _pick_merge_folder(self) -> None:
        path = filedialog.askdirectory(title="Pilih Folder Segmen Video")
        if path:
            self.merge_folder_var.set(path)
            if not os.path.isabs(self.merge_output_var.get()):
                self.merge_output_var.set(os.path.join(path, "gabung.mp4"))
            self._scan_merge_files()

    def _use_split_output_folder(self) -> None:
        folder = self.output_dir_var.get().strip()
        if not folder:
            messagebox.showinfo("Info", "Folder output potong masih kosong.\nAtur dulu di tab Potong Video.")
            return
        self.merge_folder_var.set(folder)
        pattern = self.pattern_var.get().strip()
        if "%" in pattern:
            merge_pattern = pattern.split("%", 1)[0] + "*" + os.path.splitext(pattern)[1]
            self.merge_pattern_var.set(merge_pattern)
        if not os.path.isabs(self.merge_output_var.get()):
            self.merge_output_var.set(os.path.join(folder, "gabung.mp4"))
        self._scan_merge_files()

    def _pick_merge_output(self) -> None:
        folder = self.merge_folder_var.get().strip() or self.output_dir_var.get().strip() or os.getcwd()
        path = filedialog.asksaveasfilename(
            title="Simpan Video Gabungan",
            initialdir=folder,
            initialfile="gabung.mp4",
            defaultextension=".mp4",
            filetypes=[
                ("MP4", "*.mp4"),
                ("MKV", "*.mkv"),
                ("AVI", "*.avi"),
                ("Semua file", "*.*"),
            ],
        )
        if path:
            self.merge_output_var.set(path)

    def _scan_merge_files(self) -> bool:
        folder = self.merge_folder_var.get().strip()
        pattern = self.merge_pattern_var.get().strip() or "*.*"

        if not folder:
            messagebox.showerror("Error", "Pilih folder segmen video terlebih dahulu.")
            return False
        if not os.path.isdir(folder):
            messagebox.showerror("Error", f"Folder tidak ditemukan:\n{folder}")
            return False

        self._merge_files = collect_video_files(folder, pattern)
        count = len(self._merge_files)

        if count == 0:
            self.merge_count_var.set(f"Tidak ada file yang cocok dengan filter '{pattern}'.")
            return False

        preview_names = ", ".join(os.path.basename(path) for path in self._merge_files[:5])
        if count > 5:
            preview_names += f", ... (+{count - 5} file lagi)"
        self.merge_count_var.set(f"{count} file siap digabung: {preview_names}")
        return True

    def _log(self, text: str) -> None:
        self.log_text.configure(state=tk.NORMAL)
        self.log_text.insert(tk.END, text)
        self.log_text.see(tk.END)
        self.log_text.configure(state=tk.DISABLED)

    def _validate(self) -> list[str] | None:
        input_path = self.input_var.get().strip()
        output_dir = self.output_dir_var.get().strip()
        pattern = self.pattern_var.get().strip()
        duration = parse_duration(self.duration_var.get())

        if not input_path:
            messagebox.showerror("Error", "Pilih file video terlebih dahulu.")
            return None
        if not os.path.isfile(input_path):
            messagebox.showerror("Error", f"File tidak ditemukan:\n{input_path}")
            return None
        if not output_dir:
            messagebox.showerror("Error", "Pilih folder output terlebih dahulu.")
            return None
        if not os.path.isdir(output_dir):
            messagebox.showerror("Error", f"Folder output tidak ditemukan:\n{output_dir}")
            return None
        if not pattern:
            messagebox.showerror("Error", "Nama file output tidak boleh kosong.")
            return None
        if "%" not in pattern:
            messagebox.showerror(
                "Error",
                "Nama file output harus mengandung placeholder numerik,\n"
                "contoh: output%03d.mp4",
            )
            return None
        if duration is None or duration <= 0:
            messagebox.showerror(
                "Error",
                "Durasi segmen tidak valid.\nGunakan detik (contoh: 3) atau HH:MM:SS (contoh: 00:00:03).",
            )
            return None

        if not resolve_ffmpeg():
            messagebox.showerror(
                "FFmpeg Tidak Ditemukan",
                "FFmpeg belum terpasang.\n\n"
                "Klik 'Unduh & Pasang FFmpeg Otomatis' terlebih dahulu.",
            )
            return None

        return self._build_command(input_path, output_dir, pattern, duration)

    def _build_command(
        self, input_path: str, output_dir: str, pattern: str, duration: float
    ) -> list[str]:
        output_path = os.path.join(output_dir, pattern)
        segment_time = format_duration(duration)
        ffmpeg_exe = resolve_ffmpeg() or "ffmpeg"

        cmd = [ffmpeg_exe, "-y", "-i", input_path]

        map_option = self._get_map_option()
        for map_value in map_option["maps"]:
            cmd.extend(["-map", str(map_value)])

        if self.codec_mode_var.get() == "copy":
            cmd.extend(["-c", "copy"])
        else:
            cmd.extend(["-c:v", self.video_codec_var.get().strip() or "libx264"])
            cmd.extend(["-c:a", self.audio_codec_var.get().strip() or "aac"])

        cmd.extend(["-segment_time", segment_time, "-f", "segment"])

        if self.reset_timestamps_var.get():
            cmd.extend(["-reset_timestamps", "1"])
        if self.segment_at_clock_var.get():
            cmd.extend(["-segment_atclocktime", "1"])

        extra = self.extra_args_var.get().strip()
        if extra:
            cmd.extend(extra.split())

        cmd.append(output_path)
        return cmd

    def _resolve_merge_output_path(self) -> str | None:
        folder = self.merge_folder_var.get().strip()
        output_value = self.merge_output_var.get().strip()
        if not output_value:
            return None
        if os.path.isabs(output_value):
            return output_value
        if folder:
            return os.path.join(folder, output_value)
        return os.path.abspath(output_value)

    def _create_concat_list(self, files: list[str]) -> str:
        handle, list_path = tempfile.mkstemp(suffix=".txt", prefix="ffmpeg_concat_")
        os.close(handle)
        with open(list_path, "w", encoding="utf-8", newline="\n") as list_file:
            for file_path in files:
                list_file.write(f"file '{escape_concat_path(file_path)}'\n")
        return list_path

    def _validate_merge(self) -> tuple[list[str], str] | None:
        if not resolve_ffmpeg():
            messagebox.showerror(
                "FFmpeg Tidak Ditemukan",
                "FFmpeg belum terpasang.\n\n"
                "Klik 'Unduh & Pasang FFmpeg Otomatis' terlebih dahulu.",
            )
            return None

        if not self._scan_merge_files():
            return None

        if len(self._merge_files) < 2:
            messagebox.showerror("Error", "Minimal diperlukan 2 file segmen untuk digabung.")
            return None

        output_path = self._resolve_merge_output_path()
        if not output_path:
            messagebox.showerror("Error", "Tentukan nama file output gabungan.")
            return None

        output_dir = os.path.dirname(output_path)
        if output_dir and not os.path.isdir(output_dir):
            messagebox.showerror("Error", f"Folder output tidak ditemukan:\n{output_dir}")
            return None

        segment_paths = {os.path.normcase(os.path.abspath(path)) for path in self._merge_files}
        if os.path.normcase(os.path.abspath(output_path)) in segment_paths:
            messagebox.showerror(
                "Error",
                "File output tidak boleh sama dengan salah satu file segmen.",
            )
            return None

        return self._build_merge_command(self._merge_files, output_path), output_path

    def _build_merge_command(self, files: list[str], output_path: str) -> list[str]:
        ffmpeg_exe = resolve_ffmpeg() or "ffmpeg"
        list_path = self._create_concat_list(files)
        self._concat_list_path = list_path

        cmd = [ffmpeg_exe, "-y", "-f", "concat", "-safe", "0", "-i", list_path]

        if self.merge_codec_mode_var.get() == "copy":
            cmd.extend(["-c", "copy"])
        else:
            cmd.extend(["-c:v", self.merge_video_codec_var.get().strip() or "libx264"])
            cmd.extend(["-c:a", self.merge_audio_codec_var.get().strip() or "aac"])

        cmd.append(output_path)
        return cmd

    def _cleanup_concat_list(self) -> None:
        list_path = getattr(self, "_concat_list_path", "")
        if list_path and os.path.isfile(list_path):
            os.remove(list_path)
        self._concat_list_path = ""

    def _set_running(self, running: bool) -> None:
        self.start_btn.configure(state=tk.DISABLED if running else tk.NORMAL)
        self.merge_btn.configure(state=tk.DISABLED if running else tk.NORMAL)
        self.stop_btn.configure(state=tk.NORMAL if running else tk.DISABLED)
        self.status_var.set("Memproses..." if running else "Siap")

    def _start_split(self) -> None:
        self._operation_mode = "split"
        cmd = self._validate()
        if not cmd:
            return

        self._log("\n" + "=" * 60 + "\n")
        self._log("Perintah: " + " ".join(f'"{c}"' if " " in c else c for c in cmd) + "\n")
        self._log("=" * 60 + "\n")
        self._set_running(True)

        thread = threading.Thread(target=self._run_ffmpeg, args=(cmd,), daemon=True)
        thread.start()

    def _start_merge(self) -> None:
        self._operation_mode = "merge"
        validated = self._validate_merge()
        if not validated:
            return

        cmd, output_path = validated
        self._log("\n" + "=" * 60 + "\n")
        self._log(f"Menggabungkan {len(self._merge_files)} file segmen...\n")
        self._log(f"Output: {output_path}\n")
        self._log("Perintah: " + " ".join(f'"{c}"' if " " in c else c for c in cmd) + "\n")
        self._log("=" * 60 + "\n")
        self._set_running(True)

        thread = threading.Thread(target=self._run_ffmpeg, args=(cmd,), daemon=True)
        thread.start()

    def _run_ffmpeg(self, cmd: list[str]) -> None:
        try:
            self.process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
            )

            assert self.process.stdout is not None
            for line in self.process.stdout:
                self.root.after(0, self._log, line)

            return_code = self.process.wait()
            self.root.after(0, self._on_finished, return_code)
        except FileNotFoundError:
            self.root.after(0, self._on_error, "FFmpeg tidak ditemukan.")
        except Exception as exc:
            self.root.after(0, self._on_error, str(exc))
        finally:
            self.process = None
            if self._operation_mode == "merge":
                self.root.after(0, self._cleanup_concat_list)

    def _on_finished(self, return_code: int) -> None:
        self._set_running(False)
        if return_code == 0:
            if self._operation_mode == "merge":
                self._log("\nSelesai! Video berhasil digabung.\n")
                messagebox.showinfo("Berhasil", "Proses gabung video selesai.")
            else:
                self._log("\nSelesai! Video berhasil dipotong.\n")
                messagebox.showinfo("Berhasil", "Proses potong video selesai.")
        elif return_code == -15 or return_code == 1:
            self._log(f"\nProses dihentikan (kode: {return_code}).\n")
        else:
            self._log(f"\nGagal dengan kode keluar: {return_code}\n")
            messagebox.showerror("Gagal", f"FFmpeg gagal (kode: {return_code}).\nCek log untuk detail.")

    def _on_error(self, message: str) -> None:
        self._set_running(False)
        self._cleanup_concat_list()
        self._log(f"\nError: {message}\n")
        messagebox.showerror("Error", message)

    def _stop(self) -> None:
        if self.process and self.process.poll() is None:
            self.process.terminate()
            self._log("\nMembatalkan proses...\n")
            self._cleanup_concat_list()


def main() -> None:
    root = tk.Tk()
    style = ttk.Style()
    if "vista" in style.theme_names():
        style.theme_use("vista")
    VideoSplitterApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
