"""Session controls shared by every radio stream; no durable taste writes."""
import math

DEFAULTS = {"comfort": .5, "theme": .5, "energy": .5}
NOW_REFRESH_SECONDS = 120


def controls(values):
    if not isinstance(values, dict) or set(values) != set(DEFAULTS):
        raise ValueError("Invalid stream controls")
    if any(type(v) not in (int, float) or not math.isfinite(v) or not 0 <= v <= 1
           for v in values.values()):
        raise ValueError("Invalid stream control value")
    return {key: float(values[key]) for key in DEFAULTS}


def now_bonus(base, share, gain):
    """Additive, bounded and monotonic even when the underlying score is negative."""
    return base + .12 * min(1., max(0., gain / 2)) * min(1., max(0., share))
