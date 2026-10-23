"""Calendar projections shared by listening ingestion and identity reassociation."""
import datetime as dt
from collections import defaultdict
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


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


def _foreign_rows(conn, identity_id, observations, events):
    query = ('SELECT o.id,o.identity_key,o.source_day,o.timezone,o.day FROM listening_observations o '
             'JOIN listening_calendars c ON c.identity_id=o.identity_id {scope_join} WHERE o.identity_id=? '
             "AND o.precision='calendar_day' AND o.timezone<>c.timezone")
    if observations is not None:
        for batch in _chunks(set(observations), 200):
            placeholders = ','.join('(?,?,?)' for _ in batch)
            join = ('JOIN affected a ON a.identity_key=o.identity_key '
                    'AND a.source_day=o.source_day AND a.timezone=o.timezone')
            yield from conn.execute(
                f'WITH affected(identity_key,source_day,timezone) AS (VALUES {placeholders}) '
                + query.format(scope_join=join),
                (*(value for scope in batch for value in scope), identity_id)).fetchall()
    elif events is not None:
        by_key = defaultdict(set)
        for key, timestamp in events:
            by_key[key].add(timestamp)
        source_days = {}
        for batch in _chunks(by_key, 400):
            placeholders = ','.join('?' for _ in batch)
            rows = conn.execute(query.format(scope_join='') + f' AND o.identity_key IN ({placeholders})',
                                (identity_id, *batch)).fetchall()
            for row in rows:
                pair = row['identity_key'], row['timezone']
                if pair not in source_days:
                    source_days[pair] = {day_at(at, pair[1]) for at in by_key[pair[0]]}
                if row['source_day'] in source_days[pair]:
                    yield row
    else:
        yield from conn.execute(query.format(scope_join=''), (identity_id,)).fetchall()


def reconcile_foreign_days(conn, identity_id, *, observations=None, events=None):
    """Anchor affected foreign dates; a missing or ambiguous exact match stays unknown.

    Observation scopes are (key, source day, timezone); event scopes are (key,
    timestamp). Full account reconciliation is reserved for calendar/identity
    changes and source removal. Multiple sources for one date share a lookup.
    """
    projected, bounds = {}, {}
    rows = _foreign_rows(conn, identity_id, observations, events)
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
                event['day'] for event in conn.execute(
                    'SELECT pe.played_at,c.day FROM play_events pe '
                    'JOIN play_event_calendar c ON c.event_id=pe.id '
                    'WHERE pe.identity_id=? AND pe.identity_key=? AND pe.played_at>=? AND pe.played_at<?',
                    (identity_id, scope[0], *bounds[date_zone]))
                if day_at(event['played_at'], scope[2]) == scope[1]
            }
            projected[scope] = matches.pop() if len(matches) == 1 else None
        day = projected[scope]
        if day != row['day']:
            conn.execute('UPDATE listening_observations SET day=? WHERE id=?', (day, row['id']))
