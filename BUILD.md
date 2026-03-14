# Building Standalone Executables

MSxy Converter can be packaged as a standalone desktop app that users
just double-click — no Python installation required.

| Platform | Output | Tool |
|----------|--------|------|
| Windows  | `MSxy_converter.exe` in a folder | PyInstaller |
| macOS    | `MSxy_converter.app` bundle | PyInstaller |

> **You must build on the target platform.**  
> A Mac cannot build the Windows `.exe`, and Windows cannot build the `.app`.

---

## One-time setup (both platforms)

Install PyInstaller and all runtime dependencies into the same Python
environment you use to run the app:

```bash
pip install pyinstaller
pip install sacn numpy scipy psutil
```

Verify PyInstaller works:
```bash
pyinstaller --version
```

---

## Building on Windows

From the repo root folder, double-click `build_windows.bat` or run it
from a command prompt:

```cmd
build_windows.bat
```

Output is placed in `dist\MSxy_converter\`.  The folder contains
`MSxy_converter.exe` and all supporting files.

**To distribute:** zip the entire `dist\MSxy_converter\` folder.
Users extract it anywhere and double-click `MSxy_converter.exe`.

Their `led_config.json` is created in the same folder on first run and
survives future updates as long as they extract to the same location.

### Windows icon (optional)

Create a 256×256 `.ico` file, place it at `assets\MSxy_converter.ico`,
and uncomment the `icon=` line in `MSxy_converter.spec`.

### Windows SmartScreen warning

Windows will show "Windows protected your PC" the first time users run
an unsigned `.exe`.  They click **More info → Run anyway** to proceed.
This warning disappears after they approve it once.

To eliminate the warning, sign with a code-signing certificate
(available from Sectigo, DigiCert, etc., ~$200–400/year).

---

## Building on macOS

From the repo root folder:

```bash
chmod +x build_mac.sh
./build_mac.sh
```

Output is `dist/MSxy_converter.app`.

**To distribute:** zip `dist/MSxy_converter.app` and share it.
For a more professional distribution, create a `.dmg`:

```bash
brew install create-dmg
create-dmg 'MSxy_converter.dmg' 'dist/MSxy_converter.app'
```

### macOS Gatekeeper warning (unsigned app)

Without an Apple Developer certificate, macOS shows:

> *"MSxy_converter.app" can't be opened because it is from an
> unidentified developer.*

**Users bypass this once by:**
1. Right-click the `.app` → **Open**
2. Click **Open** in the dialog that appears

After that one-time approval the app opens normally forever by
double-clicking.

### macOS signing and notarization (optional, eliminates warning)

If you have an Apple Developer account ($99/year):

```bash
# Sign the app
codesign --deep --force --verify --verbose \
  --sign "Developer ID Application: Mark Stuen (XXXXXXXXXX)" \
  dist/MSxy_converter.app

# Notarize (submits to Apple for approval, takes a few minutes)
xcrun notarytool submit dist/MSxy_converter.app \
  --apple-id "your@email.com" \
  --team-id "XXXXXXXXXX" \
  --password "app-specific-password" \
  --wait

# Staple the notarization ticket
xcrun stapler staple dist/MSxy_converter.app
```

After notarization, the Gatekeeper warning never appears regardless
of how users download or transfer the file.

### macOS network permissions

On first launch, macOS may ask:

> *"MSxy_converter" would like to find and connect to devices on
> your local network.*

Click **Allow**.  This is required for sACN multicast to work.

---

## Updating the app for users

1. Make your code changes
2. Run the build script on the appropriate platform
3. Share the new zip/dmg

Users replace their old folder or `.app` with the new one.
Their `led_config.json` lives next to the app and is unaffected.

---

## Troubleshooting builds

**Import errors at launch**

If the app crashes with an `ImportError` or `ModuleNotFoundError`,
a hidden import is missing.  Add it to the `hidden_imports` list in
`MSxy_converter.spec` and rebuild.

To diagnose, temporarily set `console=True` in the spec file —
the app will open a terminal window showing the traceback.

**App is very large (~150–250 MB)**

Normal for scipy/numpy bundles.  The `excludes` list in the spec file
already removes matplotlib, PIL, and other heavy packages.
If you installed Anaconda rather than plain Python, the bundle will be
larger because Anaconda includes many extras — use a clean `venv`
instead:

```bash
python -m venv venv
venv\Scripts\activate          # Windows
source venv/bin/activate       # Mac/Linux
pip install pyinstaller sacn numpy scipy psutil
pyinstaller MSxy_converter.spec
```

**"The app is damaged and can't be opened" on macOS**

This happens when macOS quarantines a `.app` downloaded from the
internet or extracted from a zip.  The build script already runs
`xattr -cr` to clear the quarantine flag.  If it reappears after
transferring the file, run:

```bash
xattr -cr MSxy_converter.app
```

**scipy / numpy DLL errors on Windows**

Install the Microsoft Visual C++ Redistributable from
https://aka.ms/vs/17/release/vc_redist.x64.exe
and retry.  This is a one-time system install.
