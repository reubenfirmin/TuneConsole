"""First-play index invalidation and retained pre-summary storage support.

#620 retires the predecessor path once those installed layouts are converted.
These builds run in maintenance, never during schema admission or startup.
"""


def rebuild_predecessor(conn, *, private=False):
    """Fold retained eligible dates into the predecessor's discardable index.

    Keep canonical artist IDs and earlier coarse dates. A coarse timestamp is
    the date's noon marker, as in listening_days; it is not an exact play claim.
    """
    from .preparation import subject_publication
    if subject_publication.selected(conn) is not None:
        # Canonical summaries keep coarse dates separate from exact timestamps.
        # The retained predecessor cache still requires a timestamp: its noon
        # markers stay confined to that cache, never to the selected projection.
        rows = [(kind, key, day, at if at is not None else day*86400+43200, source)
            for kind, key, day, at, source in conn.execute(
                'SELECT * FROM main.'+subject_publication.table('read_trend_first_play'))]
        prepared = {'predecessor_first_play':rows}
        if not private:
            publish_predecessor(conn, prepared)
        return prepared
    table = 'temp.trend_first_play' if private else 'main.trend_first_play'
    if private:
        conn.execute('DROP VIEW IF EXISTS temp.trend_first_play')
        conn.execute('CREATE TEMP TABLE trend_first_play AS SELECT * FROM main.trend_first_play WHERE 0')
    conn.execute('DELETE FROM ' + table)
    conn.execute('INSERT INTO ' + table + '''
        WITH evidence AS (
          SELECT coalesce(r.identity_key,e.identity_key) identity_key,r.id recording_id,e.day,
            CASE WHEN e.precision='exact_timestamp' THEN e.observed_at
                 ELSE CAST(strftime('%s',e.day||' 12:00:00') AS REAL) END at
          FROM eligible_listening_evidence e LEFT JOIN recordings r ON r.id=e.recording_id
          WHERE e.day IS NOT NULL),
        subjects AS (
          SELECT 'track' kind,identity_key id_key,day,at FROM evidence
          UNION ALL
          SELECT 'artist','@artist:'||a.artist_id,e.day,e.at FROM evidence e
          JOIN recording_listening_artists a USING(recording_id))
        SELECT kind,id_key,CAST(julianday(min(day))-julianday('1970-01-01') AS INTEGER),
          min(at),'canonical_listening' FROM subjects GROUP BY kind,id_key''')
    return {'predecessor_first_play': list(map(tuple, conn.execute('SELECT * FROM ' + table)))}


def publish_predecessor(conn, prepared):
    """Publish only prepared rows, inside the snapshot's revision-checked transaction."""
    conn.execute('DELETE FROM main.trend_first_play')
    conn.executemany('INSERT INTO main.trend_first_play VALUES (?,?,?,?,?)',
                     prepared['predecessor_first_play'])


def invalidate(conn):
    from . import listening_summaries
    if listening_summaries.available(conn):
        listening_summaries.invalidate(conn)
        return
    conn.execute("DELETE FROM main.trend_first_play")
    conn.execute("DELETE FROM settings WHERE key IN "
                 "('trend_first_play_watermark','trend_first_play_source')")
