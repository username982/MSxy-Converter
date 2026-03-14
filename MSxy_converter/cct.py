# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Mark Stuen
"""
cct.py
------
Correlated Colour Temperature (CCT) and Duv calculation.

CCT  — Robertson (1968) isotemperature line method.
       Accurate from ~1000 K to ~20 000 K.

Duv  — Signed distance from the Planckian locus in CIE 1960 UCS (uv).
       Positive = above locus (greenish / cool tint).
       Negative = below locus (magenta / rosy tint).
       Planckian locus points from Krystek (1985) rational approximation.

Both are computed in CIE 1960 UCS (u, v) — the older uniform colour space
still used by IES TM-30 and ANSI C78.377 for white-point characterisation.

Reference
---------
Robertson, A.R. (1968). Computation of correlated color temperature and
  distribution temperature.  J. Opt. Soc. Am. 58, 1528–1535.
Krystek, M. (1985). An algorithm to calculate correlated colour temperature.
  Color Res. Appl. 10, 38–40.
"""

import math

# ---------------------------------------------------------------------------
# Robertson isotemperature line table
# (reciprocal megakelvin r, CIE 1960 u_i, v_i, slope t_i)
# ---------------------------------------------------------------------------

_ROBERTSON = [
    (0,   0.18006, 0.26352, -0.24341),
    (10,  0.18066, 0.26589, -0.25479),
    (20,  0.18133, 0.26846, -0.26876),
    (30,  0.18208, 0.27119, -0.28539),
    (40,  0.18293, 0.27407, -0.30470),
    (50,  0.18388, 0.27709, -0.32675),
    (60,  0.18494, 0.28021, -0.35156),
    (70,  0.18611, 0.28342, -0.37915),
    (80,  0.18740, 0.28668, -0.40955),
    (90,  0.18880, 0.28997, -0.44278),
    (100, 0.19032, 0.29326, -0.47888),
    (125, 0.19462, 0.30141, -0.58204),
    (150, 0.19962, 0.30921, -0.70471),
    (175, 0.20525, 0.31647, -0.84901),
    (200, 0.21142, 0.32312, -1.0182),
    (225, 0.21807, 0.32909, -1.2168),
    (250, 0.22511, 0.33439, -1.4512),
    (275, 0.23247, 0.33904, -1.7298),
    (300, 0.24010, 0.34308, -2.0637),
    (325, 0.24792, 0.34655, -2.4681),
    (350, 0.25591, 0.34951, -2.9641),
    (375, 0.26400, 0.35200, -3.5814),
    (400, 0.27218, 0.35407, -4.3633),
    (425, 0.28039, 0.35577, -5.3762),
    (450, 0.28863, 0.35714, -6.7262),
    (475, 0.29685, 0.35823, -8.5955),
    (500, 0.30505, 0.35907, -11.324),
    (525, 0.31320, 0.35968, -15.628),
    (550, 0.32129, 0.36011, -23.325),
    (575, 0.32931, 0.36038, -40.770),
    (600, 0.33724, 0.36051, -116.45),
]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def xy_to_uv60(x: float, y: float):
    """CIE 1931 xy  →  CIE 1960 UCS (u, v)."""
    d = -2.0 * x + 12.0 * y + 3.0
    if abs(d) < 1e-10:
        return 0.0, 0.0
    return 4.0 * x / d, 6.0 * y / d


def planckian_uv60(T: float):
    """
    Planckian locus point in CIE 1960 uv at temperature T (kelvin).
    Uses Krystek (1985) rational approximations, accurate ~1000–15000 K.
    """
    T2 = T * T
    u = (0.860117757 + 1.54118254e-4 * T + 1.28641212e-7 * T2) / \
        (1.0 + 8.42420235e-4 * T + 7.08145163e-7 * T2)
    v = (0.317398726 + 4.22806245e-5 * T + 4.20481691e-8 * T2) / \
        (1.0 - 2.89741816e-5 * T + 1.61456053e-7 * T2)
    return u, v


def xy_to_cct_duv(x: float, y: float, duv_limit: float = 0.05):
    """
    Compute CCT and Duv for a CIE 1931 xy chromaticity.

    Returns (cct_kelvin, duv) or None if the point is too far from the
    Planckian locus (|Duv| > duv_limit) or outside the table range.

    Parameters
    ----------
    x, y       : CIE 1931 chromaticity
    duv_limit  : maximum |Duv| to return a result (default 0.05)

    Returns
    -------
    (cct, duv) : tuple of floats, or None
    """
    u, v = xy_to_uv60(x, y)

    # --- Robertson CCT ---
    d_prev = None
    r_prev = None
    cct    = None

    for i, (r, u_i, v_i, t_i) in enumerate(_ROBERTSON):
        # Signed distance to isotemperature line i
        d = (v - v_i) - t_i * (u - u_i)

        if d_prev is not None and d_prev * d < 0:
            # Crossed between i-1 and i — interpolate
            f = d_prev / (d_prev - d)
            mired = r_prev + f * (r - r_prev)
            if mired < 1e-6:
                cct = 1e9      # effectively infinity
            else:
                cct = 1e6 / mired
            break

        d_prev = d
        r_prev = r

    if cct is None:
        return None   # outside table range

    # Clamp to a reasonable display range
    if not (800 <= cct <= 25000):
        return None

    # --- Duv (signed distance from Planckian locus in CIE 1960 uv) ---
    u_p, v_p = planckian_uv60(cct)
    dist = math.sqrt((u - u_p) ** 2 + (v - v_p) ** 2)
    # Sign: positive above locus (toward green), negative below (toward magenta)
    duv = math.copysign(dist, v - v_p)

    if abs(duv) > duv_limit:
        return None

    return round(cct), round(duv, 4)
