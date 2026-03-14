# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Mark Stuen
"""
launcher.py
-----------
Cross-platform terminal re-launch for double-click / file-manager launches.

Detection strategy by platform
-------------------------------
Windows : sys.stdout.isatty() is UNRELIABLE — double-clicking a .py file
          gives you a real console (isatty=True) that just closes on exit.
          Instead we check the parent process name via psutil:
            explorer.exe  → launched by double-click  → relaunch
            cmd.exe / powershell.exe / wt.exe / etc. → real terminal → skip

macOS   : sys.stdout.isatty() works correctly.  We write a temp .command
          file and open it — macOS maps .command → Terminal.app natively
          so there are no shell-quoting issues.

Linux   : sys.stdout.isatty() works correctly.  Try common terminal
          emulators in order.

All failures are written to launcher.log beside the script so silent
failures can be diagnosed without a terminal.

Flags
-----
--in-terminal   Appended to re-launched copy's argv.  Prevents looping
                and suppresses the pause-on-exit prompt.
--no-pause      Opt-out for process supervisors / automation.
"""

import os
import sys
import shutil
import subprocess
import tempfile
import traceback

IN_TERMINAL_FLAG = "--in_terminal"   # underscore avoids argparse conflicts

# Known terminal / shell parent process names on Windows
_WINDOWS_TERMINAL_PARENTS = {
    "cmd.exe", "powershell.exe", "pwsh.exe",
    "windowsterminal.exe", "wt.exe",
    "bash.exe", "zsh.exe", "sh.exe",
    "mintty.exe", "alacritty.exe", "kitty.exe",
    "conemu.exe", "cmder.exe", "hyper.exe",
}

_LOG_PATH = None


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def _log(msg: str) -> None:
    global _LOG_PATH
    try:
        if _LOG_PATH is None:
            _LOG_PATH = os.path.join(_work_dir(), "launcher.log")
        import datetime
        with open(_LOG_PATH, "a") as f:
            f.write(f"[{datetime.datetime.now():%Y-%m-%d %H:%M:%S}] {msg}\n")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _work_dir() -> str:
    """Directory containing the script being run (not cwd)."""
    return os.path.dirname(os.path.abspath(sys.argv[0]))


def _script_path() -> str:
    return os.path.abspath(sys.argv[0])


def _extra_args() -> list:
    """User args with sentinel stripped (will be re-added)."""
    return [a for a in sys.argv[1:] if a != IN_TERMINAL_FLAG]


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------

def _launched_from_explorer_windows() -> bool:
    """
    Windows-only: return True if our parent process is explorer.exe
    (i.e. script was double-clicked, not run from a terminal).

    Walks up two levels to handle the py.exe launcher sitting between
    explorer.exe and python.exe.
    """
    try:
        import psutil
        proc   = psutil.Process()
        parent = proc.parent()
        if parent is None:
            return False
        pname = parent.name().lower()
        _log(f"parent process: {pname} (pid {parent.pid})")

        # Definitely a real terminal
        if pname in _WINDOWS_TERMINAL_PARENTS:
            return False

        # Direct double-click
        if pname == "explorer.exe":
            return True

        # py.exe launcher sits between explorer → py.exe → python.exe
        # Check grandparent too
        gp = parent.parent()
        if gp:
            gpname = gp.name().lower()
            _log(f"grandparent process: {gpname} (pid {gp.pid})")
            if gpname == "explorer.exe":
                return True
            if gpname in _WINDOWS_TERMINAL_PARENTS:
                return False

        return False   # unknown parent — assume terminal, don't relaunch

    except ImportError:
        _log("psutil not available — falling back to isatty()")
        try:
            return not sys.stdout.isatty()
        except Exception:
            return False
    except Exception as e:
        _log(f"_launched_from_explorer_windows error: {e}")
        return False


def needs_relaunch() -> bool:
    """True when the script was opened by double-click / file manager."""
    if IN_TERMINAL_FLAG in sys.argv:
        return False
    if "--no-pause" in sys.argv:
        return False

    if sys.platform == "win32":
        return _launched_from_explorer_windows()

    # macOS / Linux: isatty() is reliable
    try:
        return not sys.stdout.isatty()
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Relaunch
# ---------------------------------------------------------------------------

def relaunch_in_terminal() -> bool:
    """
    Open a terminal window running this script.
    Returns True if dispatched — caller should sys.exit(0) immediately.
    Returns False on failure — caller should fall back to pause-on-exit.
    """
    try:
        script   = _script_path()
        work_dir = _work_dir()
        extra    = _extra_args() + [IN_TERMINAL_FLAG]
        python   = sys.executable

        _log(f"relaunch: platform={sys.platform} python={python} "
             f"script={script} work_dir={work_dir} extra={extra}")

        if sys.platform == "win32":
            return _relaunch_windows(python, script, extra, work_dir)
        elif sys.platform == "darwin":
            return _relaunch_macos(python, script, extra, work_dir)
        else:
            return _relaunch_linux(python, script, extra, work_dir)

    except Exception as e:
        _log(f"relaunch top-level exception: {e}\n{traceback.format_exc()}")
        return False


def _relaunch_windows(python: str, script: str,
                      extra: list, work_dir: str) -> bool:
    """
    Spawn a new cmd.exe console window.
    CREATE_NEW_CONSOLE (0x10) creates a visible window.
    /k keeps the window open after the script exits (user closes manually).
    """
    try:
        CREATE_NEW_CONSOLE = 0x00000010
        cmd = ["cmd", "/k", python, script] + extra
        _log(f"Windows cmd: {cmd}  cwd={work_dir}")
        subprocess.Popen(cmd, creationflags=CREATE_NEW_CONSOLE, cwd=work_dir)
        return True
    except Exception as e:
        _log(f"_relaunch_windows failed: {e}\n{traceback.format_exc()}")
        return False


def _relaunch_macos(python: str, script: str,
                    extra: list, work_dir: str) -> bool:
    """
    Write a temp .command file and open it with `open`.
    macOS maps .command → Terminal.app natively (no AppleScript quoting).
    """
    import shlex
    cmd_line = " ".join(shlex.quote(a) for a in [python, script] + extra)
    content  = f"#!/bin/bash\ncd {shlex.quote(work_dir)}\n{cmd_line}\n"
    try:
        fd, path = tempfile.mkstemp(suffix=".command", prefix="sacn_")
        with os.fdopen(fd, "w") as f:
            f.write(content)
        os.chmod(path, 0o755)
        _log(f"macOS .command: {path}\n{content}")
        result = subprocess.run(["open", path], capture_output=True, text=True)
        if result.returncode != 0:
            _log(f"open failed: {result.stderr}")
            return False
        return True
    except Exception as e:
        _log(f"_relaunch_macos failed: {e}\n{traceback.format_exc()}")
        return False


def _relaunch_linux(python: str, script: str,
                    extra: list, work_dir: str) -> bool:
    """Try common Linux terminal emulators in priority order."""
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
            _log(f"Linux: launched with {term}")
            return True
        except Exception as e:
            _log(f"Linux: {term} failed: {e}")
    _log("Linux: no terminal emulator found")
    return False


# ---------------------------------------------------------------------------
# Pause-on-exit fallback
# ---------------------------------------------------------------------------

def _pause_before_exit(message: str = "") -> None:
    """
    Wait for Enter before exiting.
    Used when relaunch failed or as a safety net.
    Skipped when running inside the re-launched terminal or a real shell.
    """
    if IN_TERMINAL_FLAG in sys.argv:
        return   # inside re-launched window — it manages its own persistence
    if "--no-pause" in sys.argv:
        return
    # On Windows use our parent-process check; elsewhere use isatty
    if sys.platform == "win32":
        if not _launched_from_explorer_windows():
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
