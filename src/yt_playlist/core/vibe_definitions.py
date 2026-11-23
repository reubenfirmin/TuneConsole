"""Stable Vibe score identities and their foreign keys."""
from .schema_contract import MigrationError, table_clauses, tokens
from .schema_inspection import definitions


TABLE = 'vibe_definitions'
TABLE_SQL = ("CREATE TABLE vibe_definitions (vibe TEXT PRIMARY KEY NOT NULL, "
             "kind TEXT NOT NULL CHECK(kind IN ('specialist','intensity','retired'))) WITHOUT ROWID")
JOINT_HEAD = """joint_head TEXT CHECK(joint_head IS NULL OR joint_head='' OR
    (length(joint_head)=64 AND joint_head NOT GLOB '*[^0-9a-f]*'))"""
JOINT_OUTCOME = "joint_outcome TEXT CHECK(joint_outcome IS NULL OR json_type(joint_outcome) IS 'object')"
SCORES = ('measurements', 'predictions', 'recording_vibes')
REFERENCE = ('vibe_definition TEXT GENERATED ALWAYS AS (vibe) VIRTUAL '
             'REFERENCES vibe_definitions(vibe)')
IDENTITIES = (
    ('concentrate', 'specialist'), ('cruise', 'specialist'), ('energize', 'specialist'),
    ('relax', 'specialist'), ('release', 'specialist'), ('uplift', 'specialist'),
    ('specialist_activation', 'intensity'), ('specialist_force', 'intensity'),
    ('shared_energize', 'retired'), ('shared_release', 'retired'),
)


def install(conn, _context):
    conn.execute(TABLE_SQL)
    conn.executemany('INSERT INTO vibe_definitions(vibe,kind) VALUES (?,?)', IDENTITIES)
    # A virtual column adds an enforced key without copying any historical
    # rows or changing the ranking and original-score keys on vibe.
    for table in SCORES:
        conn.execute(f'ALTER TABLE {table} ADD COLUMN {REFERENCE}')


def validate(conn):
    observed = definitions(conn, (TABLE, *SCORES))
    with_heads = TABLE_SQL.removesuffix(') WITHOUT ROWID') + f', {JOINT_HEAD}, {JOINT_OUTCOME}) WITHOUT ROWID'
    if tokens(observed.get(TABLE)) not in (tokens(TABLE_SQL), tokens(with_heads)):
        raise MigrationError('Incompatible Vibe score definitions')
    if set(map(tuple, conn.execute('SELECT vibe,kind FROM vibe_definitions'))) != set(IDENTITIES):
        raise MigrationError('Unexpected Vibe score identities')
    key = tokens(REFERENCE)
    for table in SCORES:
        if table not in observed or key not in table_clauses(tokens(observed[table])):
            raise MigrationError(f'Missing Vibe definition foreign key: {table}')
