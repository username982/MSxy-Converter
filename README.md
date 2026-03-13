# MSxy-Converter

A Python utility that receives sACN (E1.31) fixtures using **16-bit Dimmer + CIE XY** color control from a lighting console, calculates the optimal mix of LED emitter channels to reproduce that color, and retransmits the result as sACN to an LED driver or fixture.

Designed for wide-gamut multi-chip fixtures like the **ETC Fos/4 Panel** where a console sends a single color point and the converter figures out how much Deep Red, Red, Amber, Lime, Green, Cyan, Blue, and Indigo to use.

```
Console (EOS/MA/Chamsys)          sACN Color Converter          LED Fixture / Driver
  Universe 1                  ──────────────────────────────>   Universe 2
  Ch 1-6 per fixture                                            Ch 1-12 per fixture (Fos/4)
  Dimmer + CIE X + CIE Y          color math here              Int + DR + R + A + Lm + G + Cy + B + I + ...
```

---

## Requirements

- Python 3.9 or newer
- A lighting console that outputs CIE XY over sACN (ETC EOS, grandMA, Chamsys, etc.)
- A network adapter on the same subnet as your console and LED driver

---

## Installation

Download the `.whl` file from the [Releases](../../releases) page, then:

```bash
pip install sacn_color_converter-1.0.0-py3-none-any.whl
```

Or install from source:

```bash
git clone https://github.com/your-username/sacn-color-converter.git
cd sacn-color-converter
pip install .
```

---

## Usage

**First run** — an interactive setup wizard walks you through network interfaces, fixture type, and universe patching, then saves a config file:

```bash
sacn-converter
```

**Subsequent runs** reuse the saved config automatically:

```bash
sacn-converter                  # uses led_config.json in current folder
sacn-converter fos4.json        # named config
sacn-converter fos4.json --debug  # per-frame solver output
```

**Double-clicking** `sacn_color_converter.py` in Windows Explorer works — it will open a cmd window automatically.

**Multiple instances** for multiple fixture types or universes:

```bash
sacn-converter wash.json
sacn-converter cyc.json
```

---

## Fixture Profiles

### Built-in presets

| # | Type | Chips |
|---|------|-------|
| 1 | RGB | Red, Green, Blue |
| 2 | RGBA | RGB + Amber |
| 3 | RGBW | RGB + Warm White |
| 4 | RGB + WW/CW | 5-chip warm/cool white |
| 5 | ETC Fos/4 Panel — Direct | 8-chip (DR, R, A, Lm, G, Cy, B, I) |
| 6 | Custom | Define up to 20 chips manually |

### GDTF fixture profiles

Drop any `.gdtf` file into a `gdtf/` folder next to your config file and the wizard will detect it automatically:

```
my-show/
  led_config.json
  gdtf/
    ETC@Fos_4_Panel@Direct_Mode.gdtf
    Ayrton_Rivale.gdtf
```

The wizard parses the GDTF file, lists all available DMX modes with their channel layout, and pre-fills emitter chromaticity values from the file. Any chips missing emitter data are flagged so you can enter measured values manually.

GDTF files for most fixtures are available at [gdtf-share.com](https://gdtf-share.com).

---

## Input DMX Layout (from console)

Six channels per fixture, always:

| Offset | Channel | Notes |
|--------|---------|-------|
| +0 | Dimmer MSB | 16-bit, big-endian |
| +1 | Dimmer LSB | |
| +2 | CIE X MSB | 0–65535 → 0.000–1.000 |
| +3 | CIE X LSB | |
| +4 | CIE Y MSB | |
| +5 | CIE Y LSB | |

---

## Output DMX Layout (to fixture / driver)

Configured per fixture. Example for ETC Fos/4 Panel Direct (12 ch):

| Ch | Signal | Notes |
|----|--------|-------|
| 1 | Intensity | Raw dimmer passthrough |
| 2 | Deep Red | Solver output |
| 3 | Red | |
| 4 | Amber | |
| 5 | Lime | |
| 6 | Green | |
| 7 | Cyan | |
| 8 | Blue | |
| 9 | Indigo | |
| 10 | Strobe | Held at 0 |
| 11 | Curve | Held at 0 |
| 12 | Fan | Held at 0 |

---

## Config File Reference

The wizard generates `led_config.json`. You can edit it directly:

```json
{
  "input_universe":    1,
  "output_universe":   2,
  "num_fixtures":      4,
  "input_start_addr":  1,
  "output_start_addr": 1,
  "bind_ip":           "192.168.1.50",
  "send_ip":           "192.168.1.50",
  "output_ip":         null,
  "has_intensity_ch":  true,
  "output_bit_depth":  8,
  "control_channels":  ["Strobe", "Curve", "Fan"],
  "xy_scale":          1.0,
  "output_gamma":      1.0,
  "leds": [
    {"name": "Deep Red", "x": 0.7134, "y": 0.2798, "flux": 169.0},
    {"name": "Red",      "x": 0.6984, "y": 0.3001, "flux": 350.0},
    {"name": "Amber",    "x": 0.5747, "y": 0.4241, "flux": 771.0},
    {"name": "Lime",     "x": 0.4190, "y": 0.5529, "flux": 2260.0},
    {"name": "Green",    "x": 0.1922, "y": 0.7314, "flux": 960.0},
    {"name": "Cyan",     "x": 0.0717, "y": 0.5173, "flux": 700.0},
    {"name": "Blue",     "x": 0.1232, "y": 0.0906, "flux": 256.0},
    {"name": "Indigo",   "x": 0.1559, "y": 0.0227, "flux": 119.0}
  ]
}
```

| Key | Description |
|-----|-------------|
| `input_universe` / `output_universe` | sACN universe numbers (1–63999) |
| `num_fixtures` | Number of fixtures patched consecutively |
| `input_start_addr` / `output_start_addr` | 1-based DMX start address |
| `bind_ip` | IP of the NIC to receive sACN on |
| `send_ip` | IP of the NIC to transmit sACN from |
| `output_ip` | Unicast destination IP — `null` for multicast |
| `has_intensity_ch` | `true` if the fixture expects a master intensity channel first |
| `output_bit_depth` | `8` or `16` bits per chip channel |
| `control_channels` | Trailing fixture channels held at 0 (strobe, fan, etc.) |
| `xy_scale` | XY full-scale factor — `1.0` for EOS, MA, and Chamsys |
| `output_gamma` | Output gamma exponent — `1.0` for linear |
| `leds` | Emitter chips with CIE 1931 xy chromaticity and relative flux |

---

## ETC Fos/4 Calibration Data

The built-in Fos/4 preset uses spectrometer-measured values for all 8 emitters:

| Chip | CIE x | CIE y | Flux (fc) |
|------|-------|-------|-----------|
| Deep Red | 0.7134 | 0.2798 | 169 |
| Red      | 0.6984 | 0.3001 | 350 |
| Amber    | 0.5747 | 0.4241 | 771 |
| Lime     | 0.4190 | 0.5529 | 2260 |
| Green    | 0.1922 | 0.7314 | 960 |
| Cyan     | 0.0717 | 0.5173 | 700 |
| Blue     | 0.1232 | 0.0906 | 256 |
| Indigo   | 0.1559 | 0.0227 | 119 |

Emitter xy coordinates shift with fixture temperature and drive current. For critical work, measure your own fixtures with a spectrometer and update the `leds` array in your config.

---

## Calibration Tips

- **Flux values** are relative — only the ratios between chips matter. The solver uses them to weight which chips to favor for a given color.
- **D65 reference white:** x=0.3127, y=0.3290
- **`--debug` mode** prints solver weights per frame. Use it to see exactly which chips are being driven and at what level.
- For fixtures with a dedicated **intensity channel** (`has_intensity_ch: true`), the converter solves for color direction only and normalizes chip outputs to full scale — the fixture's Int channel carries the brightness. This is what prevents wide-gamut fixtures like the Fos/4 from producing muddy colors near the spectral limits.

---

## How It Works

1. Receives sACN multicast via a raw UDP socket (coexists with sACNview and other listeners on port 5568)
2. Decodes 16-bit Dimmer, CIE X, and CIE Y for each fixture
3. Converts CIE xy + intensity to XYZ tristimulus target
4. Runs a non-negative least-squares solver to find the best chip mix
5. Writes the result to an output DMX buffer
6. Retransmits at 44 Hz via sACN multicast or unicast

---

## License

MIT — Copyright (c) 2026 Mark Stuen. See [LICENSE](LICENSE).
