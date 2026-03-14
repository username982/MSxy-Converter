# MSxy_converter.spec
# -------------------
# PyInstaller build specification for MSxy Converter.
#
# This spec file works on both Windows and macOS — PyInstaller automatically
# produces the correct output for the platform it runs on:
#   Windows  →  MSxy_converter.exe  (in dist\MSxy_converter\)
#   macOS    →  MSxy_converter.app  (in dist/)
#
# Usage:
#   pyinstaller MSxy_converter.spec
#
# The output is a FOLDER (onedir mode), not a single file.  This starts
# faster than onefile because scipy/numpy don't need to re-extract each
# launch.  On Windows, distribute the entire MSxy_converter\ folder or
# zip it.  On macOS, distribute MSxy_converter.app (it's already a bundle).
#
# To build a true single-file .exe, change onefile=True below.
# Warning: single-file builds start ~5 s slower on first launch because
# scipy/numpy (~80 MB) must be extracted to a temp directory each time.

import sys
import os
from PyInstaller.utils.hooks import collect_submodules, collect_data_files

block_cipher = None

# ---------------------------------------------------------------------------
# Hidden imports — modules PyInstaller's static analyser misses because they
# are loaded dynamically (scipy internals, sacn protocol handlers, etc.)
# ---------------------------------------------------------------------------

hidden_imports = [
    # scipy submodules used at runtime
    "scipy.optimize",
    "scipy.optimize._lsq",
    "scipy.optimize._lsqr",
    "scipy.optimize._minpack",
    "scipy.sparse",
    "scipy.sparse.linalg",
    "scipy.spatial",
    "scipy.spatial.qhull",
    "scipy._lib.messagestream",
    "scipy._lib._util",
    "scipy.linalg",
    "scipy.linalg.blas",
    "scipy.linalg.lapack",
    "scipy.linalg.cython_blas",
    "scipy.linalg.cython_lapack",

    # numpy internals
    "numpy.core._multiarray_umath",
    "numpy.core._multiarray_tests",
    "numpy.random",

    # sacn E1.31 library
    "sacn",
    "sacn.sender",
    "sacn.receiver",
    "sacn.messages",
    "sacn.messages.data_packet",

    # psutil platform backends
    "psutil",
    "psutil._pslinux",
    "psutil._pswindows",
    "psutil._psosx",
    "psutil._psposix",

    # tkinter (usually auto-detected but list explicitly for safety)
    "tkinter",
    "tkinter.ttk",
    "tkinter.filedialog",
    "tkinter.messagebox",
    "tkinter.scrolledtext",

    # Our own package — list all submodules so none are missed
    "MSxy_converter",
    "MSxy_converter.__main__",
    "MSxy_converter.cct",
    "MSxy_converter.colour",
    "MSxy_converter.converter",
    "MSxy_converter.display",
    "MSxy_converter.gdtf_loader",
    "MSxy_converter.gui",
    "MSxy_converter.launcher",
    "MSxy_converter.network",
    "MSxy_converter.presets",
    "MSxy_converter.wizard",
]

# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------

a = Analysis(
    ["MSxy_converter_app.py"],        # top-level entry point
    pathex=["."],
    binaries=[],
    datas=[],
    hiddenimports=hidden_imports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # Things we definitely don't need — keeps the bundle smaller
        "matplotlib",
        "PIL",
        "IPython",
        "jupyter",
        "notebook",
        "pandas",
        "sklearn",
        "cv2",
        "wx",
        "PyQt5",
        "PyQt6",
        "PySide2",
        "PySide6",
        "gi",
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

# ---------------------------------------------------------------------------
# EXE / bundle
# ---------------------------------------------------------------------------

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,           # onedir mode — binaries go in COLLECT
    name="MSxy_converter",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,                        # compress with UPX if available
    console=False,                   # no terminal window on Windows/Mac
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    # icon="assets/MSxy_converter.ico",  # uncomment and add .ico for Windows
    # icon="assets/MSxy_converter.icns", # uncomment and add .icns for macOS
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="MSxy_converter",
)

# ---------------------------------------------------------------------------
# macOS .app bundle (ignored on Windows)
# ---------------------------------------------------------------------------

if sys.platform == "darwin":
    app = BUNDLE(
        coll,
        name="MSxy_converter.app",
        icon=None,                   # set to "assets/MSxy_converter.icns" if you have one
        bundle_identifier="com.markstuen.msxy-converter",
        info_plist={
            "CFBundleDisplayName":        "MSxy Converter",
            "CFBundleShortVersionString": "1.0.0",
            "CFBundleVersion":            "1.0.0",
            "NSHighResolutionCapable":    True,
            "NSHumanReadableCopyright":   "Copyright © 2026 Mark Stuen",
            # Allow network access (required for sACN multicast)
            "NSLocalNetworkUsageDescription":
                "MSxy Converter uses the local network to send and receive "
                "sACN (E1.31) lighting data.",
        },
    )
