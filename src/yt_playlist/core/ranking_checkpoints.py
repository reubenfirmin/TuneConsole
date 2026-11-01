"""Recommendation and vibe preparation admission, independent of display cache."""
KEYS = {'rec_candidate_preferences':('candidate_refresh_checkpoint','preferences_fingerprint'),
        'rec_candidates_expire_at':('candidate_refresh_checkpoint','expires_at'),
        'rec_vibe_version':('vibe_preparation_checkpoint','policy_version')}


def available(conn):
    from .preparation import checkpoint_publication
    return (conn.execute("SELECT 1 FROM sqlite_schema WHERE name='candidate_refresh_checkpoint'").fetchone() is not None
            or checkpoint_publication.installed(conn, 'candidate-checkpoints'))


def get(conn,key,default=None):
    from .preparation import checkpoint_publication
    if key != 'rec_vibe_version':
        revision = checkpoint_publication.selected(conn, 'candidate-checkpoints')
        if revision is not None:
            return checkpoint_publication.read(conn, 'candidate-checkpoints', revision, key, default)
    if not conn.execute("SELECT 1 FROM main.sqlite_schema WHERE name='candidate_refresh_checkpoint'").fetchone():
        return checkpoint_publication.fallback(conn, key, default)
    table,column = KEYS[key]
    row = conn.execute(f'SELECT {column} FROM {table} WHERE singleton=1').fetchone()
    return str(row[0]) if row is not None and row[0] is not None else default


def put(conn,values):
    if not conn.execute("SELECT 1 FROM main.sqlite_schema WHERE name='candidate_refresh_checkpoint'").fetchone():
        from .preparation.control_publication import retained_write
        return retained_write(conn, values)
    for key,value in values.items():
        table,column = KEYS[key]
        value = (float(value) if value not in (None,'') else None) if column=='expires_at' else value
        conn.execute(f'INSERT INTO {table}(singleton,{column}) VALUES (1,?) '
                     f'ON CONFLICT(singleton) DO UPDATE SET {column}=excluded.{column}',(value,))
