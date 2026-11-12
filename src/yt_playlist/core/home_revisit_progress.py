"""One durable cursor for the independently completed Revisit summaries."""

FIELDS = ('rediscover', 'rediscover_albums', 'rediscover_tracks')
SQL = '''CREATE TABLE home_revisit_progress (
    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
    remaining INTEGER NOT NULL CHECK(typeof(remaining)='integer' AND remaining BETWEEN 0 AND 7))'''


def install(conn, context=None):
    conn.execute(SQL)


def validate(conn):
    from .schema_contract import MigrationError, tokens
    row = conn.execute("SELECT type,sql FROM main.sqlite_schema WHERE name='home_revisit_progress'").fetchone()
    if row is None or row[0] != 'table' or tokens(row[1]) != tokens(SQL):
        raise MigrationError('Incompatible Home Revisit progress')


def read(conn):
    row = conn.execute('SELECT remaining FROM home_revisit_progress WHERE singleton=1').fetchone()
    return [field for index, field in enumerate(FIELDS) if row[0] & (1 << index)] if row else None


def save(conn, remaining):
    if not conn.in_transaction:
        raise ValueError('Home Revisit progress requires its summary publication')
    if any(field not in FIELDS for field in remaining):
        raise ValueError('Unknown Home Revisit component')
    mask = sum(1 << index for index, field in enumerate(FIELDS) if field in remaining)
    conn.execute('INSERT INTO home_revisit_progress VALUES (1,?) '
                 'ON CONFLICT(singleton) DO UPDATE SET remaining=excluded.remaining', (mask,))
