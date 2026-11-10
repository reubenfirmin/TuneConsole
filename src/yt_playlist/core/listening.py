"""Reconcile listening evidence inside the caller's database transaction."""
import datetime as dt
import logging
import os
from pathlib import Path

from . import catalog_keys as catalog, entity_names, first_play, listening_summaries
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
    calendars = entity_names.owner(conn, 'listening_calendars')
    row = conn.execute(f'SELECT timezone,confirmed FROM {calendars.table} WHERE {calendars.column("account_id")}=?',
                       (account_id,)).fetchone()
    if row is None:
        name = source_timezone if explicit else machine_timezone()
        conn.execute(f'INSERT INTO {calendars.table} VALUES (?,?,?)',
                     (account_id,name,int(explicit is not None)))
    elif not row['confirmed'] and explicit is not None:
        name = source_timezone
        observations = entity_names.owner(conn, 'listening_observations')
        conn.execute(f'UPDATE {calendars.table} SET timezone=?,confirmed=1 WHERE {calendars.column("account_id")}=?',
                     (name,account_id))
        for event in conn.execute('SELECT id,played_at FROM play_events WHERE account_id=?',
                                  (account_id,)).fetchall():
            project_event(conn,event['id'],account_id,event['played_at'],timezone=name)
        conn.execute(f'UPDATE {observations.table} SET day=CASE WHEN timezone=? THEN source_day END '
                     f"WHERE {observations.column('account_id')}=? AND precision='calendar_day'",
                     (name,account_id))
        reconcile_foreign_days(conn,account_id)
        first_play.invalidate(conn)
    else:
        name = row['timezone']
    return name


def project_event(conn,event_id,account_id,timestamp,*,timezone=None,source=None):
    name = timezone or calendar(conn,account_id)
    conn.execute('INSERT INTO play_event_calendar(event_id,day,timezone) VALUES (?,?,?) '
                 'ON CONFLICT(event_id) DO UPDATE SET day=excluded.day,timezone=excluded.timezone',
                 (event_id,day_at(timestamp,name),name))
    if source is not None:
        conn.execute('INSERT OR IGNORE INTO play_event_sources VALUES (?,?)',(event_id,source))


def membership_reference(conn,account_id,playlist_ytm_id):
    memberships_owner = entity_names.owner(conn, 'playlist_membership_refs')
    scopes = entity_names.owner(conn, 'playlist_scopes')
    memberships = conn.execute(f'SELECT m.id FROM {memberships_owner.table} m JOIN {scopes.table} s '
        f'ON s.{scopes.column("id")}=m.{memberships_owner.column("playlist_scope_id")} '
        f'WHERE m.{memberships_owner.column("account_id")}=? '
        f'AND (s.{scopes.column("provider_playlist_id")}=? OR \'RDAMPL\'||s.{scopes.column("provider_playlist_id")}=?)',
        (account_id,playlist_ytm_id,playlist_ytm_id)).fetchall()
    return memberships[0][0] if len(memberships)==1 else None


def append_event(conn,account_id,key,timestamp,*,video_id=None,playlist_ytm_id=None,
                 like_status=None,is_generated=None):
    """Capture raw provenance once; resolve only the demonstrated exact subject."""
    subject = catalog.play_key(conn,key,video_id)
    row = conn.execute('SELECT id FROM recordings WHERE identity_key=?',(subject,)).fetchone()
    events = entity_names.owner(conn, 'play_events')
    if not listening_summaries.available(conn):
        # The installed trigger resolves this original key and retains it as
        # raw_identity_key. Supplying the current subject would erase provenance.
        return conn.execute(f'INSERT INTO {events.table}({events.column("account_id")},identity_key,'
            'video_id,played_at,playlist_ytm_id,like_status,is_generated) VALUES (?,?,?,?,?,?,?)',
            (account_id,key,video_id,timestamp,playlist_ytm_id,like_status,is_generated)).lastrowid
    return conn.execute(f'INSERT INTO {events.table}({events.column("account_id")},raw_identity_key,recording_id,unresolved_subject_key,'
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
            assignment, parameters = ('unresolved_subject_key=NULL', (recording_id, key)) if listening_summaries.available(conn) else (
                'identity_key=?', (recording_id, resolved, key))
            result = conn.execute(f'UPDATE main.{table} SET recording_id=?,{assignment} '
                f'WHERE recording_id IS NULL AND raw_identity_key=?{exact}',parameters)
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
    observations = entity_names.owner(conn, 'listening_observations')
    typed = listening_summaries.available(conn)
    subject_column = 'unresolved_subject_key' if typed else 'identity_key'
    account_column = observations.column('account_id')
    conn.execute(
        f'INSERT INTO {observations.table}({account_column},raw_identity_key,{subject_column},recording_id,'
        'source_day,day,source,timezone,precision,first_observed_at,last_observed_at) '
        "VALUES (?,?,?,?,?,?,?,?,'calendar_day',?,?) "
        f'ON CONFLICT({account_column},raw_identity_key,source_day,source,timezone) DO UPDATE SET '
        'first_observed_at=MIN(first_observed_at,excluded.first_observed_at),'
        'last_observed_at=MAX(last_observed_at,excluded.last_observed_at)',
        (account_id,key,None if typed and recording else subject,recording[0] if recording else None,source_day,day,source,
         timezone,observed_at,observed_at))
    if day is None:
        scope = subject, source_day, timezone
        if reconcile:
            reconcile_foreign_days(conn,account_id,observations={scope})
        return scope


def day_count(conn,account_id):
    return conn.execute('SELECT COUNT(*) FROM listening_days WHERE account_id=?',(account_id,)).fetchone()[0]


def remove_source(conn,source,account_id=None):
    """Remove only known contributions, preserving overlapping and unknown evidence."""
    if source in {'unknown','independent',''}:
        raise ValueError('Unknown legacy provenance cannot be assigned to a removable source')
    observations = entity_names.owner(conn, 'listening_observations')
    events = entity_names.owner(conn, 'play_events')
    calendars = entity_names.owner(conn, 'listening_calendars')
    scope = '' if account_id is None else f' AND {observations.column("account_id")}=?'
    args = (source,) if account_id is None else (source,account_id)
    conn.execute(f'DELETE FROM {observations.table} WHERE source=?'+scope,args)
    conn.execute('DELETE FROM play_event_sources WHERE source=? AND event_id IN '
                 f'(SELECT id FROM {events.table} WHERE 1=1'+scope+')',args)
    conn.execute(f'DELETE FROM {events.table} WHERE NOT EXISTS '
                 '(SELECT 1 FROM play_event_sources s WHERE s.event_id=play_events.id)'+scope,
                 () if account_id is None else (account_id,))
    for row in conn.execute(f'SELECT {calendars.column("account_id")} FROM {calendars.table} WHERE 1=1'+scope,
                            () if account_id is None else (account_id,)).fetchall():
        reconcile_foreign_days(conn,row[0])
    first_play.invalidate(conn)
