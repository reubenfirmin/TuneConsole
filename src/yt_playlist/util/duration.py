"""Parsing helpers for track durations."""


def parse_duration(text):
    """'3:45' / '1:02:03' -> seconds, else None."""
    if not text:
        return None
    try:
        parts = [int(p) for p in str(text).split(":")]
    except ValueError:
        return None
    secs = 0
    for p in parts:
        secs = secs * 60 + p
    return secs
