"""Calendar projections shared by listening ingestion and identity reassociation."""
import datetime as dt
from collections import defaultdict
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

EXACT_DAYS = ('SELECT pe.played_at,c.day FROM play_events pe '
    'JOIN play_event_calendar c ON c.event_id=pe.id WHERE pe.account_id=? '
    'AND pe.recording_id=(SELECT id FROM recordings WHERE identity_key=?) '
    'AND pe.played_at>=? AND pe.played_at<? UNION ALL '
    'SELECT pe.played_at,c.day FROM play_events pe '
    'JOIN play_event_calendar c ON c.event_id=pe.id WHERE pe.account_id=? '
    'AND pe.unresolved_subject_key=? AND pe.played_at>=? AND pe.played_at<?')


def zone(name):
    try:
        return ZoneInfo(name) if name else None
    except (ZoneInfoNotFoundError, ValueError, TypeError):
        return None


def day_at(timestamp, timezone):
    return dt.datetime.fromtimestamp(timestamp, zone(timezone)).date().isoformat()


def _chunks(values, size):
    values = sorted(values)
    for offset in range(0, len(values), size):
        yield values[offset:offset + size]


def _subject_rows(conn, account_id, values, *, observations):
    """Resolve indexed canonical owners separately from unresolved captures.

    Filtering the coalesced display key on the compatibility view prevents SQLite
    from using the account/subject/date index. Both subject kinds can contribute
    to one current key, and each independent source row must survive.
    """
    fields = 'identity_key,source_day,timezone' if observations else 'identity_key'
    placeholders = ','.join('(?,?,?)' if observations else '(?)' for _ in values)
    date_scope = ' AND o.source_day=a.source_day AND o.timezone=a.timezone' if observations else ''
    select = ('SELECT o.id,a.identity_key,o.source_day,o.timezone,o.day FROM affected a ')
    calendar = ('JOIN accounts c ON c.id=o.account_id '
                "WHERE o.account_id=? AND o.precision='calendar_day' AND o.timezone<>c.listening_timezone")
    unresolved = (
        'CROSS JOIN listening_observations o ON o.recording_id IS NULL AND o.unresolved_subject_key=a.identity_key'+date_scope)
    query = (f'WITH affected({fields}) AS (VALUES {placeholders}) '
        + select + 'JOIN recordings r ON r.identity_key=a.identity_key '
        'JOIN listening_observations o ON o.recording_id=r.id'+date_scope+' '+calendar
        # Drive unresolved lookup from the bounded affected scopes too. Without
        # this order SQLite can scan every unresolved observation in the account.
        + ' UNION ALL '+select+unresolved+' '+calendar)
    parameters = tuple(value for scope in values for value in scope) if observations else tuple(values)
    return conn.execute(query, (*parameters, account_id, account_id)).fetchall()


def foreign_rows(conn, account_id, *, observations=None, keys=None):
    """Read foreign observations for bounded, already prepared subject scopes."""
    if observations is not None:
        for batch in _chunks(set(observations), 200):
            yield from _subject_rows(conn, account_id, batch, observations=True)
    elif keys is not None:
        for batch in _chunks(set(keys), 400):
            yield from _subject_rows(conn, account_id, batch, observations=False)
    else:
        yield from conn.execute(
            'SELECT o.id,o.identity_key,o.source_day,o.timezone,o.day FROM listening_observation_subjects o '
            'JOIN accounts c ON c.id=o.account_id WHERE o.account_id=? '
            "AND o.precision='calendar_day' AND o.timezone<>c.listening_timezone", (account_id,)).fetchall()


def _foreign_rows(conn, account_id, observations, events):
    by_key = defaultdict(set)
    for key, timestamp in events or ():
        by_key[key].add(timestamp)
    rows = foreign_rows(conn, account_id, observations=observations,
                        keys=by_key if events is not None and observations is None else None)
    source_days = {}
    for row in rows:
        if events is not None and observations is None:
            pair = row['identity_key'], row['timezone']
            if pair not in source_days:
                source_days[pair] = {day_at(at, pair[1]) for at in by_key[pair[0]]}
            if row['source_day'] not in source_days[pair]:
                continue
        yield row


def reconcile_foreign_days(conn, account_id, *, observations=None, events=None):
    """Anchor affected foreign dates; a missing or ambiguous exact match stays unknown.

    Observation scopes are (key, source day, timezone); event scopes are (key,
    timestamp). Full account reconciliation is reserved for calendar/identity
    changes and source removal. Multiple sources for one date share a lookup.
    """
    projected, bounds = {}, {}
    rows = _foreign_rows(conn, account_id, observations, events)
    for row in rows:
        scope = row['identity_key'], row['source_day'], row['timezone']
        if scope not in projected:
            date_zone = scope[1:]
            if date_zone not in bounds:
                date, timezone = dt.date.fromisoformat(scope[1]), zone(scope[2])
                bounds[date_zone] = (
                    dt.datetime.combine(date, dt.time(), timezone).timestamp(),
                    dt.datetime.combine(date + dt.timedelta(days=1), dt.time(), timezone).timestamp())
            matches = {
                event['day'] for event in conn.execute(EXACT_DAYS, (account_id, scope[0], *bounds[date_zone]) * 2)
                if day_at(event['played_at'], scope[2]) == scope[1]
            }
            projected[scope] = matches.pop() if len(matches) == 1 else None
        day = projected[scope]
        if day != row['day']:
            conn.execute('UPDATE main.listening_observations SET day=? WHERE id=?', (day, row['id']))


def reconcile_recordings(conn, recording_ids):
    """Reproject only foreign dates whose recording identity changed.

    Read the post-change subjects, including both sides of a partial correction.
    Exact events on either side can change an existing date's unique anchor.
    Coalesce sources and dates per account before asking for exact evidence.
    """
    recording_ids = sorted(set(recording_ids))
    if not recording_ids:
        return
    scopes = defaultdict(set)
    marks = ','.join('?' for _ in recording_ids)
    for row in conn.execute('SELECT o.account_id,o.identity_key,o.source_day,o.timezone '
                            'FROM listening_observation_subjects o JOIN accounts c ON c.id=o.account_id '
                            f'WHERE o.recording_id IN ({marks}) '
                            "AND o.precision='calendar_day' AND o.timezone<>c.listening_timezone",
                            recording_ids):
        scopes[row['account_id']].add((row['identity_key'], row['source_day'], row['timezone']))
    for account_id, observations in scopes.items():
        reconcile_foreign_days(conn, account_id, observations=observations)
