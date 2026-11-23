"""Keep Discovery's candidate details on their artist owner."""
from .. import collection_recovery_epoch, domain_contracts
from ..discovery_candidates import ARTIST_FIELDS, ARTIST_INDEX
from ..schema_contract import MigrationError, tokens
from ..schema_inspection import definitions


NAME = 'fold_discovery_artist_candidates'
VIEW = ('CREATE VIEW discovered_artists AS SELECT id AS artist_id,'
        + ','.join(f'discovery_{field} AS {field}' for field in ARTIST_FIELDS)
        + ' FROM artists WHERE discovery_candidate=1')
INDEX = f'CREATE INDEX {ARTIST_INDEX} ON artists(discovery_genre_key) WHERE discovery_candidate=1'
COLUMNS = {
    'candidate': 'INTEGER NOT NULL DEFAULT 0 CHECK(discovery_candidate IN (0,1))',
    'score': 'REAL',
    'thumbnail': 'TEXT',
    'found_at': 'REAL',
    'last_shown': 'REAL',
    'first_shown': 'REAL',
    'offered_count': 'INTEGER NOT NULL DEFAULT 0',
    'unresolved_genre': 'TEXT',
    'genre_key': 'TEXT REFERENCES genre_catalog(key) CHECK(discovery_genre_key IS NULL OR discovery_unresolved_genre IS NULL)',
    'recovery_filename': 'TEXT',
    'recovery_digest': 'TEXT',
    'recovery_bytes': '''INTEGER CHECK(
        (discovery_recovery_filename IS NULL AND discovery_recovery_digest IS NULL AND discovery_recovery_bytes IS NULL)
        OR (discovery_recovery_filename IS NOT NULL AND discovery_recovery_digest IS NOT NULL
            AND discovery_recovery_bytes IS NOT NULL AND length(discovery_recovery_digest)=64
            AND discovery_recovery_bytes>0))''',
}


def predecessor_objects(conn):
    return [sql for sql, in conn.execute('''
        SELECT sql FROM sqlite_schema WHERE sql IS NOT NULL
        AND (tbl_name='discovered_artists' OR name='artists') ORDER BY type,name
    ''')]


def install(conn, _context):
    checks = domain_contracts.CONTRACTS['discovered_artists']
    for field, declaration in COLUMNS.items():
        column = 'discovery_' + field
        check = ' CHECK(' + checks[field].replace(field, column) + ')' if field in checks else ''
        conn.execute(f'ALTER TABLE artists ADD COLUMN {column} {declaration}{check}')
    values = ','.join(f'discovery_{field}=d.{field}' for field in ARTIST_FIELDS)
    conn.execute(f'UPDATE artists SET discovery_candidate=1,{values} '
                 'FROM discovered_artists d WHERE artists.id=d.artist_id')
    conn.execute('DROP TABLE discovered_artists')
    conn.execute(VIEW)
    conn.execute(INDEX)
    collection_recovery_epoch.install(conn)
    collection_recovery_epoch.install_keys(conn)


def validate(conn):
    actual = definitions(conn, ('discovered_artists', ARTIST_INDEX))
    if tokens(actual.get('discovered_artists')) != tokens(VIEW) or tokens(actual.get(ARTIST_INDEX)) != tokens(INDEX):
        raise MigrationError('Artist Discovery candidate storage was not folded')
