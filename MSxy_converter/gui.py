# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Mark Stuen
"""
gui.py
------
Tkinter GUI for the MSxy Converter.

Launch with:
    MSxy-converter --gui
    python MSxy_converter.py --gui

Layout
------
  Top bar   : config file path, Load / Save buttons
  Notebook  : Configuration tab | Live Status tab
  Bottom bar: Run / Stop button, status LED, log area
"""

import os
import sys
import json
import time
import threading
import traceback
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, scrolledtext

try:
    import sacn
    _SACN_OK = True
except ImportError:
    _SACN_OK = False


# ---------------------------------------------------------------------------
# Chip display colours (matched by substring of chip name, lower-case)
# ---------------------------------------------------------------------------

_CHIP_COLOURS = {
    "deep red":   "#CC0000",
    "red":        "#FF2200",
    "amber":      "#FF8800",
    "lime":       "#AAEE00",
    "green":      "#00CC00",
    "cyan":       "#00BBCC",
    "blue":       "#0044FF",
    "indigo":     "#5500CC",
    "violet":     "#7700BB",
    "uv":         "#6600AA",
    "warm white": "#FFD070",
    "cool white": "#DDEEFF",
    "white":      "#FFFFFF",
}

_DEFAULT_CHIP_COLOUR = "#888888"


def _chip_colour(name: str) -> str:
    n = name.lower()
    for key, col in _CHIP_COLOURS.items():
        if key in n:
            return col
    return _DEFAULT_CHIP_COLOUR


# ---------------------------------------------------------------------------
# Config defaults
# ---------------------------------------------------------------------------

_CONFIG_DEFAULTS = {
    "input_universe":    1,
    "output_universe":   2,
    "num_fixtures":      1,
    "input_start_addr":  1,
    "output_start_addr": 1,
    "bind_ip":           "0.0.0.0",
    "send_ip":           "0.0.0.0",
    "output_ip":         "",
    "has_intensity_ch":  False,
    "output_bit_depth":  8,
    "control_channels":  [],
    "xy_scale":          1.0,
    "output_gamma":      1.0,
    "leds":              [],
}


# ---------------------------------------------------------------------------
# Main GUI class
# ---------------------------------------------------------------------------

class ConverterGUI:
    def __init__(self, root: tk.Tk, initial_config: str = "led_config.json"):
        self.root       = root
        self.root.title("MSxy Converter")
        self.root.resizable(True, True)
        self.root.minsize(700, 600)

        self._cfg_path  = tk.StringVar(value=os.path.abspath(initial_config))
        self._cfg       = dict(_CONFIG_DEFAULTS)

        # Runtime state
        self._converter  = None
        self._stop_event = None
        self._sender     = None
        self._running    = False

        # Per-chip bar widgets  {fixture_idx: [(label, canvas, rect), ...]}
        self._chip_bars  = {}
        self._fix_labels = {}   # fixture_idx -> tk.Label (x/y/CCT line)

        # Per-fixture trim slider variables  {fi: {"gm": DoubleVar, "level": DoubleVar}}
        self._trim_vars  = {}

        self._build_ui()
        self._try_load_config(initial_config, silent=True)
        self._schedule_update()

    # -----------------------------------------------------------------------
    # UI construction
    # -----------------------------------------------------------------------

    def _build_ui(self):
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(1, weight=1)
        self.root.rowconfigure(2, weight=0)

        # Top bar — config file
        self._build_top_bar()

        # Notebook
        nb = ttk.Notebook(self.root)
        nb.grid(row=1, column=0, sticky="nsew", padx=6, pady=4)
        self._nb = nb

        self._tab_config = ttk.Frame(nb)
        self._tab_status = ttk.Frame(nb)
        nb.add(self._tab_config, text="  Configuration  ")
        nb.add(self._tab_status, text="  Live Status  ")

        self._build_config_tab()
        self._build_status_tab()

        # Bottom bar — run/stop + log
        self._build_bottom_bar()

    def _build_top_bar(self):
        bar = ttk.Frame(self.root)
        bar.grid(row=0, column=0, sticky="ew", padx=6, pady=(6, 0))
        bar.columnconfigure(1, weight=1)

        ttk.Label(bar, text="Config file:").grid(row=0, column=0, padx=(0, 4))
        ttk.Entry(bar, textvariable=self._cfg_path, width=50).grid(
            row=0, column=1, sticky="ew", padx=2)
        ttk.Button(bar, text="Browse…",
                   command=self._browse_config).grid(row=0, column=2, padx=2)
        ttk.Button(bar, text="Load",
                   command=self._load_config_clicked).grid(row=0, column=3, padx=2)
        ttk.Button(bar, text="Save",
                   command=self._save_config_clicked).grid(row=0, column=4, padx=2)

    def _build_config_tab(self):
        tab = self._tab_config
        tab.columnconfigure(0, weight=1)
        tab.columnconfigure(1, weight=1)

        canvas  = tk.Canvas(tab, borderwidth=0, highlightthickness=0)
        scrollbar = ttk.Scrollbar(tab, orient="vertical", command=canvas.yview)
        self._cfg_frame = ttk.Frame(canvas)

        self._cfg_frame.bind("<Configure>",
            lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=self._cfg_frame, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)

        canvas.grid(row=0, column=0, sticky="nsew")
        scrollbar.grid(row=0, column=1, sticky="ns")
        tab.rowconfigure(0, weight=1)

        f = self._cfg_frame
        f.columnconfigure(1, weight=1)
        f.columnconfigure(3, weight=1)

        def section(label, row):
            ttk.Separator(f, orient="horizontal").grid(
                row=row, column=0, columnspan=4, sticky="ew", pady=(10, 2))
            ttk.Label(f, text=label, font=("", 9, "bold")).grid(
                row=row+1, column=0, columnspan=4, sticky="w", padx=6)
            return row + 2

        def field(label, var, row, col=0, width=14):
            ttk.Label(f, text=label).grid(
                row=row, column=col, sticky="e", padx=(12, 4), pady=2)
            ttk.Entry(f, textvariable=var, width=width).grid(
                row=row, column=col+1, sticky="ew", padx=(0, 12), pady=2)

        r = 0

        # --- Network ---
        r = section("Network", r)
        self._v_bind_ip   = tk.StringVar()
        self._v_send_ip   = tk.StringVar()
        self._v_output_ip = tk.StringVar()
        field("Bind IP (RX):",  self._v_bind_ip,   r, 0)
        field("Bind IP (TX):",  self._v_send_ip,   r, 2)
        r += 1
        field("Output IP:", self._v_output_ip, r, 0, 20)
        ttk.Label(f, text="(blank = multicast)", foreground="gray").grid(
            row=r, column=2, columnspan=2, sticky="w")
        r += 1

        # --- Universes ---
        r = section("Universes & Addressing", r)
        self._v_in_uni    = tk.StringVar()
        self._v_out_uni   = tk.StringVar()
        self._v_in_addr   = tk.StringVar()
        self._v_out_addr  = tk.StringVar()
        self._v_num_fix   = tk.StringVar()
        field("Input Universe:",     self._v_in_uni,   r, 0)
        field("Output Universe:",    self._v_out_uni,  r, 2)
        r += 1
        field("Input Start Addr:",   self._v_in_addr,  r, 0)
        field("Output Start Addr:",  self._v_out_addr, r, 2)
        r += 1
        field("Number of Fixtures:", self._v_num_fix,  r, 0)
        r += 1

        # --- Fixture ---
        r = section("Fixture", r)
        self._v_has_int  = tk.BooleanVar()
        self._v_bit      = tk.StringVar(value="8")
        self._v_gamma    = tk.StringVar()
        self._v_xyscale  = tk.StringVar()
        self._v_ctrl_chs = tk.StringVar()

        ttk.Checkbutton(f, text="Has intensity channel",
                        variable=self._v_has_int).grid(
            row=r, column=0, columnspan=2, sticky="w", padx=12, pady=2)
        r += 1

        ttk.Label(f, text="Output bit depth:").grid(
            row=r, column=0, sticky="e", padx=(12, 4))
        bd_frame = ttk.Frame(f)
        bd_frame.grid(row=r, column=1, sticky="w")
        ttk.Radiobutton(bd_frame, text="8-bit",  variable=self._v_bit,
                        value="8").pack(side="left", padx=4)
        ttk.Radiobutton(bd_frame, text="16-bit", variable=self._v_bit,
                        value="16").pack(side="left", padx=4)
        r += 1

        field("XY Scale:",         self._v_xyscale,  r, 0)
        field("Output Gamma:",     self._v_gamma,    r, 2)
        r += 1
        ttk.Label(f, text="Control Channels:").grid(
            row=r, column=0, sticky="e", padx=(12, 4), pady=2)
        ttk.Entry(f, textvariable=self._v_ctrl_chs, width=30).grid(
            row=r, column=1, columnspan=3, sticky="ew", padx=(0, 12), pady=2)
        ttk.Label(f, text="(comma-separated, held at 0)",
                  foreground="gray").grid(row=r+1, column=1, columnspan=3,
                                         sticky="w", padx=(0, 12))
        r += 2

        # --- Fixture Preset ---
        r = section("Fixture Preset", r)

        from .presets import PRESETS
        preset_labels = ["(no preset — use current LED config)"] + \
                        [p["label"] for p in PRESETS.values()
                         if p["label"] != "Custom  (define each chip manually)"] + \
                        ["Custom  (define each chip manually)"]
        self._preset_map = {p["label"]: p for p in PRESETS.values()}

        self._v_preset = tk.StringVar(value=preset_labels[0])
        ttk.Label(f, text="Load preset:").grid(
            row=r, column=0, sticky="e", padx=(12, 4), pady=2)
        preset_cb = ttk.Combobox(f, textvariable=self._v_preset,
                                 values=preset_labels, state="readonly",
                                 width=42)
        preset_cb.grid(row=r, column=1, columnspan=3, sticky="ew",
                       padx=(0, 12), pady=2)
        preset_cb.bind("<<ComboboxSelected>>", self._on_preset_selected)
        r += 1
        ttk.Label(f, text="Selecting a preset fills in all fields below and the LED table.",
                  foreground="gray").grid(
            row=r, column=1, columnspan=3, sticky="w", padx=(0, 12))
        r += 1

        # --- LED Chips ---
        r = section("LED Chips", r)
        ttk.Label(f, text="Emitter chromaticity (loaded from preset or config file):",
                  foreground="gray").grid(
            row=r, column=0, columnspan=4, sticky="w", padx=12)
        r += 1

        tree_frame = ttk.Frame(f)
        tree_frame.grid(row=r, column=0, columnspan=4, sticky="ew",
                        padx=12, pady=4)
        self._led_tree = ttk.Treeview(
            tree_frame,
            columns=("x", "y", "flux"),
            show="headings tree",
            height=8,
        )
        self._led_tree.heading("#0",   text="Chip")
        self._led_tree.heading("x",    text="CIE x")
        self._led_tree.heading("y",    text="CIE y")
        self._led_tree.heading("flux", text="Flux")
        self._led_tree.column("#0",    width=120)
        self._led_tree.column("x",     width=70, anchor="center")
        self._led_tree.column("y",     width=70, anchor="center")
        self._led_tree.column("flux",  width=80, anchor="center")
        self._led_tree.pack(side="left", fill="both", expand=True)
        ttk.Scrollbar(tree_frame, orient="vertical",
                      command=self._led_tree.yview).pack(side="right", fill="y")

        r += 1
        ttk.Button(f, text="Open config in system editor",
                   command=self._open_in_editor).grid(
            row=r, column=0, columnspan=2, sticky="w", padx=12, pady=6)
        ttk.Label(f, text="Edit LED chips, flux, and xy values in the JSON",
                  foreground="gray").grid(
            row=r, column=2, columnspan=2, sticky="w")
        r += 1

    def _on_preset_selected(self, event=None):
        """Apply a built-in preset to all fixture fields and the LED table."""
        label = self._v_preset.get()
        preset = self._preset_map.get(label)
        if not preset:
            return
        # Update fixture settings
        self._v_has_int.set(bool(preset.get("has_intensity_ch", False)))
        self._v_bit.set(str(preset.get("output_bit_depth", 8)))
        self._v_ctrl_chs.set(", ".join(preset.get("control_channels", [])))
        # Update LED table and cfg
        self._cfg["has_intensity_ch"]  = preset.get("has_intensity_ch", False)
        self._cfg["output_bit_depth"]  = preset.get("output_bit_depth", 8)
        self._cfg["control_channels"]  = preset.get("control_channels", [])
        self._cfg["leds"]              = list(preset.get("leds", []))
        # Refresh LED tree
        for row in self._led_tree.get_children():
            self._led_tree.delete(row)
        for led in self._cfg["leds"]:
            self._led_tree.insert("", "end",
                text=led.get("name", "?"),
                values=(
                    f"{led.get('x', 0):.4f}",
                    f"{led.get('y', 0):.4f}",
                    f"{led.get('flux', 0):.1f}",
                ))
        self._log_msg(f"Preset loaded: {label}")

    def _build_status_tab(self):
        tab = self._tab_status
        tab.columnconfigure(0, weight=1)
        tab.rowconfigure(1, weight=1)

        # Header
        hdr = ttk.Frame(tab)
        hdr.grid(row=0, column=0, sticky="ew", padx=8, pady=4)
        self._status_dot = tk.Label(hdr, text="●", font=("", 14),
                                    foreground="#888888")
        self._status_dot.pack(side="left")
        self._status_lbl = ttk.Label(hdr, text="Stopped",
                                     font=("", 10, "bold"))
        self._status_lbl.pack(side="left", padx=6)
        self._rate_lbl = ttk.Label(hdr, text="", foreground="gray")
        self._rate_lbl.pack(side="right", padx=12)
        self._pkt_lbl  = ttk.Label(hdr, text="", foreground="gray")
        self._pkt_lbl.pack(side="right", padx=8)

        # Scrollable fixture area
        outer = ttk.Frame(tab)
        outer.grid(row=1, column=0, sticky="nsew", padx=4, pady=4)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(0, weight=1)

        canvas = tk.Canvas(outer, borderwidth=0, highlightthickness=0)
        sb     = ttk.Scrollbar(outer, orient="vertical", command=canvas.yview)
        self._fix_frame = ttk.Frame(canvas)
        self._fix_frame.bind("<Configure>",
            lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=self._fix_frame, anchor="nw")
        canvas.configure(yscrollcommand=sb.set)
        canvas.grid(row=0, column=0, sticky="nsew")
        sb.grid(row=0, column=1, sticky="ns")
        self._status_canvas = canvas

    def _build_bottom_bar(self):
        bar = ttk.Frame(self.root)
        bar.grid(row=2, column=0, sticky="ew", padx=6, pady=(2, 4))
        bar.columnconfigure(2, weight=1)

        self._run_btn  = ttk.Button(bar, text="▶  Run",
                                    command=self._start, width=10)
        self._stop_btn = ttk.Button(bar, text="■  Stop",
                                    command=self._stop,  width=10,
                                    state="disabled")
        self._run_btn.grid(row=0, column=0, padx=(0, 4))
        self._stop_btn.grid(row=0, column=1, padx=4)

        # Log
        log_frame = ttk.LabelFrame(bar, text="Log")
        log_frame.grid(row=0, column=2, sticky="ew", padx=(8, 0))
        log_frame.columnconfigure(0, weight=1)
        self._log = scrolledtext.ScrolledText(
            log_frame, height=4, wrap="word",
            font=("Courier", 8), state="disabled")
        self._log.grid(row=0, column=0, sticky="ew")

    # -----------------------------------------------------------------------
    # Config load / save
    # -----------------------------------------------------------------------

    def _browse_config(self):
        path = filedialog.askopenfilename(
            title="Open config file",
            filetypes=[("JSON", "*.json"), ("All files", "*.*")],
            initialfile=self._cfg_path.get(),
        )
        if path:
            self._cfg_path.set(os.path.abspath(path))
            self._try_load_config(path)

    def _load_config_clicked(self):
        self._try_load_config(self._cfg_path.get())

    def _save_config_clicked(self):
        self._fields_to_cfg()
        path = self._cfg_path.get()
        try:
            with open(path, "w") as f:
                json.dump(self._cfg, f, indent=2)
            self._log_msg(f"Saved config → {path}")
        except Exception as e:
            messagebox.showerror("Save failed", str(e))

    def _try_load_config(self, path: str, silent: bool = False):
        try:
            with open(path) as f:
                raw = json.load(f)
            self._cfg = {**_CONFIG_DEFAULTS, **raw}
            self._cfg_path.set(os.path.abspath(path))
            self._cfg_to_fields()
            self._rebuild_status_fixtures()
            if not silent:
                self._log_msg(f"Loaded config ← {path}")
        except FileNotFoundError:
            if not silent:
                messagebox.showerror("Not found", f"File not found:\n{path}")
        except Exception as e:
            if not silent:
                messagebox.showerror("Load failed", str(e))
            else:
                self._log_msg(f"No config at {path} — fill in fields and save.")

    def _cfg_to_fields(self):
        """Populate UI widgets from self._cfg."""
        c = self._cfg
        self._v_bind_ip.set(c.get("bind_ip",   "0.0.0.0"))
        self._v_send_ip.set(c.get("send_ip",   "0.0.0.0"))
        self._v_output_ip.set(c.get("output_ip") or "")
        self._v_in_uni.set(str(c.get("input_universe",    1)))
        self._v_out_uni.set(str(c.get("output_universe",  2)))
        self._v_in_addr.set(str(c.get("input_start_addr", 1)))
        self._v_out_addr.set(str(c.get("output_start_addr",1)))
        self._v_num_fix.set(str(c.get("num_fixtures", 1)))
        self._v_has_int.set(bool(c.get("has_intensity_ch", False)))
        self._v_bit.set(str(c.get("output_bit_depth", 8)))
        self._v_gamma.set(str(c.get("output_gamma", 1.0)))
        self._v_xyscale.set(str(c.get("xy_scale", 1.0)))
        self._v_ctrl_chs.set(", ".join(c.get("control_channels", [])))

        # LED tree
        for row in self._led_tree.get_children():
            self._led_tree.delete(row)
        for led in c.get("leds", []):
            self._led_tree.insert("", "end",
                text=led.get("name", "?"),
                values=(
                    f"{led.get('x', 0):.4f}",
                    f"{led.get('y', 0):.4f}",
                    f"{led.get('flux', 0):.1f}",
                ))

    def _fields_to_cfg(self):
        """Read UI widgets back into self._cfg."""
        def _int(v, default):
            try: return int(v.get())
            except: return default
        def _float(v, default):
            try: return float(v.get())
            except: return default

        self._cfg["bind_ip"]           = self._v_bind_ip.get().strip()
        self._cfg["send_ip"]           = self._v_send_ip.get().strip()
        out_ip                         = self._v_output_ip.get().strip()
        self._cfg["output_ip"]         = out_ip if out_ip else None
        self._cfg["input_universe"]    = _int(self._v_in_uni,   1)
        self._cfg["output_universe"]   = _int(self._v_out_uni,  2)
        self._cfg["input_start_addr"]  = _int(self._v_in_addr,  1)
        self._cfg["output_start_addr"] = _int(self._v_out_addr, 1)
        self._cfg["num_fixtures"]      = _int(self._v_num_fix,  1)
        self._cfg["has_intensity_ch"]  = bool(self._v_has_int.get())
        self._cfg["output_bit_depth"]  = _int(self._v_bit,      8)
        self._cfg["output_gamma"]      = _float(self._v_gamma,  1.0)
        self._cfg["xy_scale"]          = _float(self._v_xyscale,1.0)
        raw_ctrl = self._v_ctrl_chs.get().strip()
        self._cfg["control_channels"]  = (
            [s.strip() for s in raw_ctrl.split(",") if s.strip()]
            if raw_ctrl else []
        )

    def _open_in_editor(self):
        path = self._cfg_path.get()
        if not os.path.exists(path):
            self._save_config_clicked()
        try:
            if sys.platform == "win32":
                os.startfile(path)
            elif sys.platform == "darwin":
                import subprocess; subprocess.Popen(["open", path])
            else:
                import subprocess; subprocess.Popen(["xdg-open", path])
        except Exception as e:
            messagebox.showerror("Cannot open editor", str(e))

    # -----------------------------------------------------------------------
    # Status fixture widgets
    # -----------------------------------------------------------------------

    def _rebuild_status_fixtures(self):
        """Recreate the per-fixture chip-bar and trim-slider widgets."""
        for w in self._fix_frame.winfo_children():
            w.destroy()
        self._chip_bars  = {}
        self._fix_labels = {}
        self._trim_vars  = {}

        leds    = self._cfg.get("leds", [])
        n_fix   = self._cfg.get("num_fixtures", 1)
        has_int = self._cfg.get("has_intensity_ch", False)

        for fi in range(n_fix):
            frame = ttk.LabelFrame(self._fix_frame,
                                   text=f"  Fixture {fi + 1}  ")
            frame.pack(fill="x", padx=8, pady=4)
            frame.columnconfigure(0, weight=1)

            # ---- Row 0: xy / CCT / Duv / active trim readout ----
            lbl = ttk.Label(frame, text="—", font=("Courier", 9))
            lbl.grid(row=0, column=0, sticky="w", padx=8, pady=(4, 0))
            self._fix_labels[fi] = lbl

            # ---- Row 1: chip bars ----
            bar_row = ttk.Frame(frame)
            bar_row.grid(row=1, column=0, sticky="ew", padx=8, pady=(2, 4))
            bars = []

            if has_int:
                colour    = "#CCCCCC"
                col_frame = ttk.Frame(bar_row)
                col_frame.pack(side="left", padx=4)
                ttk.Label(col_frame, text="Int", font=("", 7)).pack()
                bc = tk.Canvas(col_frame, width=28, height=60,
                               bg="#222222", highlightthickness=1,
                               highlightbackground="#555555")
                bc.pack()
                rect    = bc.create_rectangle(2, 60, 26, 60,
                                              fill=colour, outline="")
                val_lbl = ttk.Label(col_frame, text="0", font=("", 7))
                val_lbl.pack()
                bars.append(("__int__", bc, rect, val_lbl, colour))

            for led in leds:
                name      = led.get("name", "?")
                colour    = _chip_colour(name)
                col_frame = ttk.Frame(bar_row)
                col_frame.pack(side="left", padx=3)
                short = (name.replace("Deep ", "D.").replace("Warm ", "W.")
                             .replace("Cool ", "C.")[:6])
                ttk.Label(col_frame, text=short, font=("", 7)).pack()
                bc = tk.Canvas(col_frame, width=28, height=60,
                               bg="#222222", highlightthickness=1,
                               highlightbackground="#555555")
                bc.pack()
                rect    = bc.create_rectangle(2, 60, 26, 60,
                                              fill=colour, outline="")
                val_lbl = ttk.Label(col_frame, text="0", font=("", 7))
                val_lbl.pack()
                bars.append((name, bc, rect, val_lbl, colour))

            self._chip_bars[fi] = bars

            # ---- Row 2: trim sliders ----
            # Use a fixed-height frame with propagate=False so slider
            # value label text changes never cause the fixture box to resize.
            trim_frame = tk.Frame(frame, height=42)
            trim_frame.grid(row=2, column=0, sticky="ew", padx=8, pady=(0, 6))
            trim_frame.pack_propagate(False)
            trim_frame.grid_propagate(False)
            # Fixed pixel column widths — sliders expand, labels stay put
            trim_frame.columnconfigure(0, minsize=14)   # M label
            trim_frame.columnconfigure(1, weight=1)     # G/M slider
            trim_frame.columnconfigure(2, minsize=14)   # G label
            trim_frame.columnconfigure(3, minsize=56)   # G/M value (fixed)
            trim_frame.columnconfigure(4, minsize=44)   # Level: label
            trim_frame.columnconfigure(5, weight=1)     # Level slider
            trim_frame.columnconfigure(6, minsize=44)   # Level value (fixed)

            gm_var    = tk.DoubleVar(value=0.0)
            level_var = tk.DoubleVar(value=1.0)
            self._trim_vars[fi] = {"gm": gm_var, "level": level_var}

            # Green/Magenta trim
            tk.Label(trim_frame, text="M", fg="#CC44CC",
                     font=("", 8, "bold"), bg=trim_frame.cget("bg")
                     ).grid(row=0, column=0, sticky="e")
            gm_slider = ttk.Scale(
                trim_frame, from_=-0.020, to=0.020,
                orient="horizontal", variable=gm_var,
                command=lambda v, f=fi: self._on_gm_trim(f, v))
            gm_slider.grid(row=0, column=1, sticky="ew", padx=2)
            tk.Label(trim_frame, text="G", fg="#44CC44",
                     font=("", 8, "bold"), bg=trim_frame.cget("bg")
                     ).grid(row=0, column=2, sticky="w")
            # Fixed-width label — text stays same width, no reflow
            gm_val_lbl = tk.Label(trim_frame, text=" +0.000",
                                  font=("Courier", 8), width=7, anchor="w",
                                  bg=trim_frame.cget("bg"))
            gm_val_lbl.grid(row=0, column=3, sticky="w", padx=(2, 8))
            self._trim_vars[fi]["gm_lbl"] = gm_val_lbl

            gm_slider.bind("<Double-Button-1>",
                           lambda e, f=fi: self._reset_gm(f))

            # Level trim
            tk.Label(trim_frame, text="Level:", font=("", 8),
                     bg=trim_frame.cget("bg")
                     ).grid(row=0, column=4, sticky="e", padx=(0, 2))
            lvl_slider = ttk.Scale(
                trim_frame, from_=0.50, to=1.50,
                orient="horizontal", variable=level_var,
                command=lambda v, f=fi: self._on_level_trim(f, v))
            lvl_slider.grid(row=0, column=5, sticky="ew", padx=2)
            lvl_val_lbl = tk.Label(trim_frame, text=" 100%",
                                   font=("Courier", 8), width=5, anchor="w",
                                   bg=trim_frame.cget("bg"))
            lvl_val_lbl.grid(row=0, column=6, sticky="w", padx=(2, 0))
            self._trim_vars[fi]["lvl_lbl"] = lvl_val_lbl

            lvl_slider.bind("<Double-Button-1>",
                            lambda e, f=fi: self._reset_level(f))

            tk.Label(trim_frame,
                     text="double-click to reset",
                     fg="gray", font=("", 7),
                     bg=trim_frame.cget("bg")
                     ).grid(row=0, column=7, sticky="w", padx=(6, 0))

    # -----------------------------------------------------------------------
    # Trim slider callbacks
    # -----------------------------------------------------------------------

    def _on_gm_trim(self, fi: int, value):
        v   = float(value)
        lbl = self._trim_vars[fi]["gm_lbl"]
        sign = "+" if v >= 0 else ""
        lbl.config(text=f"{sign}{v:.3f}")
        if self._converter:
            self._converter.set_trim(fi, gm=v)

    def _on_level_trim(self, fi: int, value):
        v   = float(value)
        lbl = self._trim_vars[fi]["lvl_lbl"]
        lbl.config(text=f"{int(round(v * 100))}%")
        if self._converter:
            self._converter.set_trim(fi, level=v)

    def _reset_gm(self, fi: int):
        tv = self._trim_vars[fi]
        tv["gm"].set(0.0)
        tv["gm_lbl"].config(text=" +0.000")
        if self._converter:
            self._converter.set_trim(fi, gm=0.0)

    def _reset_level(self, fi: int):
        tv = self._trim_vars[fi]
        tv["level"].set(1.0)
        tv["lvl_lbl"].config(text=" 100%")
        if self._converter:
            self._converter.set_trim(fi, level=1.0)

    def _update_bar(self, bar_tuple, value_0_255: int, bit_depth: int):
        """Redraw one chip bar to reflect the current DMX value."""
        _, canvas, rect, val_lbl, colour = bar_tuple
        scale = 65535 if bit_depth == 16 else 255
        frac  = max(0.0, min(1.0, value_0_255 / scale))
        h     = 60
        top   = h - int(frac * (h - 2)) - 1
        canvas.coords(rect, 2, top, 26, h - 1)
        # Dim the colour when low
        if frac < 0.01:
            canvas.itemconfig(rect, fill="#333333")
        else:
            canvas.itemconfig(rect, fill=colour)
        val_lbl.config(text=str(value_0_255))

    # -----------------------------------------------------------------------
    # Run / Stop
    # -----------------------------------------------------------------------

    def _start(self):
        if self._running:
            return
        if not _SACN_OK:
            messagebox.showerror("Missing dependency",
                                 "The 'sacn' package is not installed.\n"
                                 "Run: pip install sacn")
            return

        self._fields_to_cfg()

        # Validate minimum required fields
        if not self._cfg.get("leds"):
            messagebox.showerror(
                "No LEDs configured",
                "The config has no LED chips defined.\n"
                "Load a config file or edit led_config.json first.")
            return

        try:
            from .converter import ColorConverter, fixture_footprint
            from .network   import receiver_loop, sender_loop
        except ImportError:
            try:
                # Standalone mode — they're in the same module namespace
                from __main__ import ColorConverter, fixture_footprint
                from __main__ import receiver_loop, sender_loop
            except ImportError:
                messagebox.showerror("Import error",
                                     "Could not import converter modules.")
                return

        try:
            self._converter  = ColorConverter(self._cfg)
            self._stop_event = threading.Event()

            bind_ip  = self._cfg.get("bind_ip",  "0.0.0.0")
            send_ip  = self._cfg.get("send_ip",  bind_ip)
            out_uni  = self._cfg["output_universe"]
            in_uni   = self._cfg["input_universe"]

            self._sender = sacn.sACNsender(bind_address=send_ip, fps=44)
            self._sender.start()
            self._sender.activate_output(out_uni)
            if self._cfg.get("output_ip"):
                self._sender[out_uni].destination = self._cfg["output_ip"]
                self._sender[out_uni].multicast   = False
            else:
                self._sender[out_uni].multicast = True

            threading.Thread(
                target=receiver_loop,
                args=(bind_ip, in_uni, self._converter, self._stop_event),
                daemon=True,
            ).start()

            threading.Thread(
                target=sender_loop,
                args=(self._sender, out_uni, self._converter, 44.0),
                daemon=True,
            ).start()

            self._running = True
            self._run_btn.config(state="disabled")
            self._stop_btn.config(state="normal")
            self._rebuild_status_fixtures()
            self._nb.select(1)   # switch to Status tab
            self._log_msg(f"Started — RX uni {in_uni}, TX uni {out_uni}")

        except Exception as e:
            self._log_msg(f"ERROR starting: {e}")
            traceback.print_exc()
            messagebox.showerror("Start failed", str(e))
            self._cleanup()

    def _stop(self):
        self._cleanup()
        self._log_msg("Stopped.")

    def _cleanup(self):
        self._running = False
        if self._stop_event:
            self._stop_event.set()
        if self._sender:
            try:
                self._sender.stop()
            except Exception:
                pass
        self._sender     = None
        self._converter  = None
        self._stop_event = None
        self._run_btn.config(state="normal")
        self._stop_btn.config(state="disabled")
        self._status_dot.config(foreground="#888888")
        self._status_lbl.config(text="Stopped")
        self._rate_lbl.config(text="")
        self._pkt_lbl.config(text="")

    # -----------------------------------------------------------------------
    # Live update loop (called every 150 ms via root.after)
    # -----------------------------------------------------------------------

    def _schedule_update(self):
        self.root.after(150, self._update)

    def _update(self):
        try:
            if self._running and self._converter:
                self._update_status()
        except Exception:
            pass
        self._schedule_update()

    def _update_status(self):
        from .cct import xy_to_cct_duv
        conv      = self._converter
        bit_depth = self._cfg.get("output_bit_depth", 8)
        has_int   = self._cfg.get("has_intensity_ch", False)

        buf, snaps = conv.get_frame()

        # Header
        self._status_dot.config(foreground="#22CC22")
        self._status_lbl.config(text="Running")
        elapsed = time.monotonic() - (conv.last_rx_time or time.monotonic())
        fps_str = "44 fps" if elapsed < 1.0 else "no signal"
        self._pkt_lbl.config(text=f"Packets: {conv.rx_count:,}")
        self._rate_lbl.config(text=fps_str)

        for fi, snap in enumerate(snaps):
            if fi not in self._fix_labels:
                continue

            x, y = snap.cx, snap.cy
            cct_result = xy_to_cct_duv(x, y) if (x > 0 or y > 0) else None
            if cct_result:
                cct, duv = cct_result
                sign    = "+" if duv >= 0 else ""
                cct_str = f"  {cct}K  Duv={sign}{duv:.4f}"
            else:
                cct_str = ""

            # Show active trims inline when non-zero
            trim_parts = []
            if abs(snap.gm_trim) >= 0.0005:
                sign = "+" if snap.gm_trim >= 0 else ""
                trim_parts.append(f"G/M={sign}{snap.gm_trim:.3f}")
            if abs(snap.level_trim - 1.0) >= 0.005:
                trim_parts.append(f"Lvl={int(round(snap.level_trim*100))}%")
            trim_str = ("  [" + "  ".join(trim_parts) + "]"
                        if trim_parts else "")

            lbl_text = (f"x={x:.4f}  y={y:.4f}  "
                        f"dim={snap.dim:.3f}{cct_str}{trim_str}")
            self._fix_labels[fi].config(text=lbl_text)

            bars    = self._chip_bars.get(fi, [])
            bar_idx = 0

            if has_int and bars:
                self._update_bar(bars[0], snap.out_intensity, bit_depth)
                bar_idx = 1

            for ci, val in enumerate(snap.out_vals):
                if bar_idx + ci < len(bars):
                    self._update_bar(bars[bar_idx + ci], val, bit_depth)

    # -----------------------------------------------------------------------
    # Log
    # -----------------------------------------------------------------------

    def _log_msg(self, msg: str):
        ts = time.strftime("%H:%M:%S")
        self._log.config(state="normal")
        self._log.insert("end", f"[{ts}] {msg}\n")
        self._log.see("end")
        self._log.config(state="disabled")

    # -----------------------------------------------------------------------
    # Window close
    # -----------------------------------------------------------------------

    def on_close(self):
        self._cleanup()
        self.root.destroy()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def run_gui(config_file: str = "led_config.json"):
    """Launch the GUI. Blocks until the window is closed."""
    root = tk.Tk()
    app  = ConverterGUI(root, initial_config=config_file)
    root.protocol("WM_DELETE_WINDOW", app.on_close)

    # Centre the window
    root.update_idletasks()
    w, h = 760, 680
    sw   = root.winfo_screenwidth()
    sh   = root.winfo_screenheight()
    root.geometry(f"{w}x{h}+{(sw-w)//2}+{(sh-h)//2}")

    root.mainloop()
