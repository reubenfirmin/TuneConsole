"""Selection of the retained bounded calendar lookup indexes."""
EVENTS = 'prepared_calendar_event_lookup'
OBSERVATIONS = 'prepared_calendar_observation_lookup'
SELECTION = 'published_calendar_lookup'
CUSTODY = 'prepared_calendar_lookup_custody'


def selected(conn):
    return (conn.execute('SELECT 1 FROM main.sqlite_schema WHERE type=\'table\' AND name=?', (SELECTION,)).fetchone() is not None
            and conn.execute('SELECT 1 FROM main.sqlite_schema WHERE type=\'table\' AND name=?', (CUSTODY,)).fetchone() is not None
            and conn.execute(f'SELECT 1 FROM main.{CUSTODY} WHERE id=1 AND verified=1').fetchone() is not None
            and conn.execute(f'SELECT 1 FROM main.{SELECTION} WHERE id=1').fetchone() is not None)
