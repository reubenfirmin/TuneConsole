"""Versioned multiple-label judgments; preserve the original six-preset feedback."""


def statements():
    yield ("CREATE TABLE audio_vibe_labels (run_id TEXT PRIMARY KEY NOT NULL,"
           "taxonomy_version TEXT NOT NULL,sample_ids TEXT NOT NULL,prediction TEXT NOT NULL,"
           "vibes TEXT NOT NULL,updated_at REAL NOT NULL)")


def apply(conn, context):
    for sql in statements():
        conn.execute(sql)


def validate(conn):
    from .current import _tokens
    from .runner import MigrationError
    for sql in statements():
        name = sql.split()[2]
        row = conn.execute('SELECT sql FROM sqlite_master WHERE name=?', (name,)).fetchone()
        if row is None or _tokens(row[0]) != _tokens(sql):
            raise MigrationError(f'Incompatible audio vibe label structure: {name}')
