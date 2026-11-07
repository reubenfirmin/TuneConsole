"""Source date interpretation and listening ingestion policy."""
import datetime
import logging
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

logger = logging.getLogger(__name__)
LIVE_DEDUP_S = 1800
EXACT_TOLERANCE_S = 2.0
UNDATED_BUCKETS = frozenset(('this week', 'last week'))


def parse_played_date(played, taken_at, source_timezone=None):
    """Resolve a source label to a noon-UTC date key, never an exact play time.

    Unknown labels stay undated. Repeated history windows must never turn an
    unparseable label into a fresh play on each sync.
    """
    timezone = None
    if source_timezone:
        try:
            timezone = ZoneInfo(source_timezone)
        except (ZoneInfoNotFoundError, ValueError, TypeError):
            logger.warning("history: invalid source timezone %r; using the machine's local timezone",
                           source_timezone)
    sync_day = datetime.datetime.fromtimestamp(taken_at, tz=timezone).date()
    label = (played or '').strip().lower()
    if label == 'today':
        day = sync_day
    elif label == 'yesterday':
        day = sync_day - datetime.timedelta(days=1)
    elif not label or label in UNDATED_BUCKETS:
        return None
    else:
        day = None
        text = (played or '').strip()
        for fmt, has_year in (('%b %d, %Y', True), ('%B %d, %Y', True),
                              ('%b %d', False), ('%B %d', False)):
            try:
                if has_year:
                    parsed = datetime.datetime.strptime(text, fmt).date()
                else:
                    parsed = datetime.datetime.strptime(f'{text} {sync_day.year}', f'{fmt} %Y').date()
                    if parsed > sync_day:
                        parsed = parsed.replace(year=sync_day.year - 1)
            except (ValueError, TypeError, AttributeError):
                continue
            day = parsed
            break
        if day is None:
            return None
    return (day - datetime.date(1970, 1, 1)).days * 86400 + 43200


def removable_source(source):
    if source in {'unknown', 'independent', ''}:
        raise ValueError('Unknown legacy provenance cannot be assigned to a removable source')
    return source


def play_subject(key, video, facts):
    """Exact upload evidence wins; ambiguous text never identifies a recording."""
    if video and video.strip():
        chosen = facts['exact']
        unresolved = '@unresolved-video:' + video if facts['direct'] else key
    else:
        aliases = facts['aliases']
        chosen = facts['direct'] or (aliases[0] if len(aliases) == 1 else None)
        unresolved = key
    return (chosen['identity_key'], chosen['id']) if chosen else (unresolved, None)


def repeats_live_event(latest, subject, video, timestamp):
    return bool(latest and latest['identity_key'] == subject
        and (not video or not latest['video_id'] or video == latest['video_id'])
        and 0 <= timestamp - latest['played_at'] < LIVE_DEDUP_S and latest['live'])
