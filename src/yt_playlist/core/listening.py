"""Reconcile listening evidence inside the caller's database transaction."""
import datetime as dt
import logging
import os
from pathlib import Path
from zoneinfo import ZoneInfo,ZoneInfoNotFoundError

from . import catalog,first_play

logger = logging.getLogger(__name__)


def _zone(name):
    try:
        return ZoneInfo(name) if name else None
    except (ZoneInfoNotFoundError,ValueError,TypeError):
        return None


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
        conn.execute('UPDATE listening_observations SET day=CASE WHEN timezone=? THEN source_day END WHERE identity_id=?',
                     (name,identity_id))
        reconcile_foreign_days(conn,identity_id)
        first_play.invalidate(conn)
    else:
        name = row['timezone']
    return name


def day_at(timestamp,timezone):
    return dt.datetime.fromtimestamp(timestamp,_zone(timezone)).date().isoformat()


def project_event(conn,event_id,identity_id,timestamp,*,timezone=None,source=None):
    name = timezone or calendar(conn,identity_id)
    conn.execute('INSERT INTO play_event_calendar(event_id,day,timezone) VALUES (?,?,?) '
                 'ON CONFLICT(event_id) DO UPDATE SET day=excluded.day,timezone=excluded.timezone',
                 (event_id,day_at(timestamp,name),name))
    if source is not None:
        conn.execute('INSERT OR IGNORE INTO play_event_sources VALUES (?,?)',(event_id,source))


def observe_day(conn,identity_id,key,source_day,source,timezone,observed_at):
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
        reconcile_foreign_days(conn,identity_id)


def reconcile_foreign_days(conn,identity_id):
    """An exact event can anchor a foreign-calendar date; ambiguity remains unknown."""
    rows = conn.execute('SELECT o.* FROM listening_observations o JOIN listening_calendars c '
                        'ON c.identity_id=o.identity_id WHERE o.identity_id=? AND o.timezone<>c.timezone',
                        (identity_id,)).fetchall()
    for row in rows:
        matches = {
            event['day'] for event in conn.execute(
                'SELECT pe.played_at,c.day FROM play_events pe JOIN play_event_calendar c ON c.event_id=pe.id '
                'WHERE pe.identity_id=? AND pe.identity_key=?',(identity_id,row['identity_key']))
            if day_at(event['played_at'],row['timezone'])==row['source_day']
        }
        # A later event can reveal that an earlier match was ambiguous. Do not
        # leave an old projection behind when the same source date spans two days.
        day = matches.pop() if len(matches)==1 else None
        conn.execute('UPDATE listening_observations SET day=? WHERE id=?',(day,row['id']))


def day_count(conn,identity_id):
    return conn.execute('SELECT COUNT(*) FROM listening_days WHERE identity_id=?',(identity_id,)).fetchone()[0]


def remove_source(conn,source,identity_id=None):
    """Remove only known contributions, preserving overlapping and unknown evidence."""
    if source in {'unknown','independent',''}:
        raise ValueError('Unknown legacy provenance cannot be assigned to a removable source')
    scope = '' if identity_id is None else ' AND identity_id=?'
    args = (source,) if identity_id is None else (source,identity_id)
    conn.execute('DELETE FROM listening_observations WHERE source=?'+scope,args)
    conn.execute('DELETE FROM history_items WHERE source=? AND snapshot_id IN '
                 '(SELECT id FROM history_snapshots WHERE 1=1'+scope+')',args)
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
