"""Queue position and cursor on the musical source that owns the work."""

NEXT_POSITION = '(SELECT coalesce(max(last_queue_position),0)+1 FROM rec_musical_sources)'
ENQUEUE = f'''UPDATE rec_musical_sources
    SET last_queue_position=CASE WHEN pending_position IS NULL THEN {NEXT_POSITION} ELSE last_queue_position END,
        pending_position=coalesce(pending_position,{NEXT_POSITION}), pending_cursor=0
    WHERE source_key='''


def enqueue(conn, key):
    conn.execute(ENQUEUE + '? AND pending_position IS NULL', (key,))
