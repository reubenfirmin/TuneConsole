"""Names and selection of the retained listening projection family."""
from .preparation.listening_views import DERIVED

PREFIX = 'published_listening_subject_'
READS = ('play_event_subjects', 'listening_observation_subjects', *DERIVED,
         'recording_listening_summary', 'artist_listening_summary', 'trend_first_play')


def table(suffix):
    return PREFIX + suffix


def native(conn):
    return conn.execute("SELECT 1 FROM main.sqlite_schema WHERE name='listening_summary_state'").fetchone() is not None


def selected(conn):
    if native(conn) or not conn.execute('SELECT 1 FROM main.sqlite_schema WHERE name=?', (table('head'),)).fetchone():
        return None
    row = conn.execute('SELECT revision FROM '+table('head')+' WHERE active=1').fetchone()
    return row[0] if row else None
