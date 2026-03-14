# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Mark Stuen
"""
colour.py
---------
Colour science utilities:
  - Build a 3×N LED gamut matrix from chip chromaticities + flux
  - Convert CIE xy + intensity to XYZ tristimulus
  - Bounded least-squares solver: XYZ target → per-chip weights (0–1)
  - DMX byte packing helpers
"""

import numpy as np
from scipy.optimize import lsq_linear
from scipy.spatial import ConvexHull


# ---------------------------------------------------------------------------
# Gamut hull (for xy clamping on fixtures without a master intensity ch)
# ---------------------------------------------------------------------------

def build_gamut_xy(leds: list) -> np.ndarray:
    """
    Return an (N, 2) array of LED xy chromaticity points.
    Used to build the fixture's gamut convex hull in CIE 1931 xy.
    """
    return np.array([[led["x"], led["y"]] for led in leds], dtype=float)


def apply_gm_trim(x: float, y: float, gm_trim: float) -> tuple:
    """
    Shift a CIE 1931 xy chromaticity toward green (+) or magenta (-)
    by applying a signed Duv offset in CIE 1960 UCS (uv) space.

    The green/magenta axis is approximately the v direction in CIE 1960 uv,
    which is perpendicular to the Planckian locus.  Shifting v directly is
    the same operation a console performs when its G/M encoder is turned.

    Parameters
    ----------
    x, y     : CIE 1931 xy chromaticity
    gm_trim  : signed Duv offset in CIE 1960 uv units
               +0.010 ≈ one stop toward green
               -0.010 ≈ one stop toward magenta
               Typical useful range: -0.020 to +0.020

    Returns
    -------
    (x_trimmed, y_trimmed) : CIE 1931 xy after trim
    """
    if abs(gm_trim) < 1e-6:
        return x, y

    # xy → CIE 1960 uv
    d = -2.0 * x + 12.0 * y + 3.0
    if abs(d) < 1e-10:
        return x, y
    u = 4.0 * x / d
    v = 6.0 * y / d

    # Apply green/magenta offset along v axis
    v = v + gm_trim

    # CIE 1960 uv → xy
    denom = 2.0 * u - 8.0 * v + 4.0
    if abs(denom) < 1e-10:
        return x, y
    x_out = 3.0 * u / denom
    y_out = 2.0 * v / denom

    # Clamp to valid chromaticity range
    x_out = float(np.clip(x_out, 0.0, 0.899))
    y_out = float(np.clip(y_out, 1e-6, 0.899))
    if x_out + y_out >= 1.0:
        t     = x_out + y_out + 1e-6
        x_out = (x_out / t) * 0.998
        y_out = (y_out / t) * 0.998

    return x_out, y_out



def clamp_xy_to_gamut(x: float, y: float,
                      gamut_pts: np.ndarray) -> tuple:
    """
    Clamp a CIE xy point to the convex hull of the fixture's LED primaries.

    If (x, y) is already inside the gamut it is returned unchanged.
    If outside, the nearest point on the gamut boundary is returned.

    This prevents the bounded least-squares solver from receiving XYZ
    targets far outside the fixture's reproducible gamut, which would
    otherwise cause all chips to rail at 255 (all-white output).

    Only applied for fixtures WITHOUT a master intensity channel.
    Fixtures WITH an intensity channel already handle out-of-gamut
    colours correctly via the NNLS + normalise solve path.

    Parameters
    ----------
    x, y       : input CIE 1931 chromaticity
    gamut_pts  : (N, 2) array of LED primary xy points from build_gamut_xy()

    Returns
    -------
    (x_clamped, y_clamped) : tuple of floats
    """
    if len(gamut_pts) < 3:
        return x, y

    pt = np.array([x, y], dtype=float)

    try:
        hull = ConvexHull(gamut_pts)
    except Exception:
        return x, y

    # hull.equations rows: [a, b, c] — inside when a*x + b*y + c <= 0
    if np.all(hull.equations @ np.append(pt, 1.0) <= 1e-9):
        return x, y          # already inside — nothing to do

    # Outside: project onto every hull edge, return the nearest result
    verts   = gamut_pts[hull.vertices]
    n       = len(verts)
    best_pt = None
    best_d  = float("inf")

    for i in range(n):
        a   = verts[i]
        b   = verts[(i + 1) % n]
        ab  = b - a
        ab2 = float(np.dot(ab, ab))
        if ab2 < 1e-12:
            proj = a
        else:
            t    = float(np.clip(np.dot(pt - a, ab) / ab2, 0.0, 1.0))
            proj = a + t * ab
        d = float(np.linalg.norm(pt - proj))
        if d < best_d:
            best_d  = d
            best_pt = proj

    if best_pt is not None:
        return float(best_pt[0]), float(best_pt[1])
    return x, y


# ---------------------------------------------------------------------------
# Gamut matrix
# ---------------------------------------------------------------------------

def build_led_matrix(leds: list) -> np.ndarray:
    """
    Return a 3×N matrix M where column i = [X, Y, Z] of LED chip i at
    full output.  Normalised so the brightest chip has Y = 1.0, which
    keeps target XYZ values in the same 0–1 range as DMX intensity.

    Parameters
    ----------
    leds : list of dicts with keys  x (CIE), y (CIE), flux (relative lm)

    Returns
    -------
    M : np.ndarray, shape (3, N)
    """
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


# ---------------------------------------------------------------------------
# CIE xy → XYZ
# ---------------------------------------------------------------------------

def xy_intensity_to_XYZ(x: float, y: float, intensity: float) -> np.ndarray:
    """
    Convert CIE xy chromaticity coordinates and a photometric intensity
    (0–1) to XYZ tristimulus values.

    Returns a zero vector when y or intensity is negligibly small.
    """
    if y < 1e-7 or intensity < 1e-7:
        return np.zeros(3)
    Y = float(intensity)
    return np.array([(Y / y) * x, Y, (Y / y) * (1.0 - x - y)])


# ---------------------------------------------------------------------------
# Solver
# ---------------------------------------------------------------------------

def solve_led_weights(target_XYZ: np.ndarray, M: np.ndarray,
                      has_intensity_ch: bool = False) -> np.ndarray:
    """
    Solve for per-chip colour-direction weights using NNLS + normalise.

    Both fixture types now use the same NNLS (non-negative, unbounded)
    solve followed by normalising the result so max(w) = 1.0.

    Why NNLS + normalise for both:
    --------------------------------
    The bounded least-squares approach (w ∈ [0,1]) fails for any colour
    whose XYZ has a component larger than the fixture can produce —
    typically Z for deep-blue/violet or X for deep-red.  The solver
    rails all chips to 1.0 trying to close the gap, producing all-white.

    NNLS with no upper bound lets the dominant chip grow as large as needed
    to match the *direction* of the target XYZ; normalising back to 1.0
    then gives the correct chip mix for any achievable or out-of-gamut
    input.  Out-of-gamut requests map to the nearest achievable spectral
    direction rather than saturating every chip.

    Intensity handling:
    --------------------------------
    has_intensity_ch = True  — Int channel carries brightness; weights
        returned at full scale (max = 1.0).  The fixture multiples Int×chip.

    has_intensity_ch = False — No separate Int channel; caller scales the
        returned weights by the actual intensity value before writing DMX.
        See converter.py process() for the scaling step.

    Parameters
    ----------
    target_XYZ      : np.ndarray shape (3,) — unit-intensity XYZ target
                      (caller is responsible for passing unit-intensity XYZ
                      for has_intensity_ch=False fixtures)
    M               : np.ndarray shape (3, N) — LED gamut matrix
    has_intensity_ch: bool (informational only — solver path is now identical)

    Returns
    -------
    w : np.ndarray shape (N,), values in [0, 1] — normalised chip weights
    """
    if target_XYZ.max() < 1e-9:
        return np.zeros(M.shape[1])
    try:
        result = lsq_linear(M, target_XYZ, bounds=(0.0, np.inf))
        w      = np.clip(result.x, 0.0, None)
        peak   = w.max()
        if peak < 1e-9:
            return np.zeros(M.shape[1])
        return w / peak        # normalise: dominant chip = 1.0
    except Exception as e:
        print(f"\n  SOLVER ERROR: {e}", flush=True)
        return np.zeros(M.shape[1])


# ---------------------------------------------------------------------------
# DMX helpers
# ---------------------------------------------------------------------------

def unpack16(high: int, low: int) -> float:
    """16-bit big-endian MSB/LSB pair → float 0–1."""
    return ((int(high) << 8) | int(low)) / 65535.0


def write_channel(buf: list, pos: int, value_0_1: float,
                  bit_depth: int, gamma: float = 1.0) -> int:
    """
    Write one controlled DMX channel (intensity or LED chip) into buf.

    8-bit  : writes 1 byte  at buf[pos],                returns pos + 1
    16-bit : writes MSB then LSB at buf[pos], buf[pos+1], returns pos + 2

    Parameters
    ----------
    buf        : mutable list of ints, length 512
    pos        : 0-based index into buf
    value_0_1  : normalised value in [0, 1]
    bit_depth  : 8 or 16
    gamma      : output gamma (applied before quantisation)

    Returns
    -------
    next_pos : int
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
