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
    from ..schema_inspection import objects
    definitions = {name: sql for name, (_kind, sql) in objects(conn).items()}
    for sql in statements():
        actual = definitions.get(sql.split()[2])
        if actual is None or _tokens(actual) != _tokens(sql):
            raise MigrationError('Incompatible audio playback run structure')
