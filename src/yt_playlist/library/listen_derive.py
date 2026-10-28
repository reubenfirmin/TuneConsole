"""#91 Read-time classification of raw player exits and explicit Next requests.

Thresholds live here as constants so tuning them is a server change. Rows stay
raw; skip judgments and temporary recording breaks are recomputed on read.
"""
from collections import deque

COMPLETION_RATIO = 0.85   # listened to >= 85% of the track -> completion
BOUNCE_MAX_S = 3.0        # left within 3s -> a mis-click, not a taste signal
SKIP_RATIO = 0.30         # left at <= 30% of the track...
SKIP_MAX_S = 120.0        # ...having listened <= 2 minutes -> an active rejection
SKIP_REPEAT_WINDOW_DAYS = 30
SKIP_BLACKOUT_DAYS = (1, 2, 4, 7)
# An active break can depend on skips thirty days before it began, even after
# those supporting skips have left the current repetition window.
SKIP_HISTORY_DAYS = SKIP_REPEAT_WINDOW_DAYS + max(SKIP_BLACKOUT_DAYS)


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


def skip_blackouts(events, now):
    """Temporary recording exclusions, independent of likes and taste weights.

    Repeated skips within thirty days extend the break to two, four, then seven
    days, counting repetitions at the time of each skip. A break keeps its expiry
    as old repetitions age out; future skips can start with a shorter break.
    Duplicate exit/goodbye observations of the same playback do not extend it.
    Callers must supply SKIP_HISTORY_DAYS of history to cover active breaks.
    """
    import math

    expiries, recent, previous = {}, {}, {}
    since = now - SKIP_HISTORY_DAYS * 86400
    for event in sorted(events, key=lambda row: row['at']):
        at, position, duration = event['at'], event['position'], event['duration']
        if not since <= at <= now:
            continue
        explicit = event['kind'] == 'next'
        position_known = isinstance(position, (int, float)) and math.isfinite(position)
        measured = all(
                isinstance(value, (int, float)) and math.isfinite(value)
                for value in (position, duration))
        if not explicit and (not measured or classify_exit(position, duration) != 'skip'):
            continue
        key = event['key']
        episode = (key, event['identity_id'])
        prior = previous.get(episode)
        # An explicit Next can be followed by an exit even when its media clock
        # was unavailable. Count that one request/playback only once.
        same_clock = (prior and position_known and prior[1] is not None
                      and abs((at - position) - (prior[0] - prior[1])) <= 3)
        unknown_clock = prior and (not position_known or prior[1] is None)
        if prior and (same_clock
                or (unknown_clock and (explicit or prior[2]) and at - prior[0] <= 45)
                or (at - prior[0] <= 5 and (explicit or prior[2]
                    or (position_known and prior[1] is not None and abs(position - prior[1]) <= 5)))):
            continue
        previous[episode] = (at, position if position_known else None, explicit)
        skips = recent.setdefault(key, deque(maxlen=len(SKIP_BLACKOUT_DAYS)))
        while skips and skips[0] < at - SKIP_REPEAT_WINDOW_DAYS * 86400:
            skips.popleft()
        skips.append(at)
        until = at + SKIP_BLACKOUT_DAYS[len(skips) - 1] * 86400
        # A later skip may get a shorter break after earlier repetitions expire;
        # it must not shorten a longer break that is still active.
        expiries[key] = max(expiries.get(key, until), until)
    return {key: until for key, until in expiries.items() if until > now}
