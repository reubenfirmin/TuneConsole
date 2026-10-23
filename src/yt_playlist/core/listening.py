"""Reconcile listening evidence inside the caller's database transaction."""
import datetime as dt
import logging
import os
from pathlib import Path

from . import catalog,first_play
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


def calendar(conn,identity_id,source_timezone=None):
    """First explicit IANA calendar wins; imports before it are safely reprojected.

    A different later source calendar is retained on coarse evidence. A date in
    that calendar cannot be converted to an account date without a timestamp.
    """
    explicit = _zone(source_timezone)
    if source_timezone and explicit is None:
        logger.warning('Ignoring invalid listening timezone %r',source_timezone)
    row = conn.execute('SELECT timezone,confirmed FROM listening_calendars WHERE identity_id=?',
                       (identity_id,)).fetchone()
    if row is None:
        name = source_timezone if explicit else machine_timezone()
        conn.execute('INSERT INTO listening_calendars VALUES (?,?,?)',
                     (identity_id,name,int(explicit is not None)))
    elif not row['confirmed'] and explicit is not None:
        name = source_timezone
        conn.execute('UPDATE listening_calendars SET timezone=?,confirmed=1 WHERE identity_id=?',
                     (name,identity_id))
        for event in conn.execute('SELECT id,played_at FROM play_events WHERE identity_id=?',
                                  (identity_id,)).fetchall():
            project_event(conn,event['id'],identity_id,event['played_at'],timezone=name)
        conn.execute('UPDATE listening_observations SET day=CASE WHEN timezone=? THEN source_day END '
                     "WHERE identity_id=? AND precision='calendar_day'",
                     (name,identity_id))
        reconcile_foreign_days(conn,identity_id)
        first_play.invalidate(conn)
    else:
        name = row['timezone']
    return name


def project_event(conn,event_id,identity_id,timestamp,*,timezone=None,source=None):
    name = timezone or calendar(conn,identity_id)
    conn.execute('INSERT INTO play_event_calendar(event_id,day,timezone) VALUES (?,?,?) '
                 'ON CONFLICT(event_id) DO UPDATE SET day=excluded.day,timezone=excluded.timezone',
                 (event_id,day_at(timestamp,name),name))
    if source is not None:
        conn.execute('INSERT OR IGNORE INTO play_event_sources VALUES (?,?)',(event_id,source))


def observe_day(conn,identity_id,key,source_day,source,timezone,observed_at,*,reconcile=True):
    """Retain a coarse source contribution; daily projection deduplicates subjects."""
    dt.date.fromisoformat(source_day)
    name = calendar(conn,identity_id)
    subject = catalog.resolve_key(conn,key) or key
    recording = conn.execute('SELECT id FROM recordings WHERE identity_key=?',(subject,)).fetchone()
    day = source_day if timezone==name else None
    conn.execute(
        'INSERT INTO listening_observations(identity_id,raw_identity_key,identity_key,recording_id,'
        'source_day,day,source,timezone,precision,first_observed_at,last_observed_at) '
        "VALUES (?,?,?,?,?,?,?,?,'calendar_day',?,?) "
        'ON CONFLICT(identity_id,raw_identity_key,source_day,source,timezone) DO UPDATE SET '
        'first_observed_at=MIN(first_observed_at,excluded.first_observed_at),'
        'last_observed_at=MAX(last_observed_at,excluded.last_observed_at)',
        (identity_id,key,subject,recording[0] if recording else None,source_day,day,source,
         timezone,observed_at,observed_at))
    if day is None:
        scope = subject, source_day, timezone
        if reconcile:
            reconcile_foreign_days(conn,identity_id,observations={scope})
        return scope


def day_count(conn,identity_id):
    return conn.execute('SELECT COUNT(*) FROM listening_days WHERE identity_id=?',(identity_id,)).fetchone()[0]


def remove_source(conn,source,identity_id=None):
    """Remove only known contributions, preserving overlapping and unknown evidence."""
    if source in {'unknown','independent',''}:
        raise ValueError('Unknown legacy provenance cannot be assigned to a removable source')
    scope = '' if identity_id is None else ' AND identity_id=?'
    args = (source,) if identity_id is None else (source,identity_id)
    conn.execute('DELETE FROM listening_observations WHERE source=?'+scope,args)
    conn.execute('DELETE FROM play_event_sources WHERE source=? AND event_id IN '
                 '(SELECT id FROM play_events WHERE 1=1'+scope+')',args)
    conn.execute('DELETE FROM play_events WHERE NOT EXISTS '
                 '(SELECT 1 FROM play_event_sources s WHERE s.event_id=play_events.id)'+scope,
                 () if identity_id is None else (identity_id,))
    # The original scalar is a compatibility label, not the source of truth.
    conn.execute('UPDATE play_events SET source=(SELECT MIN(source) FROM play_event_sources s '
                 'WHERE s.event_id=play_events.id) WHERE source=?'+scope,args)
    for row in conn.execute('SELECT identity_id FROM listening_calendars WHERE 1=1'+scope,
                            () if identity_id is None else (identity_id,)).fetchall():
        reconcile_foreign_days(conn,row[0])
    first_play.invalidate(conn)
