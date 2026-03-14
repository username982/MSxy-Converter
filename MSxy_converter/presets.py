# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Mark Stuen
"""
presets.py
----------
Built-in fixture / LED chip presets.

Each entry defines:
  label             -- human-readable name shown in the setup wizard
  has_intensity_ch  -- True if the fixture's first output channel is a
                       master intensity byte (e.g. ETC Fos/4 Direct mode)
  output_bit_depth  -- 8 or 16; applies to intensity + all LED chip channels
  control_channels  -- trailing channel names that are part of the fixture
                       footprint but are never written (strobe, curve, fan…)
  leds              -- list of emitter dicts: name, CIE x, CIE y, flux

CIE xy values for ETC Fos/4 (Lumileds LUXEON C, DS144 datasheet, 350mA Tj=85°C):
  Deep Red  derived from 660 nm peak wavelength
  Red       derived from 629 nm dominant wavelength
  Amber     PC Amber bin-20 centroid  (datasheet Table 7)
  Lime      Lime   bin-10 centroid    (datasheet Table 7)
  Green     derived from 525 nm dominant wavelength
  Cyan      derived from 498 nm dominant wavelength
  Blue      derived from 472 nm dominant wavelength
  Indigo    derived from 450 nm peak  wavelength
  Flux values reflect typical lm output at 350 mA (radiometric-to-lm
  estimates used for Deep Red and Indigo whose photopic response is near zero).
"""

PRESETS = {
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
        "label":            "ETC Fos/4 Panel  -- Direct mode  (12 ch/fixture)",
        "has_intensity_ch": True,
        "output_bit_depth": 8,
        "control_channels": ["Strobe", "Curve", "Fan"],
        "leds": [
            # Output order: Int | DR | R | A | Lm | G | Cy | B | I | Strobe | Curve | Fan
            # CIE xy and fc values measured on physical unit with spectrometer.
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
