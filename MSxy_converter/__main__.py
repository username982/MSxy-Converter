# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Mark Stuen
"""
__main__.py
-----------
Entry point for both:
  python -m MSxy_converter [config_file] [--debug] [--gui]
  MSxy-converter                 [config_file] [--debug] [--gui]

Flags
-----
--gui          Launch the graphical interface instead of CLI mode.
--debug        Per-frame solver output (CLI mode only).
--in_terminal  Internal sentinel added by the launcher — do not use manually.
--no-pause     Suppress the pause-on-exit prompt for process supervisors.
"""

import sys
import time
import threading
import traceback

from .launcher import needs_relaunch, relaunch_in_terminal, _pause_before_exit


def main():
    # --- GUI mode ---
    if "--gui" in sys.argv:
        plain_args  = [a for a in sys.argv[1:] if not a.startswith("--")]
        config_file = plain_args[0] if plain_args else "led_config.json"
        from .gui import run_gui
        run_gui(config_file)
        return

    # --- Re-launch inside a real terminal if opened by double-click ---
    if needs_relaunch():
        if relaunch_in_terminal():
            sys.exit(0)

    import sacn
    from .wizard    import load_or_create_config
    from .converter import ColorConverter, fixture_footprint
    from .network   import receiver_loop, sender_loop
    from .display   import status_loop
    from .cct       import xy_to_cct_duv

    debug       = "--debug" in sys.argv
    plain_args  = [a for a in sys.argv[1:] if not a.startswith("--")]
    config_file = plain_args[0] if plain_args else "led_config.json"

    # --- Config / wizard ---
    try:
        cfg = load_or_create_config(config_file)
    except KeyboardInterrupt:
        print("\n\nSetup cancelled.")
        _pause_before_exit()
        sys.exit(0)
    except Exception:
        print("\n\nFATAL ERROR during setup:")
        traceback.print_exc()
        _pause_before_exit("Setup failed — see error above.")
        sys.exit(1)

    # --- Startup ---
    try:
        in_uni    = cfg["input_universe"]
        out_uni   = cfg["output_universe"]
        n_fix     = cfg["num_fixtures"]
        in_start  = cfg.get("input_start_addr",  1)
        out_start = cfg.get("output_start_addr", 1)
        bind_ip   = cfg.get("bind_ip",  "0.0.0.0")
        send_ip   = cfg.get("send_ip",  bind_ip)
        bit_depth = cfg.get("output_bit_depth", 8)
        has_int   = cfg.get("has_intensity_ch", False)
        ctrl_chs  = cfg.get("control_channels", [])
        led_names = [l["name"] for l in cfg["leds"]]
        fp        = fixture_footprint(cfg)
        bpc       = 2 if bit_depth == 16 else 1

        converter  = ColorConverter(cfg)
        stop_event = threading.Event()

        sender = sacn.sACNsender(bind_address=send_ip, fps=44)
        sender.start()
        sender.activate_output(out_uni)

        if cfg.get("output_ip"):
            sender[out_uni].destination = cfg["output_ip"]
            sender[out_uni].multicast   = False
            net_dest = f"unicast -> {cfg['output_ip']}"
        else:
            sender[out_uni].multicast = True
            net_dest = "multicast"

    except Exception:
        print("\n\nFATAL ERROR during startup:")
        traceback.print_exc()
        _pause_before_exit("Startup failed — see error above.")
        sys.exit(1)

    # --- Running banner ---
    in_last  = in_start  + n_fix * 6  - 1
    out_last = out_start + n_fix * fp - 1
    fp_parts = []
    if has_int:    fp_parts.append(f"Int({bpc}ch)")
    fp_parts.append(f"{len(led_names)} chips×{bpc}ch")
    if ctrl_chs:   fp_parts.append(f"{len(ctrl_chs)} ctrl")
    fp_desc   = " + ".join(fp_parts) + f" = {fp} ch/fixture"
    chips_str = ", ".join(led_names)

    W = 44
    print(f"\n+{'-'*(W+4)}+")
    print(f"|  {'Running':<{W}}  |")
    print(f"|  {'RX bind     : ' + bind_ip:<{W}}  |")
    print(f"|  {'TX bind     : ' + send_ip:<{W}}  |")
    print(f"|  {f'Input  uni {in_uni}: ch {in_start}-{in_last}':<{W}}  |")
    print(f"|  {f'Output uni {out_uni}: ch {out_start}-{out_last}':<{W}}  |")
    print(f"|  {f'Fixtures    : {n_fix}  {fp_desc}':<{W}}  |")
    print(f"|  {'LED chips   : ' + chips_str[:W-14]:<{W}}  |")
    if len(chips_str) > W - 14:
        print(f"|  {'  ' + chips_str[W-14:(W-14)*2]:<{W}}  |")
    if ctrl_chs:
        print(f"|  {'Ctrl chs    : ' + ', '.join(ctrl_chs)[:W-14]:<{W}}  |")
    print(f"|  {f'Bit depth   : {bit_depth}':<{W}}  |")
    print(f"|  {f'XY scale    : ' + str(cfg.get('xy_scale', 1.0)):<{W}}  |")
    print(f"|  {f'Gamma       : ' + str(cfg.get('output_gamma', 1.0)):<{W}}  |")
    print(f"|  {'Output dest : ' + net_dest:<{W}}  |")
    print(f"|  {'CCT + Duv   : shown when near Planckian locus':<{W}}  |")
    if debug:
        print(f"|  {'DEBUG ON (every frame printed)':<{W}}  |")
    print(f"|  {'Ctrl+C to stop':<{W}}  |")
    print(f"+{'-'*(W+4)}+\n")

    # --- Worker threads ---
    threading.Thread(
        target=receiver_loop,
        args=(bind_ip, in_uni, converter, stop_event),
        daemon=True,
    ).start()

    threading.Thread(
        target=sender_loop,
        args=(sender, out_uni, converter, 44.0),
        daemon=True,
    ).start()

    if debug:
        _orig = converter.process
        converter.process = lambda dmx: _orig(dmx, debug=True)

    # Inject CCT function into status_loop via a patched snapshot reader
    threading.Thread(
        target=status_loop,
        args=(converter, n_fix, xy_to_cct_duv),
        daemon=True,
    ).start()

    # --- Main loop ---
    try:
        while True:
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\n\nShutting down...")
    except Exception:
        print("\n\nUNEXPECTED ERROR:")
        traceback.print_exc()
        _pause_before_exit("Unexpected error — see above.")
    finally:
        stop_event.set()
        try:
            sender.stop()
        except Exception:
            pass
        print("Done.")
        _pause_before_exit()


if __name__ == "__main__":
    main()
