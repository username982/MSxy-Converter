# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Mark Stuen
"""
display.py
----------
Status display loop — prints a live per-fixture table every N seconds
showing decoded input values and computed output DMX levels.
"""

import time


def status_loop(converter, num_fixtures: int,
                cct_fn=None, interval: float = 2.0) -> None:
    """
    Thread target: periodically print a status table to stdout.

    Columns shown:
      INPUT  — DMX ch, raw 16-bit Dim/X/Y, decoded dim/CIEx/CIEy, CCT+Duv
      OUTPUT — DMX ch, intensity (if enabled), per-chip values
               control channels noted in header but not shown as columns
    """
    prev_count = 0
    prev_time  = time.monotonic()

    led_names  = [l["name"] for l in converter.leds]
    has_int    = converter.has_int_ch
    ctrl_names = converter.ctrl_chs
    bit_depth  = converter.bit_depth
    max_val    = 65535 if bit_depth == 16 else 255

    W_FIX   = 4
    W_CH    = 4
    W_RAW   = 6
    W_FLOAT = 6

    out_col_names = (["Int"] if has_int else []) + led_names
    W_OUT = max(5 if bit_depth == 16 else 4,
                max(len(n) for n in out_col_names) + 1)

    hdr_in = (f"{'Ch':>{W_CH}} {'Dim16':>{W_RAW}} {'X16':>{W_RAW}} {'Y16':>{W_RAW}}"
              f"  {'Dim':>{W_FLOAT}} {'CIEx':>{W_FLOAT}} {'CIEy':>{W_FLOAT}}"
              f"  {'CCT / Duv':<20}")

    ctrl_note = (f"  +{len(ctrl_names)} ctrl ({', '.join(ctrl_names)})"
                 if ctrl_names else "")
    hdr_out   = (f"{'Ch':>{W_CH}}"
                 + "".join(f" {n[:W_OUT]:>{W_OUT}}" for n in out_col_names)
                 + ctrl_note)

    divider = "-" * (W_FIX + 4 + len(hdr_in) + 4 + len(hdr_out))

    while True:
        time.sleep(interval)
        now        = time.monotonic()
        elapsed    = now - prev_time
        delta      = converter.rx_count - prev_count
        rx_hz      = delta / elapsed if elapsed > 0 else 0.0
        prev_count = converter.rx_count
        prev_time  = now

        _, snaps = converter.get_frame()
        lines = [
            "",
            (f"  RX: {rx_hz:.1f} Hz   total packets: {converter.rx_count}"
             f"   [{bit_depth}-bit output  max={max_val}]"),
            f"  {'Fix':>{W_FIX}}  {'--- INPUT ---':^{len(hdr_in)}}    --- OUTPUT ---",
            f"  {'':>{W_FIX}}  {hdr_in}    {hdr_out}",
            "  " + divider,
        ]
        for i, s in enumerate(snaps[:num_fixtures]):
            dim16 = (s.raw_dim_hi << 8) | s.raw_dim_lo
            x16   = (s.raw_x_hi   << 8) | s.raw_x_lo
            y16   = (s.raw_y_hi   << 8) | s.raw_y_lo

            # CCT + Duv
            cct_str = ""
            if cct_fn and (s.cx > 0 or s.cy > 0):
                result = cct_fn(s.cx, s.cy)
                if result:
                    cct, duv = result
                    sign = "+" if duv >= 0 else ""
                    cct_str = f"{cct}K {sign}{duv:.4f}"

            in_p = (f"{s.in_start_ch:>{W_CH}} {dim16:>{W_RAW}} {x16:>{W_RAW}}"
                    f" {y16:>{W_RAW}}  {s.dim:>{W_FLOAT}.3f}"
                    f" {s.cx:>{W_FLOAT}.4f} {s.cy:>{W_FLOAT}.4f}"
                    f"  {cct_str:<20}")
            out_vals = (([s.out_intensity] if has_int else []) + s.out_vals)
            out_p = (f"{s.out_start_ch:>{W_CH}}"
                     + "".join(f" {v:>{W_OUT}}" for v in out_vals))
            lines.append(f"  {i+1:>{W_FIX}}  {in_p}    {out_p}")

        print("\n".join(lines), flush=True)
