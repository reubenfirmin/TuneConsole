"""Discardable account/subject summaries, rebuilt from retained precision evidence."""
import time


TABLES = ('recording_listening_summary','artist_listening_summary')


def available(conn):
    return conn.execute("SELECT 1 FROM sqlite_schema WHERE name='listening_summary_state'").fetchone() is not None


def invalidate(conn):
    conn.execute('INSERT INTO listening_summary_state(id,input_revision) VALUES (1,1) '
                 'ON CONFLICT(id) DO UPDATE SET input_revision=input_revision+1,built_revision=NULL')


def rebuild(conn, *, private=False):
    """Caller owns the transaction. Private builds shadow only this reader's output."""
    if private:
        # TEMP foreign keys can only reference TEMP owners. These are private
        # calculation buffers; publication enforces the real tables' constraints.
        for table in TABLES:
            conn.execute('CREATE TEMP TABLE '+table+' AS SELECT * FROM main.'+table+' WHERE 0')
        sql = conn.execute("SELECT sql FROM main.sqlite_schema WHERE name='listening_summary_state'").fetchone()[0]
        conn.execute(sql.replace('CREATE TABLE','CREATE TEMP TABLE',1))
        conn.execute('INSERT INTO temp.listening_summary_state SELECT * FROM main.listening_summary_state')
        sql = conn.execute("SELECT sql FROM main.sqlite_schema WHERE name='trend_first_play'").fetchone()[0]
        conn.execute(sql.replace('CREATE VIEW','CREATE TEMP VIEW',1))
    revision = conn.execute('SELECT input_revision FROM listening_summary_state WHERE id=1').fetchone()
    revision = revision[0] if revision else 0
    for table in TABLES:
        conn.execute('DELETE FROM '+table)
    conn.execute('''INSERT INTO recording_listening_summary
        WITH exact AS (
          SELECT p.account_id,p.recording_id,p.played_at,c.day,
            row_number() OVER (PARTITION BY p.account_id,p.recording_id ORDER BY p.played_at,p.id) position
          FROM play_events p JOIN play_event_calendar c ON c.event_id=p.id
          WHERE p.recording_id IS NOT NULL AND coalesce(p.is_generated,0)=0
            AND EXISTS(SELECT 1 FROM play_event_sources s WHERE s.event_id=p.id)),
        coarse AS (
          SELECT account_id,recording_id,
            min(CASE WHEN precision='calendar_day' THEN day END) observed_day,
            min(CASE WHEN precision='legacy_unknown' THEN day END) legacy_day
          FROM eligible_listening_evidence WHERE recording_id IS NOT NULL
          GROUP BY account_id,recording_id),
        subjects AS (SELECT account_id,recording_id FROM exact UNION SELECT account_id,recording_id FROM coarse)
        SELECT s.account_id,s.recording_id,e.played_at,e.day,c.observed_day,c.legacy_day,?
        FROM subjects s LEFT JOIN exact e ON e.account_id=s.account_id AND e.recording_id=s.recording_id AND e.position=1
        LEFT JOIN coarse c ON c.account_id=s.account_id AND c.recording_id=s.recording_id''',(revision,))
    conn.execute('''INSERT INTO artist_listening_summary
        WITH ranked AS (
          SELECT s.*,c.artist_id,row_number() OVER (PARTITION BY s.account_id,c.artist_id
            ORDER BY s.first_exact_play_at IS NULL,s.first_exact_play_at,s.recording_id) position
          FROM recording_listening_summary s JOIN recording_listening_artists c USING(recording_id))
        SELECT account_id,artist_id,min(first_exact_play_at),
          max(CASE WHEN position=1 THEN first_exact_day END),
          min(first_observed_day),min(first_legacy_unknown_day),?
        FROM ranked GROUP BY account_id,artist_id''',(revision,))
    conn.execute('INSERT INTO listening_summary_state(id,input_revision,built_revision,built_at) VALUES (1,?,?,?) '
                 'ON CONFLICT(id) DO UPDATE SET built_revision=excluded.built_revision,built_at=excluded.built_at',
                 (revision,revision,time.time()))
    return {'revision':revision,**{table:list(map(tuple,conn.execute('SELECT * FROM '+table))) for table in TABLES}}


def publish(conn, prepared):
    revision = conn.execute('SELECT input_revision FROM listening_summary_state WHERE id=1').fetchone()
    if (revision[0] if revision else 0)!=prepared['revision']:
        raise ValueError('Listening evidence changed during summary preparation')
    for table in TABLES:
        conn.execute('DELETE FROM '+table)
        conn.executemany('INSERT INTO '+table+' VALUES (?,?,?,?,?,?,?)',prepared[table])
    conn.execute('INSERT INTO listening_summary_state(id,input_revision,built_revision,built_at) VALUES (1,?,?,?) '
                 'ON CONFLICT(id) DO UPDATE SET built_revision=excluded.built_revision,built_at=excluded.built_at',
                 (prepared['revision'],prepared['revision'],time.time()))
