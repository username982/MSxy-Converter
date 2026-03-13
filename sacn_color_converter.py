# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Mark Stuen
#!/usr/bin/env python3
"""
sACN Intensity/XY -> Multi-chip LED Color Space Converter
=========================================================
Receives sACN (E1.31) via raw UDP socket, converts 16-bit
Dimmer + CIE X + CIE Y to any number of LED output channels,
and re-transmits as sACN.

Input channel layout per fixture (6 DMX channels, always):
  Ch +0/+1 : Dimmer 16-bit MSB/LSB
  Ch +2/+3 : CIE X  16-bit MSB/LSB
  Ch +4/+5 : CIE Y  16-bit MSB/LSB

Output channel layout per fixture (configured per preset):
  [Intensity]     -- optional passthrough dimmer channel
  [LED chips...]  -- one channel (8-bit) or two channels (16-bit) per chip
  [Control chs]   -- trailing fixture channels (strobe, curve, fan, etc.)
                     included in footprint but held at 0, never overwritten

Usage:
  python sacn_color_converter.py                   # uses led_config.json
  python sacn_color_converter.py fos4.json         # named config
  python sacn_color_converter.py fos4.json --debug # named config + debug

Multiple instances on different universes:
  python sacn_color_converter.py wash.json  &
  python sacn_color_converter.py cyc.json   &
"""

import sacn
import numpy as np
from scipy.optimize import lsq_linear
import json
import time
import threading
import traceback
import socket
import struct
import sys
import os
import zipfile
import xml.etree.ElementTree as ET

try:
    import psutil
    HAS_PSUTIL = True
except ImportError:
    HAS_PSUTIL = False

DEBUG = "--debug" in sys.argv

_args = [a for a in sys.argv[1:] if not a.startswith("--")]
CONFIG_FILE = _args[0] if _args else "led_config.json"

# sACN / E1.31 constants
SACN_PORT         = 5568
SACN_ACN_ID       = b"ASC-E1.17\x00\x00\x00"
SACN_ROOT_VECTOR  = 0x00000004
SACN_FRAME_VECTOR = 0x00000002
SACN_DMP_VECTOR   = 0x02
OFF_ACN_ID     =   4
OFF_ROOT_VEC   =  18
OFF_FRAME_VEC  =  40
OFF_UNIVERSE   = 113
OFF_DMP_VEC    = 117
OFF_START_CODE = 125
OFF_DMX_DATA   = 126


# ---------------------------------------------------------------------------
# LED / fixture presets
#
# Each preset defines:
#   label               -- display name
#   has_intensity_ch    -- True if the fixture expects an intensity channel
#                          as its first output channel
#   output_bit_depth    -- 8 or 16; applies to intensity + all LED chip channels
#   control_channels    -- list of channel names that trail the LED chips
#                          These are included in the footprint but held at 0
#   leds                -- chip list: name, CIE x, CIE y, relative flux
#
# CIE xy values for ETC Fos/4 (LUXEON C emitters, Lumileds DS144 datasheet,
# tested at 350mA Tj=85C):
#   Deep Red  -- derived from 660nm peak wavelength
#   Red       -- derived from 629nm dominant wavelength
#   Amber     -- PC Amber bin 20 center (datasheet Table 7)
#   Lime      -- Lime bin 10 center     (datasheet Table 7)
#   Green     -- derived from 525nm dominant wavelength
#   Cyan      -- derived from 498nm dominant wavelength
#   Blue      -- derived from 472nm dominant wavelength
#   Indigo    -- derived from 450nm peak wavelength
# ---------------------------------------------------------------------------

LED_PRESETS = {
    "1": {
        "label":            "RGB  (3-chip strip / par)",
        "has_intensity_ch": False,
        "output_bit_depth": 8,
        "control_channels": [],
        "leds": [
            {"name": "Red",   "x": 0.690, "y": 0.308, "flux": 30.0},
            {"name": "Green", "x": 0.170, "y": 0.700, "flux": 60.0},
            {"name": "Blue",  "x": 0.135, "y": 0.053, "flux": 10.0},
        ],
    },
    "2": {
        "label":            "RGBA  (RGB + Amber)",
        "has_intensity_ch": False,
        "output_bit_depth": 8,
        "control_channels": [],
        "leds": [
            {"name": "Red",   "x": 0.690, "y": 0.308, "flux": 30.0},
            {"name": "Green", "x": 0.170, "y": 0.700, "flux": 60.0},
            {"name": "Blue",  "x": 0.135, "y": 0.053, "flux": 10.0},
            {"name": "Amber", "x": 0.560, "y": 0.430, "flux": 50.0},
        ],
    },
    "3": {
        "label":            "RGBW  (RGB + Warm White 3200K)",
        "has_intensity_ch": False,
        "output_bit_depth": 8,
        "control_channels": [],
        "leds": [
            {"name": "Red",         "x": 0.690, "y": 0.308, "flux":  30.0},
            {"name": "Green",       "x": 0.170, "y": 0.700, "flux":  60.0},
            {"name": "Blue",        "x": 0.135, "y": 0.053, "flux":  10.0},
            {"name": "White 3200K", "x": 0.430, "y": 0.400, "flux": 100.0},
        ],
    },
    "4": {
        "label":            "RGB + WW/CW  (5-chip, default)",
        "has_intensity_ch": False,
        "output_bit_depth": 8,
        "control_channels": [],
        "leds": [
            {"name": "Red",              "x": 0.690, "y": 0.308, "flux":  30.0},
            {"name": "Green",            "x": 0.170, "y": 0.700, "flux":  60.0},
            {"name": "Blue",             "x": 0.135, "y": 0.053, "flux":  10.0},
            {"name": "Warm White 2700K", "x": 0.461, "y": 0.413, "flux":  80.0},
            {"name": "Cool White 5600K", "x": 0.320, "y": 0.336, "flux": 100.0},
        ],
    },
    "5": {
        "label":            "ETC Fos/4 Panel  -- Direct mode  (12 ch total)",
        "has_intensity_ch": True,
        "output_bit_depth": 8,
        # Strobe/Curve/Fan are the last 3 channels of the 12-ch footprint.
        # They are included in the address count but held at 0.
        "control_channels": ["Strobe", "Curve", "Fan"],
        "leds": [
            # Channel order: Int | DR | R | A | Lm | G | Cy | B | I | Strobe | Curve | Fan
            # CIE xy and fc measured on physical unit with spectrometer.
            {"name": "Deep Red", "x": 0.7134, "y": 0.2798, "flux":  169.0},
            {"name": "Red",      "x": 0.6984, "y": 0.3001, "flux":  350.0},
            {"name": "Amber",    "x": 0.5747, "y": 0.4241, "flux":  771.0},
            {"name": "Lime",     "x": 0.419,  "y": 0.5529, "flux": 2260.0},
            {"name": "Green",    "x": 0.1922, "y": 0.7314, "flux":  960.0},
            {"name": "Cyan",     "x": 0.0717, "y": 0.5173, "flux":  700.0},
            {"name": "Blue",     "x": 0.1232, "y": 0.0906, "flux":  256.0},
            {"name": "Indigo",   "x": 0.1559, "y": 0.0227, "flux":  119.0},
        ],
    },
    "6": {
        "label":            "Custom  (define each chip manually)",
        "has_intensity_ch": False,
        "output_bit_depth": 8,
        "control_channels": [],
        "leds": [],
    },
}


# ---------------------------------------------------------------------------
# sACN packet parser
# ---------------------------------------------------------------------------

def universe_to_multicast(universe):
    hi = (universe >> 8) & 0xFF
    lo =  universe       & 0xFF
    return f"239.255.{hi}.{lo}"


def parse_sacn_packet(data):
    if len(data) < OFF_DMX_DATA + 1:
        return None, None
    if data[OFF_ACN_ID : OFF_ACN_ID + 12] != SACN_ACN_ID:
        return None, None
    if struct.unpack_from(">I", data, OFF_ROOT_VEC)[0]  != SACN_ROOT_VECTOR:
        return None, None
    if struct.unpack_from(">I", data, OFF_FRAME_VEC)[0] != SACN_FRAME_VECTOR:
        return None, None
    if data[OFF_DMP_VEC]    != SACN_DMP_VECTOR:
        return None, None
    if data[OFF_START_CODE] != 0x00:
        return None, None
    universe  = struct.unpack_from(">H", data, OFF_UNIVERSE)[0]
    dmx_bytes = data[OFF_DMX_DATA:]
    if len(dmx_bytes) < 512:
        dmx_bytes = dmx_bytes + bytes(512 - len(dmx_bytes))
    else:
        dmx_bytes = dmx_bytes[:512]
    return dmx_bytes, universe


# ---------------------------------------------------------------------------
# Network interface helpers
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
            ip = s.getsockname()[0]
            s.close()
            ifaces.append(("primary", ip))
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
# CLI helpers
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
# GDTF parser  (self-contained, no extra dependencies beyond stdlib)
# ---------------------------------------------------------------------------

GDTF_FOLDER = "gdtf"

COLOR_ATTRIBUTE_PREFIXES = ("ColorAdd_", "ColorSub_", "ColorMix")
COLOR_ATTRIBUTE_NAMES    = {"Red","Green","Blue","White","Amber","UV","Lime",
                             "Cyan","Indigo","Magenta","Yellow","WW","CW"}
CONTROL_ATTRIBUTE_PREFIXES = ("Shutter","Strobe","Pan","Tilt","Zoom","Focus",
                               "Iris","Prism","Frost","Gobo","Blade","Effects",
                               "Control","Reset","Fan","Macro","Speed","CTC",
                               "CTO","CTB","Function","Mode","Reserved")
ATTRIBUTE_DISPLAY_NAMES = {
    "ColorAdd_R":"Red","ColorAdd_G":"Green","ColorAdd_B":"Blue",
    "ColorAdd_W":"White","ColorAdd_WW":"Warm White","ColorAdd_CW":"Cool White",
    "ColorAdd_RY":"Amber","ColorAdd_GY":"Lime","ColorAdd_CY":"Cyan",
    "ColorAdd_UV":"Indigo/UV","ColorAdd_M":"Magenta","ColorAdd_Y":"Yellow",
    "Dimmer":"Intensity",
}
ATTRIBUTE_FALLBACK_XY = {
    "ColorAdd_R":(0.690,0.308),"ColorAdd_G":(0.170,0.700),
    "ColorAdd_B":(0.135,0.053),"ColorAdd_W":(0.313,0.329),
    "ColorAdd_WW":(0.461,0.413),"ColorAdd_CW":(0.320,0.336),
    "ColorAdd_RY":(0.560,0.430),"ColorAdd_GY":(0.408,0.537),
    "ColorAdd_CY":(0.040,0.450),"ColorAdd_UV":(0.157,0.018),
    "ColorAdd_M":(0.320,0.154),"ColorAdd_Y":(0.419,0.505),
}


def _gdtf_is_colour(attr):
    return (any(attr.startswith(p) for p in COLOR_ATTRIBUTE_PREFIXES)
            or attr in COLOR_ATTRIBUTE_NAMES)

def _gdtf_display_name(attr, emitter_name):
    if emitter_name: return emitter_name
    if attr in ATTRIBUTE_DISPLAY_NAMES: return ATTRIBUTE_DISPLAY_NAMES[attr]
    for p in ("ColorAdd_","ColorSub_","ColorMix_"):
        if attr.startswith(p): return attr[len(p):].replace("_"," ").title()
    return attr.replace("_"," ").title()

def _gdtf_parse_color(s):
    try:
        p = s.strip().split()
        if len(p) >= 3: return float(p[0]), float(p[1]), float(p[2])
        if len(p) == 2: return float(p[0]), float(p[1]), 1.0
    except Exception: pass
    return None

def parse_gdtf_file(filepath):
    """
    Parse a GDTF file.  Returns (manufacturer, name, modes, emitters) where:
      emitters = {name: {x,y,flux}}
      modes    = [{name, channels:[{offset,attribute,display_name,is_colour,
                                    is_intensity,is_16bit,x,y,flux}]}]
    """
    with zipfile.ZipFile(filepath) as zf:
        xml_name = next((n for n in zf.namelist()
                         if n.lower().endswith("device_description.xml")), None)
        if not xml_name:
            raise ValueError("No device_description.xml in GDTF")
        xml_data = zf.read(xml_name)

    root = ET.fromstring(xml_data)
    ft   = root if root.tag == "FixtureType" else root.find(".//FixtureType")
    if ft is None: raise ValueError("No FixtureType element")

    manufacturer = ft.get("Manufacturer","Unknown")
    name = ft.get("LongName") or ft.get("Name","Unknown")

    emitters = {}
    for em in ft.findall(".//Emitter"):
        em_name = em.get("Name","")
        color   = _gdtf_parse_color(em.get("Color",""))
        if color:
            x, y, Y = color
            lum = float(em.get("LuminousFlux", Y)) if em.get("LuminousFlux") else Y
            emitters[em_name] = {"x": x, "y": y, "flux": lum}

    modes = []
    for mode_el in ft.findall(".//DMXMode"):
        mode_name = mode_el.get("Name","Default")
        channels  = []
        for ch_el in mode_el.findall(".//DMXChannel"):
            offset_str = ch_el.get("Offset","")
            if not offset_str or offset_str == "None": continue
            offsets = [int(o) for o in offset_str.split(",") if o.strip().isdigit()]
            if not offsets: continue
            offset   = offsets[0]
            is_16bit = len(offsets) >= 2
            attr = emitter_ref = ""
            for lc in ch_el.findall("LogicalChannel"):
                attr = lc.get("Attribute","")
                for cf in lc.findall("ChannelFunction"):
                    ref = cf.get("EmitterSpectrum") or cf.get("Emitter","")
                    if ref and ref in emitters: emitter_ref = ref
                break
            if not attr: attr = ch_el.get("Attribute","")
            is_intensity = attr == "Dimmer"
            is_colour    = is_intensity or _gdtf_is_colour(attr)
            x = y = flux = None
            if emitter_ref and emitter_ref in emitters:
                x, y, flux = (emitters[emitter_ref]["x"],
                               emitters[emitter_ref]["y"],
                               emitters[emitter_ref]["flux"])
            elif attr in ATTRIBUTE_FALLBACK_XY:
                x, y = ATTRIBUTE_FALLBACK_XY[attr]; flux = 50.0
            channels.append({
                "offset": offset, "attribute": attr,
                "display_name": _gdtf_display_name(attr, emitter_ref),
                "is_colour": is_colour, "is_intensity": is_intensity,
                "is_16bit": is_16bit, "x": x, "y": y, "flux": flux,
            })
        channels.sort(key=lambda c: c["offset"])
        modes.append({"name": mode_name, "channels": channels})

    return manufacturer, name, modes, emitters


def find_gdtf_files():
    if not os.path.isdir(GDTF_FOLDER): return []
    return sorted(os.path.join(GDTF_FOLDER, f)
                  for f in os.listdir(GDTF_FOLDER)
                  if f.lower().endswith(".gdtf"))


def _gdtf_mode_to_cfg(manufacturer, fixture_name, filename, mode, bit_depth=8):
    channels  = mode["channels"]
    has_int   = any(c["is_intensity"] for c in channels)
    colour_ch = [c for c in channels if c["is_colour"] and not c["is_intensity"]]
    ctrl_ch   = [c for c in channels if not c["is_colour"] and not c["is_intensity"]]
    leds = []
    missing = []
    for c in colour_ch:
        if c["x"] is not None:
            leds.append({"name":c["display_name"],"x":round(c["x"],4),
                         "y":round(c["y"],4),"flux":round(c["flux"],1)})
        else:
            leds.append({"name":c["display_name"],"x":0.333,"y":0.333,
                         "flux":50.0,"_needs_xy":True})
            missing.append(c["display_name"])
    return {
        "leds": leds,
        "has_intensity_ch": has_int,
        "output_bit_depth": bit_depth,
        "control_channels": [c["display_name"] for c in ctrl_ch],
        "_gdtf_source": f"{filename} :: {mode['name']}",
        "_missing_xy":  missing,
    }


def _pick_gdtf_flow():
    """Full GDTF file → mode picker.  Returns fixture cfg dict or None."""
    gdtf_files = find_gdtf_files()
    if not gdtf_files: return None, False

    print("\n-- GDTF Fixture Profiles -----------------------------")
    print(f"  Found {len(gdtf_files)} GDTF file(s) in ./{GDTF_FOLDER}/\n")
    labels = []
    for path in gdtf_files:
        try:
            mfr, nm, modes, _ = parse_gdtf_file(path)
            labels.append(f"{mfr}  {nm}  ({len(modes)} mode(s))")
        except Exception:
            labels.append(os.path.basename(path))
    for idx, (path, label) in enumerate(zip(gdtf_files, labels)):
        print(f"  [{idx}]  {label}")
        print(f"         {os.path.basename(path)}")
        print()
    print("  [s]  Skip — use built-in presets instead\n")

    while True:
        raw = input("  Choose GDTF file [0]: ").strip().lower() or "0"
        if raw == "s": return None, True  # (cfg, gdtf_was_offered)
        try:
            idx = int(raw)
            if 0 <= idx < len(gdtf_files): break
            print(f"  ! Enter 0-{len(gdtf_files)-1} or s")
        except ValueError:
            print("  ! Enter a number or s")

    filepath = gdtf_files[idx]
    print(f"\n  Parsing {os.path.basename(filepath)}...")
    try:
        manufacturer, fixture_name, modes, _ = parse_gdtf_file(filepath)
    except Exception as e:
        print(f"  ! Failed to parse GDTF: {e}\n  Falling back to presets.")
        return None, True

    print(f"\n  Fixture : {manufacturer} {fixture_name}\n")
    for idx2, mode in enumerate(modes):
        chs      = mode["channels"]
        col_chs  = [c for c in chs if c["is_colour"] and not c["is_intensity"]]
        ctrl_chs = [c for c in chs if not c["is_colour"] and not c["is_intensity"]]
        int_note = "+Intensity " if any(c["is_intensity"] for c in chs) else ""
        col_str  = ", ".join(c["display_name"] for c in col_chs) or "(none)"
        ctrl_str = ", ".join(c["display_name"] for c in ctrl_chs)
        print(f"  [{idx2}]  {mode['name']}  ({len(chs)} ch)")
        print(f"         {int_note}colour: {col_str}")
        if ctrl_str: print(f"         ctrl  : {ctrl_str}")
        print()

    while True:
        raw = input("  Choose mode [0]: ").strip() or "0"
        try:
            mi = int(raw)
            if 0 <= mi < len(modes): break
            print(f"  ! Enter 0-{len(modes)-1}")
        except ValueError:
            print("  ! Enter a number")

    mode = modes[mi]
    has_16 = any(c["is_16bit"] for c in mode["channels"])
    default_depth = 16 if has_16 else 8
    print("\n-- Output Bit Depth ----------------------------------")
    print(f"  GDTF reports {'16-bit' if has_16 else '8-bit'} channels.\n")
    while True:
        raw = input(f"  Bit depth [8 or 16] [{default_depth}]: ").strip()
        if raw == "": bit_depth = default_depth; break
        if raw in ("8","16"): bit_depth = int(raw); break
        print("  ! Enter 8 or 16")

    cfg = _gdtf_mode_to_cfg(manufacturer, fixture_name,
                             os.path.basename(filepath), mode, bit_depth)
    return cfg, True


def _edit_chips(base_leds):
    """Prompt user to review / edit each chip's xy and flux."""
    print("\n  Review CIE 1931 (x,y) and relative flux for each chip.")
    print("  Press Enter to accept the value shown in [brackets].")
    print("  Chips marked [!] have no emitter data — please enter")
    print("  measured or datasheet values.\n")
    leds = []
    for d in base_leds:
        flag = "  [!] " if d.get("_needs_xy") else "  >   "
        print(f"{flag}{d['name']}")
        x    = get_float("    CIE x",         d["x"],    0.001, 0.899)
        y    = get_float("    CIE y",          d["y"],    0.001, 0.899)
        flux = get_float("    Relative flux",  d["flux"], 0.01)
        leds.append({"name": d["name"], "x": x, "y": y, "flux": flux})
        print()
    return leds


# ---------------------------------------------------------------------------
# Preset / LED chip setup
# ---------------------------------------------------------------------------

def pick_led_preset():
    """
    Show preset menu, let user pick and edit chips, then ask about intensity
    channel, bit depth, and control channels.
    Returns a dict with keys: leds, has_intensity_ch, output_bit_depth,
    control_channels.
    """
    print("\n-- Fixture / LED Chip Configuration ------------------")
    # --- Try GDTF first ---
    gdtf_cfg, gdtf_offered = _pick_gdtf_flow()

    if gdtf_cfg is not None:
        gdtf_source = gdtf_cfg.pop("_gdtf_source", "GDTF")
        missing_xy  = gdtf_cfg.pop("_missing_xy",  [])
        print(f"\n  Loaded from: {gdtf_source}")
        if missing_xy:
            print(f"  Chips with no emitter data (marked [!]): {', '.join(missing_xy)}")

        leds       = _edit_chips(gdtf_cfg["leds"])
        has_int    = gdtf_cfg["has_intensity_ch"]
        bit_depth  = gdtf_cfg["output_bit_depth"]
        ctrl_names = gdtf_cfg["control_channels"]

        print("-- Intensity Channel ---------------------------------")
        print("  GDTF Dimmer channel detected." if has_int
              else "  No Dimmer channel detected in this mode.")
        has_int = get_yn("  Does this fixture have an intensity channel?", has_int)

        print("\n-- Control Channels ----------------------------------")
        if ctrl_names:
            print(f"  GDTF reports {len(ctrl_names)} control channel(s): "
                  f"{', '.join(ctrl_names)}")
        ctrl_count = get_int("  Number of control channels", len(ctrl_names), 0, 20)
        if ctrl_count != len(ctrl_names):
            ctrl_names = []
            if ctrl_count > 0 and get_yn("  Name them?", True):
                for i in range(ctrl_count):
                    n = input(f"  Control ch {i+1} name [Ctrl{i+1}]: ").strip()
                    ctrl_names.append(n or f"Ctrl{i+1}")
            else:
                ctrl_names = [f"Ctrl{i+1}" for i in range(ctrl_count)]

        return {"leds": leds, "has_intensity_ch": has_int,
                "output_bit_depth": bit_depth, "control_channels": ctrl_names}

    # --- Built-in presets ---
    if gdtf_offered:
        print("\n  Using built-in presets.\n")
    else:
        print(f"\n  (No GDTF files found in ./{GDTF_FOLDER}/ — "
              f"drop .gdtf files there to load fixture profiles)\n")

    print("  Select a fixture type:\n")
    for key, p in LED_PRESETS.items():
        chip_names = ", ".join(l["name"] for l in p["leds"]) if p["leds"] else "..."
        ctrl_note  = f"  +{len(p['control_channels'])} ctrl ch" if p["control_channels"] else ""
        int_note   = "  +Intensity" if p["has_intensity_ch"] else ""
        print(f"  [{key}]  {p['label']}")
        if p["leds"]:
            print(f"         chips : {chip_names}")
        if int_note or ctrl_note:
            print(f"         output: {int_note}{ctrl_note}")
        print()

    while True:
        choice = input("  Choose preset [4]: ").strip() or "4"
        if choice in LED_PRESETS: break
        print(f"  ! Enter a number 1-{len(LED_PRESETS)}")

    preset = LED_PRESETS[choice]
    print(f"\n  Selected: {preset['label']}")

    if choice == "6":
        num_chips = get_int("  Number of LED chips", 3, 1, 20)
        base_leds = [
            {"name": input(f"  Chip {i+1} name: ").strip() or f"Ch{i+1}",
             "x": 0.333, "y": 0.333, "flux": 50.0}
            for i in range(num_chips)
        ]
    else:
        base_leds = [dict(l) for l in preset["leds"]]

    leds = _edit_chips(base_leds)

    print("-- Intensity Channel ---------------------------------")
    print("  Some fixtures (e.g. ETC Fos/4) expect a master intensity")
    print("  byte as their first output DMX channel.\n")
    has_int = get_yn("  Does this fixture have an intensity channel?",
                     default=preset["has_intensity_ch"])

    print("\n-- Output Bit Depth ----------------------------------")
    print("  8-bit  : 1 DMX channel per chip (0-255)")
    print("  16-bit : 2 DMX channels per chip (MSB then LSB)\n")
    while True:
        raw = input(f"  Bit depth [8 or 16] [{preset['output_bit_depth']}]: ").strip()
        if raw == "": bit_depth = preset["output_bit_depth"]; break
        if raw in ("8","16"): bit_depth = int(raw); break
        print("  ! Enter 8 or 16")

    print("\n-- Control Channels ----------------------------------")
    print("  Trailing channels held at 0 (strobe, curve, fan, etc.).\n")
    if preset["control_channels"]:
        print(f"  Preset default: {', '.join(preset['control_channels'])}\n")
    ctrl_count = get_int("  Number of control channels",
                         len(preset["control_channels"]), 0, 20)
    ctrl_names = []
    if ctrl_count > 0 and get_yn("  Name the control channels?", True):
        defaults = preset["control_channels"] + [f"Ctrl{i+1}" for i in range(ctrl_count)]
        for i in range(ctrl_count):
            n = input(f"  Control ch {i+1} name [{defaults[i]}]: ").strip()
            ctrl_names.append(n if n else defaults[i])
    else:
        ctrl_names = [f"Ctrl{i+1}" for i in range(ctrl_count)]

    return {"leds": leds, "has_intensity_ch": has_int,
            "output_bit_depth": bit_depth, "control_channels": ctrl_names}


# ---------------------------------------------------------------------------
# Setup wizard
# ---------------------------------------------------------------------------

def fixture_footprint(cfg):
    """Total DMX channels consumed per fixture on the output universe."""
    bpc  = 2 if cfg.get("output_bit_depth", 8) == 16 else 1
    ctrl = len(cfg.get("control_channels", []))
    int_ = bpc if cfg.get("has_intensity_ch", False) else 0
    led  = len(cfg["leds"]) * bpc
    return int_ + led + ctrl


def setup_wizard():
    print("\n+----------------------------------------------------+")
    print("|   sACN Color Space Converter -- First-Time Setup   |")
    print("+----------------------------------------------------+\n")
    cfg = {}

    if not HAS_PSUTIL:
        print("  TIP: pip install psutil  for a full interface list\n")

    print("-- Network Interfaces --------------------------------")
    print("  Pick the adapter on the same subnet as your console")
    print("  and LED driver. For loopback testing use 127.0.0.1\n")
    cfg["bind_ip"] = pick_interface("receive")
    diff = input("\n  Use same interface for output? [Y/n]: ").strip().lower()
    cfg["send_ip"] = pick_interface("send") if diff == "n" else cfg["bind_ip"]
    print(f"\n  RX bind : {cfg['bind_ip']}")
    print(f"  TX bind : {cfg['send_ip']}")

    # LED / fixture type
    fixture_cfg = pick_led_preset()
    cfg.update(fixture_cfg)

    fp = fixture_footprint(cfg)
    num_chips = len(cfg["leds"])

    # Universe / patch
    max_fix_in  = 512 // 6
    max_fix_out = 512 // fp if fp > 0 else 512
    max_fixtures = min(max_fix_in, max_fix_out)

    print("\n-- Universe & Fixture Settings -----------------------")
    cfg["input_universe"]    = get_int("  Input  universe  (from console)", 1, 1, 63999)
    cfg["output_universe"]   = get_int("  Output universe  (to LED driver)", 2, 1, 63999)
    cfg["num_fixtures"]      = get_int(f"  Number of fixtures (max {max_fixtures})", 1, 1, max_fixtures)

    bpc_label = f"{'16-bit (2ch)' if cfg['output_bit_depth'] == 16 else '8-bit (1ch)'}"
    int_label = "+ 1 intensity" if cfg["has_intensity_ch"] else ""
    ctrl_label = f"+ {len(cfg['control_channels'])} ctrl" if cfg["control_channels"] else ""
    print(f"\n  Output footprint per fixture: {fp} ch total")
    print(f"  ({bpc_label} x {num_chips} chips {int_label} {ctrl_label})")
    cfg["input_start_addr"]  = get_int("  Input  start address (1-based)", 1, 1, 512)
    cfg["output_start_addr"] = get_int("  Output start address (1-based)", 1, 1, 512)

    last_in  = cfg["input_start_addr"]  + cfg["num_fixtures"] * 6  - 1
    last_out = cfg["output_start_addr"] + cfg["num_fixtures"] * fp - 1
    if last_in  > 512: print(f"  WARNING: input  needs up to ch {last_in}  (> 512)")
    if last_out > 512: print(f"  WARNING: output needs up to ch {last_out} (> 512)")

    print("\n-- Output Destination --------------------------------")
    raw = input("  Destination IP  (blank = multicast): ").strip()
    cfg["output_ip"] = raw if raw else None

    print("\n-- CIE XY Encoding -----------------------------------")
    print("    1.0 = ETC EOS / grandMA / Chamsys (most common)")
    cfg["xy_scale"] = get_float("  XY full-scale value", 1.0, 0.05, 1.0)

    print("\n-- Gamma ---------------------------------------------")
    print("  1.0 = linear (recommended)   2.2 = perceptual")
    cfg["output_gamma"] = get_float("  Output gamma", 1.0, 0.5, 3.0)

    with open(CONFIG_FILE, "w") as fh:
        json.dump(cfg, fh, indent=2)
    print(f"\n  Config saved -> {CONFIG_FILE}\n")
    return cfg


def load_or_create_config():
    if os.path.exists(CONFIG_FILE):
        print(f"\nExisting config found: {CONFIG_FILE}")
        if input("Use it? [Y/n]: ").strip().lower() != "n":
            with open(CONFIG_FILE) as fh:
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
    return setup_wizard()


# ---------------------------------------------------------------------------
# Colour math
# ---------------------------------------------------------------------------

def build_led_matrix(leds):
    M = np.zeros((3, len(leds)))
    for i, led in enumerate(leds):
        x, y, flux = led["x"], led["y"], float(led["flux"])
        Y = flux
        X = (Y / y) * x
        Z = (Y / y) * (1.0 - x - y)
        M[:, i] = [X, Y, Z]
    peak_y = M[1, :].max()
    if peak_y > 0:
        M /= peak_y
    return M


def xy_intensity_to_XYZ(x, y, intensity):
    if y < 1e-7 or intensity < 1e-7:
        return np.zeros(3)
    Y = float(intensity)
    return np.array([(Y / y) * x, Y, (Y / y) * (1.0 - x - y)])


def solve_led_weights(target_XYZ, M, has_intensity_ch=False):
    """
    Solve for per-chip weights given target XYZ.

    has_intensity_ch=False (RGB strips, no master dimmer channel)
    -------------------------------------------------------------
    Bounded lsq: w ∈ [0,1], M @ w ≈ target_XYZ.
    Chips reproduce both colour and brightness.

    has_intensity_ch=True (ETC Fos/4 Direct, fixtures with master dimmer)
    ----------------------------------------------------------------------
    NNLS + normalise: find non-negative w (no upper bound), then scale so
    max(w) = 1.0.  This solves for colour DIRECTION only; the separate Int
    channel carries the brightness.

    Why: On wide-gamut fixtures the peak chip (lime, 2260 fc) is ~13× the
    dimmest chip (deep red, 169 fc).  Requesting pure deep red at full
    intensity asks for Y that deep red alone can't supply, so the bounded
    solver adds lime/amber/green to compensate → wrong colour.  The
    off-spectrum all-white bug has the same cause: extreme xy values give
    enormous X or Z targets that no chip can match, so everything goes to
    255.  NNLS+normalise avoids both by only matching colour direction.
    """
    if target_XYZ.max() < 1e-9:
        return np.zeros(M.shape[1])
    try:
        if has_intensity_ch:
            result = lsq_linear(M, target_XYZ, bounds=(0.0, np.inf))
            w = np.clip(result.x, 0.0, None)
            peak = w.max()
            if peak < 1e-9:
                return np.zeros(M.shape[1])
            return w / peak
        else:
            return np.clip(lsq_linear(M, target_XYZ, bounds=(0.0, 1.0)).x, 0.0, 1.0)
    except Exception as e:
        print(f"\n  SOLVER ERROR: {e}", flush=True)
        return np.zeros(M.shape[1])


def unpack16(high, low):
    return ((int(high) << 8) | int(low)) / 65535.0


def to_dmx8(value, gamma=1.0):
    v = float(np.clip(value, 0.0, 1.0))
    if gamma != 1.0:
        v = v ** (1.0 / gamma)
    return int(round(v * 255))


def write_channel(buf, pos, value_0_1, bit_depth, gamma=1.0):
    """
    Write one controlled channel (intensity or LED chip) into buf at pos.
    8-bit  : writes 1 byte,  returns pos + 1
    16-bit : writes MSB then LSB, returns pos + 2
    """
    v = float(np.clip(value_0_1, 0.0, 1.0))
    if gamma != 1.0:
        v = v ** (1.0 / gamma)
    if bit_depth == 16:
        val = int(round(v * 65535))
        if pos + 1 < 512:
            buf[pos]     = (val >> 8) & 0xFF
            buf[pos + 1] =  val       & 0xFF
        return pos + 2
    else:
        if pos < 512:
            buf[pos] = int(round(v * 255))
        return pos + 1


# ---------------------------------------------------------------------------
# Per-fixture snapshot
# ---------------------------------------------------------------------------

class FixtureSnapshot:
    def __init__(self, in_ch, out_ch, num_chips, has_intensity_ch, bit_depth):
        self.in_start_ch    = in_ch
        self.out_start_ch   = out_ch
        self.has_intensity  = has_intensity_ch
        self.bit_depth      = bit_depth
        self.raw_dim_hi = self.raw_dim_lo = 0
        self.raw_x_hi   = self.raw_x_lo   = 0
        self.raw_y_hi   = self.raw_y_lo   = 0
        self.dim  = 0.0
        self.cx   = 0.0
        self.cy   = 0.0
        # Display values: stored as 0-255 for 8-bit, 0-65535 for 16-bit
        self.out_intensity = 0
        self.out_vals      = [0] * num_chips


# ---------------------------------------------------------------------------
# Core converter
# ---------------------------------------------------------------------------

class ColorConverter:
    def __init__(self, cfg):
        self.leds          = cfg["leds"]
        self.num_chips     = len(self.leds)
        self.num_fixtures  = cfg["num_fixtures"]
        self.xy_scale      = cfg.get("xy_scale", 1.0)
        self.gamma         = cfg.get("output_gamma", 1.0)
        self.in_start      = cfg.get("input_start_addr",  1)
        self.out_start     = cfg.get("output_start_addr", 1)
        self.has_int_ch    = cfg.get("has_intensity_ch",  False)
        self.bit_depth     = cfg.get("output_bit_depth",  8)
        self.ctrl_chs      = cfg.get("control_channels",  [])
        self.num_ctrl      = len(self.ctrl_chs)
        self.footprint     = fixture_footprint(cfg)  # total output ch per fixture
        self.M             = build_led_matrix(self.leds)
        self._out_buf      = [0] * 512
        self._snapshots    = [
            FixtureSnapshot(
                in_ch          = self.in_start  + i * 6,
                out_ch         = self.out_start + i * self.footprint,
                num_chips      = self.num_chips,
                has_intensity_ch = self.has_int_ch,
                bit_depth      = self.bit_depth,
            ) for i in range(self.num_fixtures)
        ]
        self._lock         = threading.Lock()
        self.rx_count      = 0
        self.last_rx_time  = None

        print("\n-- LED Gamut Matrix (normalised XYZ primaries) ------")
        names = [l["name"] for l in self.leds]
        col_w = max(len(n) for n in names) + 2
        print("       " + "".join(f"{n:>{col_w}}" for n in names))
        for label, row in zip(["  X", "  Y", "  Z"], self.M):
            print(label + "".join(f"{v:>{col_w}.4f}" for v in row))
        print()

    def process(self, dmx_data):
        self.last_rx_time = time.monotonic()
        self.rx_count    += 1
        try:
            new_out = list(self._out_buf)

            for i in range(self.num_fixtures):
                snap     = self._snapshots[i]
                in_base  = (self.in_start  - 1) + i * 6
                out_base = (self.out_start - 1) + i * self.footprint

                if in_base + 6 > len(dmx_data):
                    # Out of range -- zero the controlled portion, skip ctrl
                    pos = out_base
                    if self.has_int_ch:
                        for _ in range(2 if self.bit_depth == 16 else 1):
                            if pos < 512: new_out[pos] = 0
                            pos += 1
                    for _ in range(self.num_chips * (2 if self.bit_depth == 16 else 1)):
                        if pos < 512: new_out[pos] = 0
                        pos += 1
                    snap.out_intensity = 0
                    snap.out_vals = [0] * self.num_chips
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

                target_XYZ = xy_intensity_to_XYZ(x, y, intensity)
                weights    = solve_led_weights(target_XYZ, self.M,
                                               has_intensity_ch=self.has_int_ch)

                # Write output channels sequentially
                pos = out_base

                # 1. Intensity channel (no gamma -- it's a raw dimmer passthrough)
                if self.has_int_ch:
                    scale = 65535 if self.bit_depth == 16 else 255
                    snap.out_intensity = int(round(intensity * scale))
                    pos = write_channel(new_out, pos, intensity, self.bit_depth, gamma=1.0)

                # 2. LED chip channels (with gamma)
                chip_vals = []
                for w in weights:
                    scale = 65535 if self.bit_depth == 16 else 255
                    chip_vals.append(int(round(float(np.clip(w, 0, 1)) ** (1.0 / self.gamma if self.gamma != 1.0 else 1.0) * scale)))
                    pos = write_channel(new_out, pos, float(w), self.bit_depth, self.gamma)
                snap.out_vals = chip_vals

                # 3. Control channels -- advance pos but do NOT write
                # (leave whatever is in new_out, which starts as 0)
                # pos += self.num_ctrl  (not needed, we're done with this fixture)

                if DEBUG:
                    print(
                        f"  [#{self.rx_count:>5} fix{i+1}]"
                        f" inCh={snap.in_start_ch}"
                        f" raw=[{dhi},{dlo},{xhi},{xlo},{yhi},{ylo}]"
                        f" dim={intensity:.3f} x={x:.4f} y={y:.4f}"
                        f" XYZ={np.round(target_XYZ,4)}"
                        f" w={[round(float(w),3) for w in weights]}"
                        f" out={chip_vals} outCh={snap.out_start_ch}",
                        flush=True,
                    )

            with self._lock:
                self._out_buf = new_out

        except Exception:
            print(f"\n  EXCEPTION in process() packet #{self.rx_count}:", flush=True)
            traceback.print_exc()

    def get_frame(self):
        with self._lock:
            return list(self._out_buf), list(self._snapshots)


# ---------------------------------------------------------------------------
# Raw sACN receiver
# ---------------------------------------------------------------------------

def build_rx_socket(bind_ip, universe):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    if hasattr(socket, "SO_REUSEPORT"):
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        except OSError:
            pass
    sock.bind(("", SACN_PORT))
    sock.settimeout(1.0)
    if not bind_ip.startswith("127."):
        mcast_addr = universe_to_multicast(universe)
        mreq = socket.inet_aton(mcast_addr) + socket.inet_aton(bind_ip)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
        print(f"  RX joined multicast {mcast_addr} on {bind_ip}")
    else:
        print(f"  RX bound to loopback {bind_ip}:{SACN_PORT}")
    return sock


def receiver_loop(bind_ip, universe, converter, stop_event):
    sock = None
    try:
        sock = build_rx_socket(bind_ip, universe)
        print(f"  RX listening for universe {universe}...", flush=True)
        while not stop_event.is_set():
            try:
                data, _ = sock.recvfrom(638)
                dmx, pkt_uni = parse_sacn_packet(data)
                if dmx is None or pkt_uni != universe:
                    continue
                converter.process(dmx)
            except socket.timeout:
                continue
            except Exception as e:
                if not stop_event.is_set():
                    print(f"\n  RX ERROR: {e}", flush=True)
    except Exception as e:
        print(f"\n  FATAL RX SETUP ERROR: {e}", flush=True)
        traceback.print_exc()
    finally:
        if sock:
            sock.close()


# ---------------------------------------------------------------------------
# 44 Hz sender thread
# ---------------------------------------------------------------------------

def sender_loop(sender, universe, converter, fps=44.0):
    interval  = 1.0 / fps
    next_tick = time.monotonic()
    while True:
        buf, _ = converter.get_frame()
        try:
            sender[universe].dmx_data = tuple(buf)
        except Exception as e:
            print(f"\n  SENDER ERROR: {e}", flush=True)
        next_tick += interval
        sleep_for  = next_tick - time.monotonic()
        if sleep_for > 0:
            time.sleep(sleep_for)
        else:
            next_tick = time.monotonic()


# ---------------------------------------------------------------------------
# Status display
# ---------------------------------------------------------------------------

def status_loop(converter, num_fixtures, interval=2.0):
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
    # Output column width: wide enough for chip name or "Int", min 5 for 16-bit
    all_out_names = (["Int"] if has_int else []) + led_names
    W_OUT = max(5 if bit_depth == 16 else 4, max(len(n) for n in all_out_names) + 1)

    hdr_in  = (f"{'Ch':>{W_CH}} {'Dim16':>{W_RAW}} {'X16':>{W_RAW}} {'Y16':>{W_RAW}}"
               f"  {'Dim':>{W_FLOAT}} {'CIEx':>{W_FLOAT}} {'CIEy':>{W_FLOAT}}")

    out_col_names = []
    if has_int:
        out_col_names.append("Int")
    out_col_names.extend(led_names)
    ctrl_suffix = f"  +{len(ctrl_names)} ctrl ({','.join(ctrl_names)})" if ctrl_names else ""
    hdr_out = f"{'Ch':>{W_CH}}" + "".join(f" {n[:W_OUT]:>{W_OUT}}" for n in out_col_names) + ctrl_suffix

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
            f"  RX: {rx_hz:.1f} Hz   total packets: {converter.rx_count}"
            + (f"   [{bit_depth}-bit output  max={max_val}]" ),
            f"  {'Fix':>{W_FIX}}  {'--- INPUT ---':^{len(hdr_in)}}    {'--- OUTPUT ---'}",
            f"  {'':>{W_FIX}}  {hdr_in}    {hdr_out}",
            "  " + divider,
        ]
        for i, s in enumerate(snaps[:num_fixtures]):
            dim16 = (s.raw_dim_hi << 8) | s.raw_dim_lo
            x16   = (s.raw_x_hi   << 8) | s.raw_x_lo
            y16   = (s.raw_y_hi   << 8) | s.raw_y_lo
            in_p  = (f"{s.in_start_ch:>{W_CH}} {dim16:>{W_RAW}} {x16:>{W_RAW}} {y16:>{W_RAW}}"
                     f"  {s.dim:>{W_FLOAT}.3f} {s.cx:>{W_FLOAT}.4f} {s.cy:>{W_FLOAT}.4f}")
            out_vals_display = []
            if has_int:
                out_vals_display.append(s.out_intensity)
            out_vals_display.extend(s.out_vals)
            out_p = f"{s.out_start_ch:>{W_CH}}" + "".join(f" {v:>{W_OUT}}" for v in out_vals_display)
            lines.append(f"  {i+1:>{W_FIX}}  {in_p}    {out_p}")
        print("\n".join(lines), flush=True)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Terminal re-launch  (handles double-click / file-manager launch)
# ---------------------------------------------------------------------------
#
# Windows detection:
#   Double-clicking a .py file creates a real console, so isatty()=True
#   and that check is useless.  Instead we inspect the parent process:
#     explorer.exe          → double-clicked → relaunch in new cmd window
#     cmd.exe/powershell/.. → real terminal  → run normally
#
# macOS: isatty() works; write a .command file and open it.
# Linux: isatty() works; try common terminal emulators.
#
# Failures logged to launcher.log in the script's directory.
#
# Flags:
#   --in_terminal  sentinel on the re-launched copy — prevents looping
#   --no-pause     opt-out for process supervisors / automation

_IN_TERMINAL = "--in_terminal"
_LAUNCH_LOG  = None

_WIN_TERMINAL_PARENTS = {
    "cmd.exe", "powershell.exe", "pwsh.exe",
    "windowsterminal.exe", "wt.exe",
    "bash.exe", "zsh.exe", "sh.exe",
    "mintty.exe", "alacritty.exe", "kitty.exe",
    "conemu.exe", "cmder.exe", "hyper.exe",
}


def _launcher_log(msg):
    global _LAUNCH_LOG
    try:
        if _LAUNCH_LOG is None:
            _LAUNCH_LOG = os.path.join(
                os.path.dirname(os.path.abspath(sys.argv[0])), "launcher.log")
        import datetime
        with open(_LAUNCH_LOG, "a") as f:
            f.write(f"[{datetime.datetime.now():%Y-%m-%d %H:%M:%S}] {msg}\n")
    except Exception:
        pass


def _from_explorer_windows():
    """True if our parent process is explorer.exe (double-clicked)."""
    try:
        import psutil
        proc   = psutil.Process()
        parent = proc.parent()
        if parent is None:
            return False
        pname = parent.name().lower()
        _launcher_log(f"parent={pname} pid={parent.pid}")
        if pname in _WIN_TERMINAL_PARENTS:
            return False
        if pname == "explorer.exe":
            return True
        # py.exe launcher sits between explorer and python
        gp = parent.parent()
        if gp:
            gpname = gp.name().lower()
            _launcher_log(f"grandparent={gpname} pid={gp.pid}")
            if gpname == "explorer.exe":
                return True
            if gpname in _WIN_TERMINAL_PARENTS:
                return False
        return False
    except ImportError:
        try: return not sys.stdout.isatty()
        except Exception: return False
    except Exception as e:
        _launcher_log(f"_from_explorer_windows error: {e}")
        return False


def _needs_relaunch():
    if _IN_TERMINAL in sys.argv or "--no-pause" in sys.argv:
        return False
    if sys.platform == "win32":
        return _from_explorer_windows()
    try:
        return not sys.stdout.isatty()
    except Exception:
        return False


def _relaunch_in_terminal():
    """Re-launch inside a visible terminal. Returns True if dispatched."""
    import shutil, subprocess, tempfile, traceback as _tb
    script   = os.path.abspath(sys.argv[0])
    work_dir = os.path.dirname(script)
    extra    = [a for a in sys.argv[1:] if a != _IN_TERMINAL] + [_IN_TERMINAL]
    python   = sys.executable
    _launcher_log(f"relaunch: platform={sys.platform} script={script} extra={extra}")

    if sys.platform == "win32":
        try:
            CREATE_NEW_CONSOLE = 0x00000010
            cmd = ["cmd", "/k", python, script] + extra
            _launcher_log(f"cmd: {cmd}  cwd={work_dir}")
            subprocess.Popen(cmd, creationflags=CREATE_NEW_CONSOLE, cwd=work_dir)
            return True
        except Exception as e:
            _launcher_log(f"Windows relaunch failed: {e}\n{_tb.format_exc()}")
            return False

    elif sys.platform == "darwin":
        import shlex
        cmd_line = " ".join(shlex.quote(a) for a in [python, script] + extra)
        content  = f"#!/bin/bash\ncd {shlex.quote(work_dir)}\n{cmd_line}\n"
        try:
            fd, path = tempfile.mkstemp(suffix=".command", prefix="sacn_")
            with os.fdopen(fd, "w") as f:
                f.write(content)
            os.chmod(path, 0o755)
            r = subprocess.run(["open", path], capture_output=True, text=True)
            if r.returncode != 0:
                _launcher_log(f"open failed: {r.stderr}")
                return False
            return True
        except Exception as e:
            _launcher_log(f"macOS relaunch failed: {e}\n{_tb.format_exc()}")
            return False

    else:
        candidates = [
            ("gnome-terminal", ["--working-directory", work_dir, "--"]),
            ("xterm",          ["-e"]),
            ("konsole",        ["--workdir", work_dir, "-e"]),
            ("xfce4-terminal", ["--working-directory", work_dir, "-e"]),
            ("lxterminal",     ["-e"]),
            ("mate-terminal",  ["--working-directory", work_dir, "-e"]),
            ("tilix",          ["-e"]),
            ("alacritty",      ["-e"]),
            ("kitty",          []),
        ]
        for term, flags in candidates:
            if not shutil.which(term):
                continue
            try:
                subprocess.Popen([term] + flags + [python, script] + extra,
                                 cwd=work_dir)
                _launcher_log(f"Linux: launched with {term}")
                return True
            except Exception as e:
                _launcher_log(f"Linux: {term} failed: {e}")
        _launcher_log("Linux: no terminal emulator found")
        return False


def _pause_before_exit(message=""):
    """Fallback: wait for Enter when relaunch failed."""
    if _IN_TERMINAL in sys.argv or "--no-pause" in sys.argv:
        return
    if sys.platform == "win32":
        if not _from_explorer_windows():
            return
    else:
        try:
            if sys.stdout.isatty():
                return
        except Exception:
            pass
    if message:
        print(f"\n{message}")
    print("\n  Press Enter to close this window...", end="", flush=True)
    try:
        input()
    except (EOFError, KeyboardInterrupt):
        pass


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    # Re-launch inside a real terminal if opened by double-click
    if _needs_relaunch():
        if _relaunch_in_terminal():
            sys.exit(0)
        # relaunch failed — continue with pause-on-exit fallback

    # --- Config / wizard ---
    try:
        cfg = load_or_create_config()
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

    in_last  = in_start  + n_fix * 6  - 1
    out_last = out_start + n_fix * fp - 1
    fp_parts = []
    if has_int:  fp_parts.append(f"Int({bpc}ch)")
    fp_parts.append(f"{len(led_names)} chips x {bpc}ch")
    if ctrl_chs: fp_parts.append(f"{len(ctrl_chs)} ctrl")
    fp_desc   = " + ".join(fp_parts) + f" = {fp} ch/fixture"
    chips_str = ", ".join(led_names)

    print(f"\n+--------------------------------------------------------+")
    print(f"|  Running                                               |")
    print(f"|  RX bind     : {bind_ip:<40}|")
    print(f"|  TX bind     : {send_ip:<40}|")
    print(f"|  Input  uni {in_uni:<5}: ch {in_start}-{in_last:<37}|")
    print(f"|  Output uni {out_uni:<5}: ch {out_start}-{out_last:<37}|")
    print(f"|  Fixtures    : {n_fix:<5}  {fp_desc:<38}|")
    print(f"|  LED chips   : {chips_str[:42]:<42}|")
    if len(chips_str) > 42:
        print(f"|               {chips_str[42:84][:42]:<42}|")
    if ctrl_chs:
        print(f"|  Ctrl chs    : {', '.join(ctrl_chs)[:42]:<42}|")
    print(f"|  Bit depth   : {bit_depth:<40}|")
    print(f"|  XY scale    : {cfg.get('xy_scale',1.0):<40}|")
    print(f"|  Gamma       : {cfg.get('output_gamma',1.0):<40}|")
    print(f"|  Output dest : {net_dest:<40}|")
    if DEBUG:
        print(f"|  DEBUG ON  (every frame printed to console)            |")
    print(f"|  {'Ctrl+C to stop':<40}|")
    print(f"+--------------------------------------------------------+\n")

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

    threading.Thread(
        target=status_loop,
        args=(converter, n_fix),
        daemon=True,
    ).start()

    try:
        while True:
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\n\nShutting down...")
    except Exception:
        print("\n\nUNEXPECTED ERROR in main loop:")
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
