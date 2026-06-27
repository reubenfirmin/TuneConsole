"""Pure SVG geometry for the Taste-model rose (coxcomb) charts. No web/store imports.

A rose draws one equal-width petal per category around a circle; the petal's outer radius encodes
its value. `rose_geometry` is the unsigned (distribution) form; `rose_geometry_signed` is the
diverging form for signed transient leans - 0 sits on a neutral ring, + grows outward, - pulls in.
Geometry is emitted as SVG path strings in a viewBox centred on (0,0); the template wraps them.
"""
import math


def _arc_path(inner, outer, a0, a1) -> str:
    """Annular sector path between angles a0..a1 (radians), radii inner..outer, centred at 0,0."""
    x0o, y0o = outer * math.cos(a0), outer * math.sin(a0)
    x1o, y1o = outer * math.cos(a1), outer * math.sin(a1)
    x0i, y0i = inner * math.cos(a0), inner * math.sin(a0)
    x1i, y1i = inner * math.cos(a1), inner * math.sin(a1)
    large = 1 if (a1 - a0) > math.pi else 0   # SVG large-arc-flag: 1 when the sweep exceeds 180 degrees
    return (f"M{x0o:.2f},{y0o:.2f} A{outer:.2f},{outer:.2f} 0 {large} 1 {x1o:.2f},{y1o:.2f} "
            f"L{x1i:.2f},{y1i:.2f} A{inner:.2f},{inner:.2f} 0 {large} 0 {x0i:.2f},{y0i:.2f} Z")


def _petals(values, gap_deg, start_deg, fracs) -> list:
    n = len(values)
    step = 360.0 / n
    half_gap = gap_deg / 2.0
    out = []
    for i, (v, frac) in enumerate(zip(values, fracs)):
        a0 = math.radians(start_deg + i * step + half_gap)
        a1 = math.radians(start_deg + (i + 1) * step - half_gap)
        out.append({"a0": a0, "a1": a1, "value": v, "frac": frac,
                    "mid_deg": (start_deg + (i + 0.5) * step) % 360})
    return out


def rose_geometry(values, *, radius=100.0, inner=18.0, gap_deg=4.0, start_deg=-90.0) -> list:
    """Unsigned coxcomb. Each petal: {path, mid_deg, value, frac}; frac = value / max(values)."""
    values = list(values)
    if not values:
        return []
    mx = max(values) or 0.0
    fracs = [(v / mx) if mx > 0 else 0.0 for v in values]
    petals = _petals(values, gap_deg, start_deg, fracs)
    for p in petals:
        outer = max(inner + p["frac"] * (radius - inner), inner + 0.5)   # empty petal stays a visible stub
        p["path"] = _arc_path(inner, outer, p["a0"], p["a1"])
    return petals


def rose_geometry_deviation(values, *, scale, radius=100.0, inner=18.0, neutral=0.5,
                            gap_deg=4.0, start_deg=-90.0, eps=0.03) -> list:
    """Deviation rose for signed values measured against an ABSOLUTE `scale` (not the per-rose max, so
    a near-flat set stays small instead of being blown up to full amplitude). Each petal is a *band*
    from the neutral baseline ring: positive grows outward, negative inward, and a value at/near zero
    draws nothing (empty path) - it simply sits on the ring. frac = clamp(value / scale, -1, 1).
    Each petal adds {sign, neutral_r}."""
    values = list(values)
    if not values:
        return []
    fracs = [max(-1.0, min(1.0, (v / scale) if scale else 0.0)) for v in values]
    petals = _petals(values, gap_deg, start_deg, fracs)
    span = radius - inner
    neutral_r = inner + neutral * span
    for p in petals:
        f = p["frac"]
        p["sign"] = 1 if f > 0 else (-1 if f < 0 else 0)
        p["neutral_r"] = neutral_r
        if abs(f) < eps:
            p["path"] = ""                      # no meaningful change -> no petal, rests on the ring
            continue
        if f > 0:                               # positive lean grows OUTWARD from the neutral ring
            r0, r1 = neutral_r, neutral_r + f * (radius - neutral_r)
        else:                                   # negative lean (f < 0) pulls INWARD toward the inner radius
            r0, r1 = neutral_r + f * (neutral_r - inner), neutral_r
        p["path"] = _arc_path(min(r0, r1), max(r0, r1), p["a0"], p["a1"])
    return petals
