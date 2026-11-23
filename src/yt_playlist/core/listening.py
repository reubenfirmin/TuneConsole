"""Reconcile listening evidence inside the caller's database transaction."""
import datetime as dt
import logging
import os
from pathlib import Path

from . import catalog_keys as catalog
from .listening_calendar import zone as _zone, day_at, reconcile_foreign_days

logger = logging.getLogger(__name__)


def machine_timezone():
    name = os.environ.get('TZ','').lstrip(':')
    if _zone(name):
        return name
    try:
        name = str(Path('/etc/localtime').resolve()).split('/zoneinfo/',1)[1]
    except (OSError,IndexError):
        name = 'UTC'
    return name if _zone(name) else 'UTC'


def calendar(conn,account_id,source_timezone=None):
    """First explicit IANA calendar wins; imports before it are safely reprojected.

    A different later source calendar is retained on coarse evidence. A date in
    that calendar cannot be converted to an account date without a timestamp.
    """
    explicit = _zone(source_timezone)
    if source_timezone and explicit is None:
        logger.warning('Ignoring invalid listening timezone %r',source_timezone)
    row = conn.execute('SELECT listening_timezone, listening_timezone_confirmed FROM accounts WHERE id=?',
                       (account_id,)).fetchone()
    if row is None:
        raise ValueError('Listening calendar requires a recorded account')
    name, confirmed = row
    if name is None:
        name = source_timezone if explicit else machine_timezone()
        conn.execute('UPDATE accounts SET listening_timezone=?,listening_timezone_confirmed=? WHERE id=?',
                     (name,int(explicit is not None),account_id))
    elif not confirmed and explicit is not None:
        name = source_timezone
        conn.execute('UPDATE accounts SET listening_timezone=?,listening_timezone_confirmed=1 WHERE id=?',
                     (name,account_id))
        for event in conn.execute('SELECT id,played_at FROM play_events WHERE account_id=?',
                                  (account_id,)).fetchall():
            project_event(conn,event['id'],account_id,event['played_at'],timezone=name)
        conn.execute('UPDATE listening_observations SET day=CASE WHEN timezone=? THEN source_day END '
                     "WHERE account_id=? AND precision='calendar_day'",
                     (name,account_id))
        reconcile_foreign_days(conn,account_id)
    return name


def project_event(conn,event_id,account_id,timestamp,*,timezone=None,source=None):
    name = timezone or calendar(conn,account_id)
    store_event_day(conn, event_id, day_at(timestamp,name), source)


def store_event_day(conn, event_id, day, source):
    conn.execute('UPDATE play_events SET day=?,sources=CASE WHEN ? IS NULL OR EXISTS '
                 '(SELECT 1 FROM json_each(sources) WHERE value=?) THEN sources '
                 "ELSE json_insert(sources,'$[#]',?) END WHERE id=?",
                 (day,source,source,source,event_id))


def membership_reference(conn,account_id,playlist_ytm_id):
    if not playlist_ytm_id:
        return None
    memberships = conn.execute('SELECT p.id,p.is_current FROM playlists p '
        'JOIN playlist_scopes s ON s.id=p.playlist_scope_id WHERE p.account_id=? '
        'AND s.provider_playlist_id IN (?,?) ORDER BY p.is_current DESC LIMIT 2',
        (account_id,playlist_ytm_id,playlist_ytm_id.removeprefix('RDAMPL'))).fetchall()
    if len(memberships) == 1 or (len(memberships) == 2 and memberships[0][1] > memberships[1][1]):
        return memberships[0][0]
    return None


def append_event(conn,account_id,key,timestamp,*,video_id=None,playlist_ytm_id=None,
                 like_status=None,is_generated=None):
    """Capture raw provenance once; resolve only the demonstrated exact subject."""
    subject = catalog.play_key(conn,key,video_id)
    row = conn.execute('SELECT id FROM recordings WHERE identity_key=?',(subject,)).fetchone()
    return conn.execute('INSERT INTO play_events(account_id,raw_identity_key,recording_id,unresolved_subject_key,'
        'video_id,played_at,playlist_ytm_id,like_status,is_generated,membership_ref_id) VALUES (?,?,?,?,?,?,?,?,?,?)',
        (account_id,key,row[0] if row else None,None if row else subject,video_id,timestamp,
         playlist_ytm_id,like_status,is_generated,membership_reference(conn,account_id,playlist_ytm_id))).lastrowid


def resolve_pending(conn,recording_id):
    """Late catalog admission can resolve text evidence, but never an unknown exact upload."""
    keys = {r[0] for r in conn.execute('SELECT alias FROM recording_aliases WHERE recording_id=? '
        'UNION SELECT identity_key FROM recordings WHERE id=?',(recording_id,recording_id))}
    changed = False
    for key in keys:
        resolved = catalog.resolve_key(conn,key)
        target = conn.execute('SELECT id FROM recordings WHERE identity_key=?',(resolved,)).fetchone()
        if not target or target[0]!=recording_id:
            continue
        for table in ('play_events','listening_observations'):
            exact = " AND NULLIF(trim(video_id),'') IS NULL" if table=='play_events' else ''
            result = conn.execute(f'UPDATE main.{table} SET recording_id=?,unresolved_subject_key=NULL '
                f'WHERE recording_id IS NULL AND raw_identity_key=?{exact}',(recording_id,key))
            changed = changed or result.rowcount>0
    if changed:
        from .listening_calendar import reconcile_recordings
        reconcile_recordings(conn,{recording_id})


def observe_day(conn,account_id,key,source_day,source,timezone,observed_at,*,reconcile=True):
    """Retain a coarse source contribution; daily projection deduplicates subjects."""
    dt.date.fromisoformat(source_day)
    name = calendar(conn,account_id)
    subject = catalog.resolve_key(conn,key) or key
    recording = conn.execute('SELECT id FROM recordings WHERE identity_key=?',(subject,)).fetchone()
    day = source_day if timezone==name else None
    conn.execute(
        'INSERT INTO main.listening_observations(account_id,raw_identity_key,unresolved_subject_key,recording_id,'
        'source_day,day,source,timezone,precision,first_observed_at,last_observed_at) '
        "VALUES (?,?,?,?,?,?,?,?,'calendar_day',?,?) "
        'ON CONFLICT(account_id,raw_identity_key,source_day,source,timezone) DO UPDATE SET '
        'first_observed_at=MIN(first_observed_at,excluded.first_observed_at),'
        'last_observed_at=MAX(last_observed_at,excluded.last_observed_at)',
        (account_id,key,None if recording else subject,recording[0] if recording else None,source_day,day,source,
         timezone,observed_at,observed_at))
    if day is None:
        scope = subject, source_day, timezone
        if reconcile:
            reconcile_foreign_days(conn,account_id,observations={scope})
        return scope


def day_count(conn,account_id):
    return conn.execute('SELECT COUNT(*) FROM listening_days WHERE account_id=?',(account_id,)).fetchone()[0]


def delete_source(conn,source,account_id=None):
    from .history_policy import removable_source
    removable_source(source)
    scope = '' if account_id is None else ' AND account_id=?'
    args = (source,) if account_id is None else (source,account_id)
    conn.execute('DELETE FROM listening_observations WHERE source=?'+scope,args)
    conn.execute('DELETE FROM play_events WHERE json_array_length(sources)=1 AND EXISTS '
                 '(SELECT 1 FROM json_each(sources) WHERE value=?)'+scope,args)
    conn.execute("UPDATE play_events SET sources=json_remove(sources,'$[' || "
                 "(SELECT key FROM json_each(sources) WHERE value=?) || ']') WHERE EXISTS "
                 '(SELECT 1 FROM json_each(sources) WHERE value=?)'+scope,(source,*args))


def remove_source(conn,source,account_id=None):
    """Remove only known contributions, preserving overlapping and unknown evidence."""
    delete_source(conn,source,account_id)
    for row in conn.execute('SELECT id FROM accounts WHERE listening_timezone IS NOT NULL' +
                            ('' if account_id is None else ' AND id=?'),
                            () if account_id is None else (account_id,)).fetchall():
        reconcile_foreign_days(conn,row[0])
