"""Keep listening judgments independent of successful audio analysis."""


def statements():
    yield ('CREATE TABLE audio_vibe_runs (run_id TEXT PRIMARY KEY NOT NULL,'
           'video_id TEXT NOT NULL,title TEXT NOT NULL,artist TEXT NOT NULL,'
           'created_at REAL NOT NULL)')


def apply(conn, context):
    for sql in statements():
        conn.execute(sql)


def validate(conn):
    from .current import _tokens
    from .runner import MigrationError
    for sql in statements():
        row = conn.execute('SELECT sql FROM sqlite_master WHERE name=?', (sql.split()[2],)).fetchone()
        if row is None or _tokens(row[0]) != _tokens(sql):
            raise MigrationError('Incompatible audio playback run structure')
