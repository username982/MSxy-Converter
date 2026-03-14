# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Mark Stuen
"""
MSxy_converter
====================
sACN (E1.31) Intensity/XY → multi-chip LED colour space converter.

Receives 16-bit Dimmer + CIE X + CIE Y per fixture from a lighting console,
solves the optimal mix of LED emitter channels to reproduce the requested
colour, and retransmits the result as sACN to an LED driver or fixture.

Usage
-----
CLI (interactive wizard on first run)::

    MSxy-converter                     # uses led_config.json
    MSxy-converter fos4.json           # named config
    MSxy-converter fos4.json --debug   # per-frame solver output

GUI::

    MSxy-converter --gui
    MSxy-converter fos4.json --gui

Module::

    python -m MSxy_converter fos4.json
"""

__version__ = "1.0.0"
__author__  = "Mark Stuen"
__license__ = "MIT"
