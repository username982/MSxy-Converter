# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Mark Stuen
"""
gdtf_loader.py
--------------
Parse GDTF (General Device Type Format) fixture profile files.

GDTF files are ZIP archives containing a device_description.xml manifest.
This module extracts everything the converter needs:

  - Fixture name and manufacturer
  - Available DMX modes (name + channel count)
  - Per-mode channel list: name, attribute, colour emitter link, offset
  - Emitter chromaticity (CIE 1931 xy) and luminous flux

GDTF spec: https://gdtf-share.com/wiki/GDTF_File_Format_Description

Attribute naming convention (GDTF §Attribute):
  Dimmer           -- master intensity
  ColorAdd_R       -- additive red
  ColorAdd_G       -- additive green
  ColorAdd_B       -- additive blue
  ColorAdd_RY      -- red-yellow / amber
  ColorAdd_GY      -- green-yellow / lime
  ColorAdd_CY      -- cyan
  ColorAdd_UV      -- UV / indigo / violet
  Shutter, Strobe  -- control
  Pan, Tilt        -- movement control
  ...etc

Color channels are identified by Attribute starting with "ColorAdd_",
"ColorSub_", "Color", or by a direct emitter link in the channel function.
"""

import zipfile
import xml.etree.ElementTree as ET
import os
import re
from typing import Optional


# ---------------------------------------------------------------------------
# Attributes that map to controllable colour emitters
# ---------------------------------------------------------------------------

COLOR_ATTRIBUTE_PREFIXES = (
    "ColorAdd_",
    "ColorSub_",
    "ColorMix",
)

# Well-known single-word colour attributes
COLOR_ATTRIBUTE_NAMES = {
    "Red", "Green", "Blue", "White", "Amber", "UV",
    "Lime", "Cyan", "Indigo", "Magenta", "Yellow",
    "WW", "CW", "WarmWhite", "CoolWhite",
}

# Attributes that are definitely NOT colour emitters
CONTROL_ATTRIBUTE_PREFIXES = (
    "Shutter", "Strobe", "Pan", "Tilt", "Zoom", "Focus",
    "Iris", "Prism", "Frost", "Gobo", "Blade", "Effects",
    "Control", "Reset", "Fan", "Macro", "Speed", "CTC",
    "CTO", "CTB", "Function", "Mode", "Reserved",
)

# Human-readable short names for common GDTF colour attributes
ATTRIBUTE_DISPLAY_NAMES = {
    "ColorAdd_R":   "Red",
    "ColorAdd_G":   "Green",
    "ColorAdd_B":   "Blue",
    "ColorAdd_W":   "White",
    "ColorAdd_WW":  "Warm White",
    "ColorAdd_CW":  "Cool White",
    "ColorAdd_RY":  "Amber",
    "ColorAdd_GY":  "Lime",
    "ColorAdd_CY":  "Cyan",
    "ColorAdd_UV":  "Indigo/UV",
    "ColorAdd_M":   "Magenta",
    "ColorAdd_Y":   "Yellow",
    "ColorSub_R":   "Red (sub)",
    "ColorSub_G":   "Green (sub)",
    "ColorSub_B":   "Blue (sub)",
    "ColorSub_C":   "Cyan (sub)",
    "ColorSub_M":   "Magenta (sub)",
    "ColorSub_Y":   "Yellow (sub)",
    "Dimmer":       "Intensity",
}

# Fallback CIE xy for attributes without an emitter definition in the GDTF
ATTRIBUTE_FALLBACK_XY = {
    "ColorAdd_R":  (0.690, 0.308),
    "ColorAdd_G":  (0.170, 0.700),
    "ColorAdd_B":  (0.135, 0.053),
    "ColorAdd_W":  (0.313, 0.329),
    "ColorAdd_WW": (0.461, 0.413),
    "ColorAdd_CW": (0.320, 0.336),
    "ColorAdd_RY": (0.560, 0.430),
    "ColorAdd_GY": (0.408, 0.537),
    "ColorAdd_CY": (0.040, 0.450),
    "ColorAdd_UV": (0.157, 0.018),
    "ColorAdd_M":  (0.320, 0.154),
    "ColorAdd_Y":  (0.419, 0.505),
}


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

class GDTFChannel:
    """One DMX channel parsed from a GDTF mode."""
    def __init__(self, offset: int, attribute: str, display_name: str,
                 is_colour: bool, is_intensity: bool, is_16bit: bool,
                 emitter_name: Optional[str], x: Optional[float],
                 y: Optional[float], flux: Optional[float]):
        self.offset       = offset        # 1-based DMX offset within mode
        self.attribute    = attribute     # raw GDTF attribute string
        self.display_name = display_name  # friendly name for UI
        self.is_colour    = is_colour
        self.is_intensity = is_intensity
        self.is_16bit     = is_16bit      # True if this is an MSB channel (fine follows)
        self.emitter_name = emitter_name
        self.x            = x            # CIE 1931 x (None if unknown)
        self.y            = y            # CIE 1931 y (None if unknown)
        self.flux         = flux         # relative luminous flux (None if unknown)

    def __repr__(self):
        return (f"GDTFChannel(offset={self.offset}, attr={self.attribute!r}, "
                f"name={self.display_name!r}, colour={self.is_colour}, "
                f"16bit={self.is_16bit}, x={self.x}, y={self.y}, flux={self.flux})")


class GDTFMode:
    """One DMX mode from a GDTF file."""
    def __init__(self, name: str, channels: list):
        self.name     = name
        self.channels = channels   # list of GDTFChannel, ordered by offset

    @property
    def channel_count(self):
        return len(self.channels)

    @property
    def colour_channels(self):
        return [c for c in self.channels if c.is_colour and not c.is_intensity]

    @property
    def intensity_channels(self):
        return [c for c in self.channels if c.is_intensity]

    @property
    def control_channels(self):
        return [c for c in self.channels if not c.is_colour and not c.is_intensity]


class GDTFFixture:
    """Parsed GDTF fixture profile."""
    def __init__(self, filename: str, manufacturer: str, name: str,
                 modes: list, emitters: dict):
        self.filename     = filename
        self.manufacturer = manufacturer
        self.name         = name
        self.modes        = modes      # list of GDTFMode
        self.emitters     = emitters   # name -> {x, y, flux}

    def __repr__(self):
        return f"GDTFFixture({self.manufacturer} {self.name}, {len(self.modes)} modes)"


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------

def _parse_color(color_str: str):
    """
    Parse GDTF Color attribute string  "x y Y"  →  (x, y, Y).
    Returns None on failure.
    """
    if not color_str:
        return None
    try:
        parts = color_str.strip().split()
        if len(parts) >= 3:
            return float(parts[0]), float(parts[1]), float(parts[2])
        if len(parts) == 2:
            return float(parts[0]), float(parts[1]), 1.0
    except (ValueError, IndexError):
        pass
    return None


def _is_colour_attribute(attr: str) -> bool:
    for prefix in COLOR_ATTRIBUTE_PREFIXES:
        if attr.startswith(prefix):
            return True
    return attr in COLOR_ATTRIBUTE_NAMES


def _is_control_attribute(attr: str) -> bool:
    for prefix in CONTROL_ATTRIBUTE_PREFIXES:
        if attr.startswith(prefix):
            return True
    return False


def _display_name_for(attr: str, emitter_name: Optional[str]) -> str:
    """Best human-readable name: emitter name > lookup table > clean attribute."""
    if emitter_name:
        return emitter_name
    if attr in ATTRIBUTE_DISPLAY_NAMES:
        return ATTRIBUTE_DISPLAY_NAMES[attr]
    # Strip "ColorAdd_" prefix and title-case
    for prefix in ("ColorAdd_", "ColorSub_", "ColorMix_"):
        if attr.startswith(prefix):
            return attr[len(prefix):].replace("_", " ").title()
    return attr.replace("_", " ").title()


def parse_gdtf(filepath: str) -> GDTFFixture:
    """
    Parse a GDTF file and return a GDTFFixture.

    Raises FileNotFoundError, zipfile.BadZipFile, or ET.ParseError on failure.
    """
    with zipfile.ZipFile(filepath, "r") as zf:
        names = zf.namelist()
        xml_name = next(
            (n for n in names if n.lower() == "device_description.xml"),
            None
        )
        if xml_name is None:
            # Some GDTFs nest it in a subfolder
            xml_name = next(
                (n for n in names if n.lower().endswith("device_description.xml")),
                None
            )
        if xml_name is None:
            raise ValueError(f"No device_description.xml found in {filepath}")
        xml_data = zf.read(xml_name)

    root = ET.fromstring(xml_data)
    ft   = root if root.tag == "FixtureType" else root.find(".//FixtureType")
    if ft is None:
        raise ValueError("No FixtureType element in device_description.xml")

    manufacturer = ft.get("Manufacturer", "Unknown")
    name         = ft.get("LongName") or ft.get("Name", "Unknown Fixture")

    # --- Parse emitters ---
    emitters = {}
    for em in ft.findall(".//Emitter"):
        em_name  = em.get("Name", "")
        color    = _parse_color(em.get("Color", ""))
        dom_wave = em.get("DominantWaveLength")
        if color:
            x, y, Y = color
            # Use DiodePart luminous power if available, else Y from Color
            lum = float(em.get("LuminousFlux", Y)) if em.get("LuminousFlux") else Y
            emitters[em_name] = {"x": x, "y": y, "flux": lum}

    # --- Parse DMX modes ---
    modes = []
    for mode_el in ft.findall(".//DMXMode"):
        mode_name = mode_el.get("Name", "Default")
        channels  = []

        # Collect all DMXChannel elements
        # GDTF channels: Offset is 1-based, can be "1" or "1,2" for 16-bit
        for ch_el in mode_el.findall(".//DMXChannel"):
            offset_str = ch_el.get("Offset", "")
            if not offset_str or offset_str == "None":
                continue

            # "1" = 8-bit, "1,2" = 16-bit (MSB at 1, LSB at 2)
            offsets  = [int(o) for o in offset_str.split(",") if o.strip().isdigit()]
            if not offsets:
                continue
            offset   = offsets[0]
            is_16bit = len(offsets) >= 2

            # Find attribute via LogicalChannel
            attr        = ""
            emitter_ref = None
            for lc in ch_el.findall("LogicalChannel"):
                attr = lc.get("Attribute", "")
                # ChannelFunction may reference an emitter
                for cf in lc.findall("ChannelFunction"):
                    em_ref = cf.get("EmitterSpectrum") or cf.get("Emitter")
                    if em_ref and em_ref in emitters:
                        emitter_ref = em_ref
                break  # first LogicalChannel only

            if not attr:
                attr = ch_el.get("Attribute", "")

            is_intensity = attr == "Dimmer"
            is_colour    = is_intensity or _is_colour_attribute(attr)

            # Resolve xy + flux
            x = y = flux = None
            if emitter_ref and emitter_ref in emitters:
                x    = emitters[emitter_ref]["x"]
                y    = emitters[emitter_ref]["y"]
                flux = emitters[emitter_ref]["flux"]
            elif attr in ATTRIBUTE_FALLBACK_XY:
                x, y = ATTRIBUTE_FALLBACK_XY[attr]
                flux = 50.0  # unknown, placeholder

            disp = _display_name_for(attr, emitter_ref)

            channels.append(GDTFChannel(
                offset       = offset,
                attribute    = attr,
                display_name = disp,
                is_colour    = is_colour,
                is_intensity = is_intensity,
                is_16bit     = is_16bit,
                emitter_name = emitter_ref,
                x            = x,
                y            = y,
                flux         = flux,
            ))

        # Sort by DMX offset
        channels.sort(key=lambda c: c.offset)
        modes.append(GDTFMode(name=mode_name, channels=channels))

    filename = os.path.basename(filepath)
    return GDTFFixture(filename=filename, manufacturer=manufacturer,
                       name=name, modes=modes, emitters=emitters)


# ---------------------------------------------------------------------------
# Folder scanner
# ---------------------------------------------------------------------------

GDTF_FOLDER = "gdtf"


def find_gdtf_files(folder: str = GDTF_FOLDER) -> list:
    """
    Return a sorted list of .gdtf file paths found in folder.
    Returns [] if folder does not exist.
    """
    if not os.path.isdir(folder):
        return []
    return sorted(
        os.path.join(folder, f)
        for f in os.listdir(folder)
        if f.lower().endswith(".gdtf")
    )


# ---------------------------------------------------------------------------
# Convert GDTFMode → converter config fragment
# ---------------------------------------------------------------------------

def mode_to_fixture_cfg(fixture: GDTFFixture, mode: GDTFMode,
                        bit_depth: int = 8) -> dict:
    """
    Convert a parsed GDTFMode into the dict format expected by
    ColorConverter / setup wizard:

      {
        leds:              [ {name, x, y, flux}, ... ]   # colour chips only
        has_intensity_ch:  bool
        output_bit_depth:  int
        control_channels:  [ name, ... ]
        _gdtf_source:      "filename :: mode_name"       # for display only
      }

    Channels with missing xy are included with placeholder values and
    flagged so the wizard can prompt the user to fill them in.
    """
    has_int = len(mode.intensity_channels) > 0

    leds = []
    missing_xy = []
    for ch in mode.colour_channels:
        if ch.x is not None and ch.y is not None:
            leds.append({
                "name": ch.display_name,
                "x":    round(ch.x, 4),
                "y":    round(ch.y, 4),
                "flux": round(ch.flux, 1) if ch.flux else 50.0,
            })
        else:
            # Include with placeholder — wizard will prompt
            leds.append({
                "name":        ch.display_name,
                "x":           0.333,
                "y":           0.333,
                "flux":        50.0,
                "_needs_xy":   True,
            })
            missing_xy.append(ch.display_name)

    ctrl_names = [ch.display_name for ch in mode.control_channels]

    return {
        "leds":             leds,
        "has_intensity_ch": has_int,
        "output_bit_depth": bit_depth,
        "control_channels": ctrl_names,
        "_gdtf_source":     f"{fixture.filename} :: {mode.name}",
        "_missing_xy":      missing_xy,
    }
