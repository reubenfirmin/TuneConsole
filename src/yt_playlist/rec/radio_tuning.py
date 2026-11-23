"""Session controls shared by every radio stream; no durable taste writes."""
import math

from yt_playlist.rec.journeys import JOURNEYS

DEFAULTS = {"comfort": .5, "theme": .5, "energy": .5}
NOW_REFRESH_SECONDS = 120


def controls(values):
    if not isinstance(values, dict) or set(values) - {'journey'} != set(DEFAULTS):
        raise ValueError("Invalid stream controls")
    if any(type(values[key]) not in (int, float) or not math.isfinite(values[key])
           or not 0 <= values[key] <= 1 for key in DEFAULTS):
        raise ValueError("Invalid stream control value")
    result = {key: float(values[key]) for key in DEFAULTS}
    journey = values.get('journey')
    if journey is not None:
        if journey not in JOURNEYS:
            raise ValueError("Invalid DJ sequencing style")
        result['journey'] = journey
    return result


def now_bonus(base, share, gain):
    """Additive, bounded and monotonic even when the underlying score is negative."""
    return base + .12 * min(1., max(0., gain / 2)) * min(1., max(0., share))
