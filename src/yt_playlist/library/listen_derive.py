"""#91 Read-time classification of raw player_events exit observations (track_exit / bye rows).

Pure functions over (position, duration); thresholds live here as constants so tuning them is a
server change, never an extension release. Nothing is persisted: rows stay raw, judgments are
recomputed on read."""

COMPLETION_RATIO = 0.85   # listened to >= 85% of the track -> completion
BOUNCE_MAX_S = 3.0        # left within 3s -> a mis-click, not a taste signal
SKIP_RATIO = 0.30         # left at <= 30% of the track...
SKIP_MAX_S = 120.0        # ...having listened <= 2 minutes -> an active rejection


def classify_exit(position, duration) -> str:
    """One exit observation -> completion | skip | bounce | partial | unknown."""
    if position is None or not duration or duration <= 0:
        return "unknown"
    ratio = position / duration
    if ratio >= COMPLETION_RATIO:
        return "completion"
    if position < BOUNCE_MAX_S:
        return "bounce"
    if ratio <= SKIP_RATIO and position <= SKIP_MAX_S:
        return "skip"
    return "partial"
