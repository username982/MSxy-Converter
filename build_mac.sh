#!/bin/bash
# build_mac.sh
# ------------
# Builds MSxy_converter.app on macOS using PyInstaller.
# Run this from the MSxy_converter repo root folder.
#
# Prerequisites (run once):
#   pip3 install pyinstaller
#   pip3 install sacn numpy scipy psutil
#
# Output:
#   dist/MSxy_converter.app
#
# To distribute:
#   zip dist/MSxy_converter.app and share it, or drag to a .dmg.
#   Users double-click MSxy_converter.app — no Python needed.
#
# IMPORTANT — Gatekeeper (unsigned app warning):
#   Without an Apple Developer certificate, macOS will warn users
#   "MSxy_converter.app can't be opened because it is from an
#   unidentified developer."
#
#   Users can bypass this ONE TIME by:
#     Right-click the .app → Open → Open (in the dialog)
#   After that first approval it opens normally forever.
#
#   To suppress the warning entirely, sign with an Apple Developer
#   certificate ($99/year) and optionally notarize with Apple.
#   See the BUILD.md for signing instructions.

set -e

echo ""
echo "============================================================"
echo " MSxy Converter -- macOS build"
echo "============================================================"
echo ""

# Check PyInstaller
if ! command -v pyinstaller &> /dev/null; then
    echo "ERROR: PyInstaller not found."
    echo "Run:  pip3 install pyinstaller"
    exit 1
fi

# Clean previous build
echo "Cleaning previous build..."
rm -rf build/ dist/

# Run PyInstaller
echo "Running PyInstaller..."
pyinstaller MSxy_converter.spec --noconfirm

# Verify
if [ ! -d "dist/MSxy_converter.app" ]; then
    echo ""
    echo "BUILD FAILED -- .app not found in dist/"
    exit 1
fi

# Copy example config into the .app's Resources folder
# so it's available when the user first launches
cp led_config_example.json \
   "dist/MSxy_converter.app/Contents/Resources/led_config_example.json" \
   2>/dev/null || true

# Remove macOS quarantine attribute from the app
# (avoids the "damaged and can't be opened" message when copied from a zip)
xattr -cr "dist/MSxy_converter.app" 2>/dev/null || true

echo ""
echo "============================================================"
echo " BUILD SUCCEEDED"
echo " App bundle: dist/MSxy_converter.app"
echo ""
echo " To distribute:"
echo "   Zip dist/MSxy_converter.app and share it."
echo "   Users right-click → Open on first launch (Gatekeeper)."
echo "   After that, it opens normally by double-clicking."
echo ""
echo " For a .dmg (optional, more professional):"
echo "   brew install create-dmg"
echo "   create-dmg 'MSxy_converter.dmg' 'dist/MSxy_converter.app'"
echo "============================================================"
echo ""
