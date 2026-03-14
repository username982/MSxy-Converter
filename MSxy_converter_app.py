# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Mark Stuen
"""
MSxy_converter_app.py
---------------------
Thin entry-point used by PyInstaller to build the standalone .exe / .app.

When frozen, this script:
  1. Sets the working directory to the folder containing the executable
     so that led_config.json is always found next to the app, not inside
     the PyInstaller temp-extraction directory.
  2. Launches the GUI directly — no CLI wizard, no terminal needed.

Users just double-click MSxy_converter.exe (Windows) or
MSxy_converter.app (macOS) and the GUI opens.
"""

import os
import sys


def _fix_working_directory():
    """
    When PyInstaller runs as a frozen single-file app it extracts to a
    temp folder (_MEIPASS).  The *executable* itself lives one level up.
    Set cwd to the executable's directory so config files are found there.
    """
    if getattr(sys, "frozen", False):
        exe_dir = os.path.dirname(sys.executable)
        os.chdir(exe_dir)


if __name__ == "__main__":
    _fix_working_directory()

    # Optional: allow passing a config file as the first argument
    config_file = "led_config.json"
    for arg in sys.argv[1:]:
        if not arg.startswith("--") and arg.endswith(".json"):
            config_file = arg
            break

    from MSxy_converter.gui import run_gui
    run_gui(config_file)
