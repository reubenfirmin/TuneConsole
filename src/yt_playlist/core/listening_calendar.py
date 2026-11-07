"""Calendar projections shared by listening ingestion and identity reassociation."""
import datetime as dt
from collections import defaultdict
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def _subjects(conn):
    """Frozen migrations can reconcile the installed pre-subject vocabulary."""
    from . import entity_names
    typed = conn.execute("SELECT 1 FROM sqlite_schema WHERE name='play_event_subjects'").fetchone()
    return ('listening_observation_subjects' if typed else 'listening_observations',
            'play_event_subjects' if typed else 'play_events',
            'account_id' if entity_names.accounts(conn)=='accounts' else 'identity_id')


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
    from . import calendar_lookup as calendar_indexes
    indexed = calendar_indexes.selected(conn)
    fields = 'identity_key,source_day,timezone' if observations else 'identity_key'
    placeholders = ','.join('(?,?,?)' if observations else '(?)' for _ in values)
    date_scope = ' AND o.source_day=a.source_day AND o.timezone=a.timezone' if observations else ''
    select = ('SELECT o.id,a.identity_key,o.source_day,o.timezone,o.day FROM affected a ')
    calendar = ('JOIN listening_calendars c ON c.account_id=o.account_id '
                "WHERE o.account_id=? AND o.precision='calendar_day' AND o.timezone<>c.timezone")
    unresolved = ('CROSS JOIN '+calendar_indexes.OBSERVATIONS+' lookup '
        'ON lookup.account_id=? AND lookup.unresolved_subject_key=a.identity_key'+
        (' AND lookup.source_day=a.source_day AND lookup.timezone=a.timezone' if observations else '')+
        ' JOIN listening_observations o ON o.id=lookup.id') if indexed else (
        'CROSS JOIN listening_observations o ON o.recording_id IS NULL AND o.unresolved_subject_key=a.identity_key'+date_scope)
    query = (f'WITH affected({fields}) AS (VALUES {placeholders}) '
        + select + 'JOIN recordings r ON r.identity_key=a.identity_key '
        'JOIN listening_observations o ON o.recording_id=r.id'+date_scope+' '+calendar
        # Drive unresolved lookup from the bounded affected scopes too. Without
        # this order SQLite can scan every unresolved observation in the account.
        + ' UNION ALL '+select+unresolved+' '+calendar)
    parameters = tuple(value for scope in values for value in scope) if observations else tuple(values)
    return conn.execute(query,(*parameters,account_id,*((account_id,) if indexed else ()),account_id)).fetchall()


def foreign_rows(conn, account_id, *, observations=None, keys=None):
    """Read foreign observations for bounded, already prepared subject scopes."""
    observation_table,_,account = _subjects(conn)
    query = (f'SELECT o.id,o.identity_key,o.source_day,o.timezone,o.day FROM {observation_table} o '
             f'JOIN listening_calendars c ON c.{account}=o.{account} '
             '{scope_join} '+f'WHERE o.{account}=? '
             "AND o.precision='calendar_day' AND o.timezone<>c.timezone")
    if observations is not None:
        for batch in _chunks(set(observations), 200):
            if observation_table=='listening_observation_subjects':
                yield from _subject_rows(conn,account_id,batch,observations=True)
                continue
            placeholders = ','.join('(?,?,?)' for _ in batch)
            join = ('JOIN affected a ON a.identity_key=o.identity_key '
                    'AND a.source_day=o.source_day AND a.timezone=o.timezone')
            yield from conn.execute(
                f'WITH affected(identity_key,source_day,timezone) AS (VALUES {placeholders}) '
                + query.format(scope_join=join),
                (*(value for scope in batch for value in scope), account_id)).fetchall()
    elif keys is not None:
        for batch in _chunks(set(keys), 400):
            if observation_table=='listening_observation_subjects':
                rows = _subject_rows(conn,account_id,batch,observations=False)
            else:
                placeholders = ','.join('?' for _ in batch)
                rows = conn.execute(query.format(scope_join='') + f' AND o.identity_key IN ({placeholders})',
                                    (account_id, *batch)).fetchall()
            yield from rows
    else:
        yield from conn.execute(query.format(scope_join=''), (account_id,)).fetchall()


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
    from . import calendar_lookup as calendar_indexes
    _,event_table,account = _subjects(conn)
    indexed = event_table == 'play_event_subjects' and calendar_indexes.selected(conn)
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
            if event_table=='play_event_subjects':
                unresolved = ('FROM '+calendar_indexes.EVENTS+' lookup JOIN play_events pe ON pe.id=lookup.id '
                    'JOIN play_event_calendar c ON c.event_id=pe.id WHERE lookup.account_id=? '
                    'AND lookup.unresolved_subject_key=? AND lookup.played_at>=? AND lookup.played_at<?') if indexed else (
                    'FROM play_events pe JOIN play_event_calendar c ON c.event_id=pe.id WHERE pe.account_id=? '
                    'AND pe.unresolved_subject_key=? AND pe.played_at>=? AND pe.played_at<?')
                query = ('SELECT pe.played_at,c.day FROM play_events pe '
                    'JOIN play_event_calendar c ON c.event_id=pe.id WHERE pe.account_id=? '
                    'AND pe.recording_id=(SELECT id FROM recordings WHERE identity_key=?) '
                    'AND pe.played_at>=? AND pe.played_at<? UNION ALL '
                    # The subject CHECK makes a non-NULL unresolved key exclusive
                    # with recording_id. Drive its own index rather than the NULL recording bucket.
                    'SELECT pe.played_at,c.day '+unresolved)
                parameters = (account_id,scope[0],*bounds[date_zone])*2
            else:
                query = (
                    f'SELECT pe.played_at,c.day FROM {event_table} pe '
                    'JOIN play_event_calendar c ON c.event_id=pe.id '
                    f'WHERE pe.{account}=? AND pe.identity_key=? AND pe.played_at>=? AND pe.played_at<?')
                parameters = (account_id,scope[0],*bounds[date_zone])
            matches = {
                event['day'] for event in conn.execute(query,parameters)
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
    observation_table,_,account = _subjects(conn)
    marks = ','.join('?' for _ in recording_ids)
    for row in conn.execute(f'SELECT o.{account} account_id,o.identity_key,o.source_day,o.timezone '
                            f'FROM {observation_table} o JOIN listening_calendars c '
                            f'ON c.{account}=o.{account} '
                            f'WHERE o.recording_id IN ({marks}) '
                            "AND o.precision='calendar_day' AND o.timezone<>c.timezone",
                            recording_ids):
        scopes[row['account_id']].add((row['identity_key'], row['source_day'], row['timezone']))
    for account_id, observations in scopes.items():
        reconcile_foreign_days(conn, account_id, observations=observations)
