# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Mark Stuen
"""
wizard.py
---------
Interactive first-time setup wizard and config file management.
"""

import json
import os
import socket

try:
    import psutil
    HAS_PSUTIL = True
except ImportError:
    HAS_PSUTIL = False

from .presets import PRESETS
from .converter import fixture_footprint
from .gdtf_loader import (
    find_gdtf_files, parse_gdtf, mode_to_fixture_cfg,
    GDTF_FOLDER, GDTFFixture,
)


# ---------------------------------------------------------------------------
# Interface enumeration
# ---------------------------------------------------------------------------

def list_interfaces():
    ifaces = []
    if HAS_PSUTIL:
        for name, addrs in psutil.net_if_addrs().items():
            for addr in addrs:
                if addr.family == socket.AF_INET:
                    ifaces.append((name, addr.address))
    else:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ifaces.append(("primary", s.getsockname()[0]))
            s.close()
        except Exception:
            pass
        ifaces.append(("all interfaces", "0.0.0.0"))
    return ifaces


def pick_interface(label="network interface"):
    ifaces = list_interfaces()
    print(f"\n  Available interfaces ({label}):")
    for idx, (name, ip) in enumerate(ifaces):
        print(f"    [{idx}]  {name:<22}  {ip}")
    print(f"    [*]  Enter IP manually")
    while True:
        raw = input("  Choose [0]: ").strip()
        if raw == "":
            return ifaces[0][1]
        if raw == "*":
            return input("  Enter IP: ").strip()
        try:
            idx = int(raw)
            if 0 <= idx < len(ifaces):
                return ifaces[idx][1]
            print(f"  ! Enter 0-{len(ifaces)-1}")
        except ValueError:
            print("  ! Enter a number or *")


# ---------------------------------------------------------------------------
# Primitive input helpers
# ---------------------------------------------------------------------------

def get_int(prompt, default=None, min_val=None, max_val=None):
    while True:
        suffix = f" [{default}]" if default is not None else ""
        try:
            raw = input(f"{prompt}{suffix}: ").strip()
            if raw == "" and default is not None:
                return default
            val = int(raw)
            if min_val is not None and val < min_val:
                print(f"  ! Must be >= {min_val}"); continue
            if max_val is not None and val > max_val:
                print(f"  ! Must be <= {max_val}"); continue
            return val
        except ValueError:
            print("  ! Enter a whole number.")


def get_float(prompt, default=None, min_val=None, max_val=None):
    while True:
        suffix = f" [{default}]" if default is not None else ""
        try:
            raw = input(f"{prompt}{suffix}: ").strip()
            if raw == "" and default is not None:
                return default
            val = float(raw)
            if min_val is not None and val < min_val:
                print(f"  ! Must be >= {min_val}"); continue
            if max_val is not None and val > max_val:
                print(f"  ! Must be <= {max_val}"); continue
            return val
        except ValueError:
            print("  ! Enter a decimal number.")


def get_yn(prompt, default=True):
    hint = "[Y/n]" if default else "[y/N]"
    while True:
        raw = input(f"  {prompt} {hint}: ").strip().lower()
        if raw == "":
            return default
        if raw in ("y", "yes"):
            return True
        if raw in ("n", "no"):
            return False
        print("  ! Enter y or n")


# ---------------------------------------------------------------------------
# GDTF file picker
# ---------------------------------------------------------------------------

def _pick_gdtf_mode(fixture: GDTFFixture) -> dict:
    """
    Let the user choose a DMX mode from a parsed GDTF fixture,
    then returns a fixture config dict (same shape as pick_led_preset).
    """
    print(f"\n  Fixture : {fixture.manufacturer} {fixture.name}")
    print(f"  File    : {fixture.filename}\n")

    if not fixture.modes:
        print("  ! No DMX modes found in this GDTF file.")
        return None

    print("  Available DMX modes:\n")
    for idx, mode in enumerate(fixture.modes):
        colour_names = ", ".join(c.display_name for c in mode.colour_channels)
        ctrl_names   = ", ".join(c.display_name for c in mode.control_channels)
        int_note     = "+Intensity " if mode.intensity_channels else ""
        print(f"  [{idx}]  {mode.name}  ({mode.channel_count} ch total)")
        print(f"         {int_note}colour: {colour_names or '(none detected)'}")
        if ctrl_names:
            print(f"         ctrl  : {ctrl_names}")
        print()

    while True:
        raw = input("  Choose mode [0]: ").strip() or "0"
        try:
            idx = int(raw)
            if 0 <= idx < len(fixture.modes):
                break
            print(f"  ! Enter 0-{len(fixture.modes)-1}")
        except ValueError:
            print("  ! Enter a number")

    mode = fixture.modes[idx]

    # Ask bit depth before converting so mode_to_fixture_cfg can record it
    print("\n-- Output Bit Depth ----------------------------------")
    print("  8-bit  : 1 DMX channel per chip / intensity  (0-255)")
    print("  16-bit : 2 DMX channels per chip / intensity (MSB then LSB)")
    # Suggest 16-bit if the GDTF mode has any 16-bit channels
    has_16 = any(c.is_16bit for c in mode.channels)
    default_depth = 16 if has_16 else 8
    print(f"  GDTF reports {'16-bit' if has_16 else '8-bit'} channels in this mode.\n")
    while True:
        raw = input(f"  Bit depth [8 or 16] [{default_depth}]: ").strip()
        if raw == "":
            bit_depth = default_depth; break
        if raw in ("8", "16"):
            bit_depth = int(raw); break
        print("  ! Enter 8 or 16")

    cfg = mode_to_fixture_cfg(fixture, mode, bit_depth=bit_depth)
    return cfg


def _pick_gdtf_file(gdtf_files: list) -> dict | None:
    """
    Show a numbered list of GDTF files, let the user pick one,
    parse it, pick a mode, and return a fixture config dict.
    Returns None if the user skips GDTF.
    """
    print("\n-- GDTF Fixture Profiles -----------------------------")
    print(f"  Found {len(gdtf_files)} GDTF file(s) in ./{GDTF_FOLDER}/\n")
    for idx, path in enumerate(gdtf_files):
        basename = os.path.basename(path)
        # Try a quick parse just for the display name
        try:
            fx = parse_gdtf(path)
            label = f"{fx.manufacturer}  {fx.name}  ({len(fx.modes)} mode(s))"
        except Exception:
            label = basename
        print(f"  [{idx}]  {label}")
        print(f"         {basename}")
        print()
    print(f"  [s]  Skip — use built-in presets instead\n")

    while True:
        raw = input("  Choose GDTF file [0]: ").strip().lower() or "0"
        if raw == "s":
            return None
        try:
            idx = int(raw)
            if 0 <= idx < len(gdtf_files):
                break
            print(f"  ! Enter 0-{len(gdtf_files)-1} or s")
        except ValueError:
            print("  ! Enter a number or s")

    filepath = gdtf_files[idx]
    print(f"\n  Parsing {os.path.basename(filepath)}...")
    try:
        fixture = parse_gdtf(filepath)
    except Exception as e:
        print(f"  ! Failed to parse GDTF: {e}")
        print("  Falling back to built-in presets.\n")
        return None

    return _pick_gdtf_mode(fixture)


# ---------------------------------------------------------------------------
# Shared chip editor (used by both GDTF and preset flows)
# ---------------------------------------------------------------------------

def _edit_chips(base_leds: list) -> list:
    """
    Prompt the user to review / edit each chip's CIE xy and flux.
    Chips flagged with _needs_xy=True get an extra warning.
    Returns a clean list of {name, x, y, flux} dicts.
    """
    print("\n  Review CIE 1931 (x,y) and relative flux for each chip.")
    print("  Press Enter to accept the value shown in [brackets].")
    print("  Chips marked [!] have no emitter data in the GDTF —")
    print("  please enter measured or datasheet values.\n")
    leds = []
    for d in base_leds:
        needs = d.get("_needs_xy", False)
        flag  = "  [!] " if needs else "  >   "
        print(f"{flag}{d['name']}")
        x    = get_float("    CIE x",         d["x"],    0.001, 0.899)
        y    = get_float("    CIE y",          d["y"],    0.001, 0.899)
        flux = get_float("    Relative flux",  d["flux"], 0.01)
        leds.append({"name": d["name"], "x": x, "y": y, "flux": flux})
        print()
    return leds


# ---------------------------------------------------------------------------
# Preset / LED chip picker
# ---------------------------------------------------------------------------

def pick_led_preset():
    """
    Interactive fixture configuration.

    Checks for GDTF files first; if found, offers them before the
    built-in preset list.  In both flows the user can review and edit
    every chip's CIE xy and flux before proceeding.

    Returns a dict with keys:
      leds, has_intensity_ch, output_bit_depth, control_channels
    """
    print("\n-- Fixture / LED Chip Configuration ------------------")

    # --- GDTF path ---
    gdtf_files = find_gdtf_files()
    base_cfg   = None

    if gdtf_files:
        base_cfg = _pick_gdtf_file(gdtf_files)

    if base_cfg is not None:
        # GDTF was chosen — user edits chips, then confirm intensity/ctrl
        gdtf_source = base_cfg.pop("_gdtf_source", "GDTF")
        missing_xy  = base_cfg.pop("_missing_xy",  [])

        print(f"\n  Loaded from: {gdtf_source}")
        if missing_xy:
            print(f"  Chips with no emitter data in GDTF "
                  f"(marked [!]): {', '.join(missing_xy)}")

        base_leds  = base_cfg["leds"]
        has_int    = base_cfg["has_intensity_ch"]
        bit_depth  = base_cfg["output_bit_depth"]
        ctrl_names = base_cfg["control_channels"]

        leds = _edit_chips(base_leds)

        # Confirm intensity channel (GDTF may have detected it)
        print("-- Intensity Channel ---------------------------------")
        print("  GDTF Dimmer channel detected." if has_int
              else "  No Dimmer channel detected in this mode.")
        has_int = get_yn("  Does this fixture have an intensity channel?",
                         default=has_int)

        # Confirm control channels
        print("\n-- Control Channels ----------------------------------")
        if ctrl_names:
            print(f"  GDTF reports {len(ctrl_names)} control channel(s): "
                  f"{', '.join(ctrl_names)}")
        else:
            print("  No control channels detected in GDTF mode.")
        ctrl_count = get_int("  Number of control channels",
                              len(ctrl_names), 0, 20)
        if ctrl_count != len(ctrl_names):
            ctrl_names = []
            if ctrl_count > 0 and get_yn("  Name the control channels?", True):
                for i in range(ctrl_count):
                    name = input(f"  Control ch {i+1} name [Ctrl{i+1}]: ").strip()
                    ctrl_names.append(name or f"Ctrl{i+1}")
            else:
                ctrl_names = [f"Ctrl{i+1}" for i in range(ctrl_count)]

        return {
            "leds":             leds,
            "has_intensity_ch": has_int,
            "output_bit_depth": bit_depth,
            "control_channels": ctrl_names,
        }

    # --- Built-in preset path ---
    if gdtf_files:
        print("\n  Using built-in presets.\n")
    else:
        print(f"\n  (No GDTF files found in ./{GDTF_FOLDER}/ — "
              f"drop .gdtf files there to use fixture profiles)\n")

    print("  Select a fixture type:\n")
    for key, p in PRESETS.items():
        chip_names = ", ".join(l["name"] for l in p["leds"]) if p["leds"] else "..."
        ctrl_note  = f"  +{len(p['control_channels'])} ctrl ch" if p["control_channels"] else ""
        int_note   = "  +Intensity" if p["has_intensity_ch"] else ""
        print(f"  [{key}]  {p['label']}")
        if p["leds"]:
            print(f"         chips : {chip_names}")
        if int_note or ctrl_note:
            print(f"         output:{int_note}{ctrl_note}")
        print()

    while True:
        choice = input("  Choose preset [4]: ").strip() or "4"
        if choice in PRESETS:
            break
        print(f"  ! Enter a number 1-{len(PRESETS)}")

    preset = PRESETS[choice]
    print(f"\n  Selected: {preset['label']}")

    if choice == "6":
        n = get_int("  Number of LED chips", 3, 1, 20)
        base_leds = [
            {"name": input(f"  Chip {i+1} name: ").strip() or f"Ch{i+1}",
             "x": 0.333, "y": 0.333, "flux": 50.0}
            for i in range(n)
        ]
    else:
        base_leds = [dict(l) for l in preset["leds"]]

    leds = _edit_chips(base_leds)

    # Intensity channel
    print("-- Intensity Channel ---------------------------------")
    print("  Some fixtures (e.g. ETC Fos/4) expect a master intensity")
    print("  byte as their first output DMX channel.  When enabled,")
    print("  the decoded 16-bit dimmer is written there so the fixture")
    print("  handles its own dimming curve.\n")
    has_int = get_yn("  Does this fixture have an intensity channel?",
                     default=preset["has_intensity_ch"])

    # Bit depth
    print("\n-- Output Bit Depth ----------------------------------")
    print("  8-bit  : 1 DMX channel per chip / intensity  (0-255)")
    print("  16-bit : 2 DMX channels per chip / intensity (MSB then LSB)\n")
    while True:
        raw = input(f"  Bit depth [8 or 16] [{preset['output_bit_depth']}]: ").strip()
        if raw == "":
            bit_depth = preset["output_bit_depth"]; break
        if raw in ("8", "16"):
            bit_depth = int(raw); break
        print("  ! Enter 8 or 16")

    # Control channels
    print("\n-- Control Channels ----------------------------------")
    print("  Trailing channels that belong to the fixture footprint")
    print("  but are never written (strobe, curve, fan, etc.).  They")
    print("  are counted in the address map so multi-fixture patching")
    print("  stays correct and are held at 0.\n")
    if preset["control_channels"]:
        print(f"  Preset default: {len(preset['control_channels'])} "
              f"({', '.join(preset['control_channels'])})\n")
    ctrl_count = get_int("  Number of control channels",
                         len(preset["control_channels"]), 0, 20)
    ctrl_names = []
    if ctrl_count > 0 and get_yn("  Name the control channels?", default=True):
        defaults = preset["control_channels"] + [f"Ctrl{i+1}" for i in range(ctrl_count)]
        for i in range(ctrl_count):
            name = input(f"  Control ch {i+1} name [{defaults[i]}]: ").strip()
            ctrl_names.append(name if name else defaults[i])
    else:
        ctrl_names = [f"Ctrl{i+1}" for i in range(ctrl_count)]

    return {
        "leds":             leds,
        "has_intensity_ch": has_int,
        "output_bit_depth": bit_depth,
        "control_channels": ctrl_names,
    }


# ---------------------------------------------------------------------------
# Full wizard
# ---------------------------------------------------------------------------

def run_wizard(config_file: str) -> dict:
    print("\n+----------------------------------------------------+")
    print("|   sACN Color Space Converter -- First-Time Setup   |")
    print("+----------------------------------------------------+\n")
    cfg = {}

    if not HAS_PSUTIL:
        print("  TIP: pip install psutil  for a full interface list\n")

    print("-- Network Interfaces --------------------------------")
    print("  Pick the adapter on the same subnet as your console")
    print("  and LED driver.  For loopback testing use 127.0.0.1\n")
    cfg["bind_ip"] = pick_interface("receive")
    diff = input("\n  Use same interface for output? [Y/n]: ").strip().lower()
    cfg["send_ip"] = pick_interface("send") if diff == "n" else cfg["bind_ip"]
    print(f"\n  RX bind : {cfg['bind_ip']}")
    print(f"  TX bind : {cfg['send_ip']}")

    fixture_cfg = pick_led_preset()
    cfg.update(fixture_cfg)

    fp        = fixture_footprint(cfg)
    num_chips = len(cfg["leds"])
    bpc       = 2 if cfg["output_bit_depth"] == 16 else 1

    max_fix = min(512 // 6, 512 // fp if fp > 0 else 512)

    print("\n-- Universe & Fixture Settings -----------------------")
    cfg["input_universe"]   = get_int("  Input  universe  (from console)", 1, 1, 63999)
    cfg["output_universe"]  = get_int("  Output universe  (to LED driver)", 2, 1, 63999)
    cfg["num_fixtures"]     = get_int(f"  Number of fixtures (max {max_fix})", 1, 1, max_fix)

    int_label  = f" + 1 Int({bpc}ch)" if cfg["has_intensity_ch"] else ""
    ctrl_label = f" + {len(cfg['control_channels'])} ctrl" if cfg["control_channels"] else ""
    print(f"\n  Output footprint: {fp} ch/fixture"
          f"  ({num_chips} chips x {bpc}ch{int_label}{ctrl_label})")
    cfg["input_start_addr"]  = get_int("  Input  start address (1-based DMX ch)", 1, 1, 512)
    cfg["output_start_addr"] = get_int("  Output start address (1-based DMX ch)", 1, 1, 512)

    last_in  = cfg["input_start_addr"]  + cfg["num_fixtures"] * 6  - 1
    last_out = cfg["output_start_addr"] + cfg["num_fixtures"] * fp - 1
    if last_in  > 512: print(f"  WARNING: input  reaches ch {last_in}  (> 512)")
    if last_out > 512: print(f"  WARNING: output reaches ch {last_out} (> 512)")

    print("\n-- Output Destination --------------------------------")
    raw = input("  Destination IP  (blank = multicast): ").strip()
    cfg["output_ip"] = raw if raw else None

    print("\n-- CIE XY Encoding -----------------------------------")
    print("  1.0 = ETC EOS / grandMA / Chamsys (most common)")
    cfg["xy_scale"] = get_float("  XY full-scale value", 1.0, 0.05, 1.0)

    print("\n-- Output Gamma --------------------------------------")
    print("  1.0 = linear (recommended)   2.2 = perceptual")
    cfg["output_gamma"] = get_float("  Output gamma", 1.0, 0.5, 3.0)

    with open(config_file, "w") as fh:
        json.dump(cfg, fh, indent=2)
    print(f"\n  Config saved -> {config_file}\n")
    return cfg


def load_or_create_config(config_file: str) -> dict:
    """Load an existing config or run the setup wizard."""
    if os.path.exists(config_file):
        print(f"\nExisting config found: {config_file}")
        if input("Use it? [Y/n]: ").strip().lower() != "n":
            with open(config_file) as fh:
                cfg = json.load(fh)
            # Back-fill keys added in later versions
            cfg.setdefault("input_start_addr",  1)
            cfg.setdefault("output_start_addr", 1)
            cfg.setdefault("bind_ip",           "0.0.0.0")
            cfg.setdefault("send_ip",           "0.0.0.0")
            cfg.setdefault("has_intensity_ch",  False)
            cfg.setdefault("output_bit_depth",  8)
            cfg.setdefault("control_channels",  [])
            return cfg
        print()
    return run_wizard(config_file)
