"""Compute first-listening facts from the retained listening evidence."""
from . import listening_views


# The numbered and earlier regular migrations still install these contracts.
CATALOG_LINK_UPDATE = ('CREATE TRIGGER listening_summary_catalog_entries_update '
    'AFTER UPDATE OF id,recording_id,video_id,source_ref ON catalog_entries BEGIN '
    'INSERT INTO listening_summary_state(id,input_revision) VALUES (1,1) '
    'ON CONFLICT(id) DO UPDATE SET input_revision=input_revision+1; END')
RECORDING_IDENTITY_UPDATE = ('CREATE TRIGGER listening_summary_recordings_update '
    'AFTER UPDATE OF id,identity_key ON recordings WHEN OLD.id IS NOT NEW.id OR OLD.identity_key IS NOT NEW.identity_key '
    'BEGIN INSERT INTO listening_summary_state(id,input_revision) VALUES (1,1) '
    'ON CONFLICT(id) DO UPDATE SET input_revision=input_revision+1; END')


def available(conn):
    """Distinguish the supported recording-owned ledger from old migration input."""
    return conn.execute("SELECT 1 FROM main.sqlite_schema WHERE name='play_event_subjects'").fetchone() is not None


def definitions():
    yield 'recording_listening_summary', '''WITH evidence AS (
        SELECT account_id,recording_id,day,observed_at,precision,
            row_number() OVER (PARTITION BY account_id,recording_id,precision ORDER BY observed_at) position
        FROM eligible_listening_evidence WHERE recording_id IS NOT NULL
    )
    SELECT account_id,recording_id,
        min(observed_at) FILTER (WHERE precision='exact_timestamp') first_exact_play_at,
        max(day) FILTER (WHERE precision='exact_timestamp' AND position=1) first_exact_day,
        min(day) FILTER (WHERE precision='calendar_day') first_observed_day,
        min(day) FILTER (WHERE precision='legacy_unknown') first_legacy_unknown_day
    FROM evidence GROUP BY account_id,recording_id'''

    artist = '''WITH ranked AS (
        SELECT s.*,c.artist_id,row_number() OVER (PARTITION BY s.account_id,c.artist_id
            ORDER BY s.first_exact_play_at IS NULL,s.first_exact_play_at,s.recording_id) position
        FROM recording_listening_summary s JOIN recording_listening_artists c USING(recording_id)
    )
    SELECT account_id,artist_id,min(first_exact_play_at) first_exact_play_at,
        max(first_exact_day) FILTER (WHERE position=1) first_exact_day,
        min(first_observed_day) first_observed_day,min(first_legacy_unknown_day) first_legacy_unknown_day
    FROM ranked GROUP BY account_id,artist_id'''
    yield 'artist_listening_summary', artist
    yield 'trend_first_play', ('WITH recording_facts AS MATERIALIZED (SELECT * FROM recording_listening_summary), '
        'artist_facts AS MATERIALIZED ('+artist.replace('FROM recording_listening_summary', 'FROM recording_facts')+') '
        +listening_views.first_play(lambda name: {'recording_listening_summary': 'recording_facts',
            'artist_listening_summary': 'artist_facts'}.get(name, name)))
