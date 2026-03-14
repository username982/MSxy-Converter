# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Mark Stuen
"""
converter.py
------------
ColorConverter class — the core per-frame processing engine.

Reads a 512-byte sACN DMX frame, extracts per-fixture Dimmer/X/Y,
solves for LED chip weights, and writes the result into a 512-byte
output buffer ready to be sent as sACN.
"""

import threading
import traceback
import time
import numpy as np

from .colour import (
    build_led_matrix, build_gamut_xy, clamp_xy_to_gamut, apply_gm_trim,
    xy_intensity_to_XYZ, solve_led_weights,
    unpack16, write_channel,
)


def fixture_footprint(cfg: dict) -> int:
    """
    Total DMX channels consumed per fixture on the output universe.

      footprint = (intensity_ch ? bpc : 0)
                + num_led_chips * bpc
                + num_control_channels
    where bpc = 2 if 16-bit, else 1.
    """
    bpc  = 2 if cfg.get("output_bit_depth", 8) == 16 else 1
    int_ = bpc if cfg.get("has_intensity_ch", False) else 0
    led  = len(cfg["leds"]) * bpc
    ctrl = len(cfg.get("control_channels", []))
    return int_ + led + ctrl


class FixtureSnapshot:
    """Holds the most recently decoded input and computed output for one fixture."""

    def __init__(self, in_ch: int, out_ch: int, num_chips: int,
                 has_intensity_ch: bool, bit_depth: int):
        self.in_start_ch   = in_ch
        self.out_start_ch  = out_ch
        self.has_intensity = has_intensity_ch
        self.bit_depth     = bit_depth
        # Raw input bytes
        self.raw_dim_hi = self.raw_dim_lo = 0
        self.raw_x_hi   = self.raw_x_lo   = 0
        self.raw_y_hi   = self.raw_y_lo   = 0
        # Decoded input values
        self.dim = 0.0
        self.cx  = 0.0
        self.cy  = 0.0
        # Active trim values (written each frame for display)
        self.gm_trim    = 0.0   # green(+) / magenta(-) Duv offset
        self.level_trim = 1.0   # output level multiplier (0.5–1.5)
        # Output values (scaled to bit_depth max for display)
        self.out_intensity = 0
        self.out_vals      = [0] * num_chips


class ColorConverter:
    """
    Convert sACN Intensity/XY frames to multi-chip LED DMX output.

    Parameters
    ----------
    cfg : dict
        Loaded configuration (from led_config.json or equivalent).
        Expected keys: leds, num_fixtures, xy_scale, output_gamma,
        input_start_addr, output_start_addr, has_intensity_ch,
        output_bit_depth, control_channels.
    """

    def __init__(self, cfg: dict):
        self.leds         = cfg["leds"]
        self.num_chips    = len(self.leds)
        self.num_fixtures = cfg["num_fixtures"]
        self.xy_scale     = cfg.get("xy_scale",           1.0)
        self.gamma        = cfg.get("output_gamma",        1.0)
        self.in_start     = cfg.get("input_start_addr",    1)
        self.out_start    = cfg.get("output_start_addr",   1)
        self.has_int_ch   = cfg.get("has_intensity_ch",    False)
        self.bit_depth    = cfg.get("output_bit_depth",    8)
        self.ctrl_chs     = cfg.get("control_channels",    [])
        self.num_ctrl     = len(self.ctrl_chs)
        self.footprint    = fixture_footprint(cfg)
        self.M            = build_led_matrix(self.leds)
        self._gamut_xy    = build_gamut_xy(self.leds)   # for xy clamping
        self._out_buf     = [0] * 512
        self._snapshots   = [
            FixtureSnapshot(
                in_ch           = self.in_start  + i * 6,
                out_ch          = self.out_start + i * self.footprint,
                num_chips       = self.num_chips,
                has_intensity_ch= self.has_int_ch,
                bit_depth       = self.bit_depth,
            )
            for i in range(self.num_fixtures)
        ]
        self._lock        = threading.Lock()
        self.rx_count     = 0
        self.last_rx_time = None

        # Per-fixture runtime trims — adjusted live from the GUI.
        self._trims = [{"gm": 0.0, "level": 1.0}
                       for _ in range(self.num_fixtures)]

        # ---------------------------------------------------------------
        # Adaptive EMA output smoothing
        # ---------------------------------------------------------------
        # Each output frame is blended toward the newly solved target using
        # an exponential moving average.  The blend rate (alpha) adapts to
        # the magnitude of the change:
        #
        #   Large move  (delta > smooth_threshold) → smooth_fast alpha
        #     Reaches 99 % of target in ≈ 8 packets  (~180 ms at 44 Hz)
        #     Keeps console moves feeling immediate
        #
        #   Small move  (delta ≤ smooth_threshold) → smooth_slow alpha
        #     Reaches 99 % of target in ≈ 38 packets (~860 ms at 44 Hz)
        #     Suppresses jitter from trim sliders, CCT quantisation, and
        #     console noise without making intentional fades feel sluggish
        #
        # Both alphas and the threshold are user-configurable in the JSON.
        # Defaults are tuned for live theatrical use.
        self._s_fast  = float(cfg.get("smooth_fast",      0.55))
        self._s_slow  = float(cfg.get("smooth_slow",      0.12))
        self._s_thr   = float(cfg.get("smooth_threshold", 0.04))

        # Smoothed chip weights per fixture — initialised to zero
        self._sw = [np.zeros(self.num_chips)
                    for _ in range(self.num_fixtures)]
        # Smoothed intensity per fixture
        self._si = [0.0] * self.num_fixtures

        self._print_matrix()

    def set_trim(self, fixture_idx: int,
                 gm: float = None, level: float = None) -> None:
        """
        Update trim values for one fixture.  Thread-safe; called from GUI.

        Parameters
        ----------
        fixture_idx : 0-based fixture index
        gm          : green/magenta Duv offset (-0.020 … +0.020)
                      positive = shift toward green, negative = toward magenta
        level       : output level multiplier (0.50 … 1.50)
                      compensates for voltage drop or ribbon length differences
        """
        if not (0 <= fixture_idx < self.num_fixtures):
            return
        with self._lock:
            if gm    is not None:
                self._trims[fixture_idx]["gm"]    = float(gm)
            if level is not None:
                self._trims[fixture_idx]["level"] = float(level)

    def get_trims(self) -> list:
        """Return a copy of the current trim list (thread-safe)."""
        with self._lock:
            return [dict(t) for t in self._trims]

    def _print_matrix(self):
        names = [l["name"] for l in self.leds]
        col_w = max(len(n) for n in names) + 2
        print("\n-- LED Gamut Matrix (normalised XYZ primaries) ------")
        print("       " + "".join(f"{n:>{col_w}}" for n in names))
        for label, row in zip(["  X", "  Y", "  Z"], self.M):
            print(label + "".join(f"{v:>{col_w}.4f}" for v in row))
        print()

    def process(self, dmx_data: bytes, debug: bool = False) -> None:
        """
        Process one 512-byte sACN DMX frame.

        Called from the receiver thread; all exceptions are caught and
        printed explicitly because the sacn library silently discards any
        unhandled exception thrown inside a callback.
        """
        self.last_rx_time = time.monotonic()
        self.rx_count    += 1
        try:
            new_out = list(self._out_buf)

            for i in range(self.num_fixtures):
                snap     = self._snapshots[i]
                in_base  = (self.in_start  - 1) + i * 6
                out_base = (self.out_start - 1) + i * self.footprint

                if in_base + 6 > len(dmx_data):
                    self._zero_fixture(new_out, out_base, snap)
                    continue

                dhi = dmx_data[in_base];     dlo = dmx_data[in_base + 1]
                xhi = dmx_data[in_base + 2]; xlo = dmx_data[in_base + 3]
                yhi = dmx_data[in_base + 4]; ylo = dmx_data[in_base + 5]

                snap.raw_dim_hi = dhi; snap.raw_dim_lo = dlo
                snap.raw_x_hi   = xhi; snap.raw_x_lo   = xlo
                snap.raw_y_hi   = yhi; snap.raw_y_lo   = ylo

                intensity = unpack16(dhi, dlo)
                x = float(np.clip(unpack16(xhi, xlo) * self.xy_scale, 0.0,  0.899))
                y = float(np.clip(unpack16(yhi, ylo) * self.xy_scale, 1e-6, 0.899))
                if x + y >= 1.0:
                    t = x + y + 1e-6
                    x = (x / t) * 0.998
                    y = (y / t) * 0.998

                snap.dim = intensity
                snap.cx  = x
                snap.cy  = y

                # Read this fixture's live trim values
                gm_trim    = self._trims[i]["gm"]
                level_trim = self._trims[i]["level"]
                snap.gm_trim    = gm_trim
                snap.level_trim = level_trim

                # Apply green/magenta trim in CIE 1960 uv space
                tx, ty = apply_gm_trim(x, y, gm_trim)

                if self.has_int_ch:
                    target_XYZ = xy_intensity_to_XYZ(tx, ty, intensity)
                    weights    = solve_led_weights(target_XYZ, self.M,
                                                   has_intensity_ch=True)
                else:
                    solve_x, solve_y = clamp_xy_to_gamut(tx, ty,
                                                         self._gamut_xy)
                    unit_XYZ = xy_intensity_to_XYZ(solve_x, solve_y, 1.0)
                    weights  = solve_led_weights(unit_XYZ, self.M,
                                                 has_intensity_ch=False)
                    weights  = np.clip(weights * intensity, 0.0, 1.0)

                # Apply level trim to all chip outputs
                weights = np.clip(weights * level_trim, 0.0, 1.0)

                # ---- Adaptive EMA smoothing ----
                # Choose alpha based on how large the move is.
                # Intensity and chip weights are smoothed independently
                # so a dimmer snap doesn't force slow-smooth chip changes.
                prev_w  = self._sw[i]
                prev_si = self._si[i]

                w_delta  = float(np.max(np.abs(weights - prev_w)))
                alpha_w  = self._s_fast if w_delta > self._s_thr \
                           else self._s_slow

                int_val  = float(np.clip(intensity * level_trim, 0.0, 1.0)) \
                           if self.has_int_ch else 0.0
                i_delta  = abs(int_val - prev_si)
                alpha_i  = self._s_fast if i_delta > self._s_thr \
                           else self._s_slow

                smooth_w  = prev_w  + (weights  - prev_w)  * alpha_w
                smooth_si = prev_si + (int_val  - prev_si) * alpha_i

                self._sw[i] = smooth_w
                self._si[i] = smooth_si

                # Write smoothed values to DMX buffer
                pos   = out_base
                scale = 65535 if self.bit_depth == 16 else 255

                if self.has_int_ch:
                    snap.out_intensity = int(round(smooth_si * scale))
                    pos = write_channel(new_out, pos, smooth_si,
                                        self.bit_depth, gamma=1.0)

                chip_vals = []
                for w in smooth_w:
                    v_gamma = float(np.clip(w, 0, 1))
                    if self.gamma != 1.0:
                        v_gamma = v_gamma ** (1.0 / self.gamma)
                    chip_vals.append(int(round(v_gamma * scale)))
                    pos = write_channel(new_out, pos, float(w),
                                        self.bit_depth, self.gamma)
                snap.out_vals = chip_vals

                if debug:
                    print(
                        f"  [#{self.rx_count:>5} fix{i+1}]"
                        f" dim={intensity:.3f} x={x:.4f} y={y:.4f}"
                        f" gm={gm_trim:+.4f} lvl={level_trim:.2f}"
                        f" tx={tx:.4f} ty={ty:.4f}"
                        f" αw={alpha_w:.2f} αi={alpha_i:.2f}"
                        f" w={[round(float(w),3) for w in smooth_w]}"
                        f" out={chip_vals}",
                        flush=True,
                    )

            with self._lock:
                self._out_buf = new_out

        except Exception:
            print(f"\n  EXCEPTION in process() packet #{self.rx_count}:", flush=True)
            traceback.print_exc()

    def _zero_fixture(self, buf: list, out_base: int,
                      snap: FixtureSnapshot) -> None:
        bpc = 2 if self.bit_depth == 16 else 1
        n_write = (bpc if self.has_int_ch else 0) + self.num_chips * bpc
        for j in range(n_write):
            if out_base + j < 512:
                buf[out_base + j] = 0
        snap.out_intensity = 0
        snap.out_vals = [0] * self.num_chips

    def get_frame(self):
        """Return a copy of (output_buf, snapshots) under lock."""
        with self._lock:
            return list(self._out_buf), list(self._snapshots)
