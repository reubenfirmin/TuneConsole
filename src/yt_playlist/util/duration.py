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


def ago(seconds) -> str:
    """Humanize elapsed seconds as N days/hours/minutes ago (or just now)."""
    days = int(seconds // 86400)
    if days >= 1:
        return f"{days} day{'s' if days != 1 else ''} ago"
    hours = int(seconds // 3600)
    if hours >= 1:
        return f"{hours} hour{'s' if hours != 1 else ''} ago"
    minutes = int(seconds // 60)
    if minutes >= 1:
        return f"{minutes} minute{'s' if minutes != 1 else ''} ago"
    return "just now"
