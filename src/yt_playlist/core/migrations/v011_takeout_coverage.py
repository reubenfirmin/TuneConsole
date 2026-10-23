"""Keep import coverage separate from event timestamps and preserve unknown observation sources."""
from .runner import MigrationError


IMPORTS_SQL = """CREATE TABLE takeout_imports (
    id INTEGER PRIMARY KEY,
    identity_id INTEGER NOT NULL REFERENCES identities(id),
    fingerprint TEXT NOT NULL,
    imported_at REAL NOT NULL,
    observed_start REAL,
    observed_end REAL,
    unparsed_dates INTEGER NOT NULL CHECK(unparsed_dates >= 0),
    account_verified INTEGER NOT NULL DEFAULT 0 CHECK(account_verified IN (0,1)),
    complete INTEGER NOT NULL DEFAULT 0 CHECK(complete IN (0,1)),
    coverage_start REAL,
    coverage_end REAL,
    CHECK(coverage_start IS NULL OR coverage_end > coverage_start)
)"""


def apply(conn, context):
    conn.execute(IMPORTS_SQL)
    # Existing rows have no reliable provenance. Never invent a source for them.
    conn.execute("ALTER TABLE history_items ADD COLUMN source TEXT NOT NULL DEFAULT 'unknown'")


def validate(conn):
    row = conn.execute("SELECT sql FROM sqlite_master WHERE name='takeout_imports'").fetchone()
    if not row or row[0] != IMPORTS_SQL:
        raise MigrationError('Incompatible Takeout import coverage table')
    columns = {r[1]: r for r in conn.execute('PRAGMA table_info(history_items)')}
    if 'source' not in columns or columns['source'][2:5] != ('TEXT', 1, "'unknown'"):
        raise MigrationError('Missing history observation provenance')
