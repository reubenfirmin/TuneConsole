"""Give each pool one ordered membership, including unresolved candidates."""
from ..schema_contract import MigrationError
from ..schema_inspection import definitions
from .track_source_cutover import _replace_table


NAME = 'own_recommendation_pool_membership'
TABLE = 'recommendation_candidates'
RETIRED = 'unresolved_recommendation_candidates'
MARKER = 'resolved_recommendation_candidates'
COMPLETE = 'complete_recommendation_pool'
OWNERS = (TABLE, RETIRED)
COLUMNS = 'pool_id,recording_id,rank,score,intensity_band,genre_key'


def predecessor_objects(conn):
    return [sql for sql, in conn.execute(
        'SELECT sql FROM sqlite_schema WHERE sql IS NOT NULL AND '
        '(tbl_name IN (?,?) OR name=?) ORDER BY type,name', (*OWNERS, COMPLETE))]


def install(conn, _context):
    original = definitions(conn, (TABLE,))[TABLE]
    shared = original[original.index('rank INTEGER'):original.index('PRIMARY KEY(pool_id,recording_id)')]
    table = f'''CREATE TABLE recommendation_candidates (
        pool_id INTEGER NOT NULL REFERENCES recommendation_pools(id),
        recording_id INTEGER REFERENCES recordings(id),
        {shared}
        raw_key TEXT CHECK(raw_key IS NULL OR length(raw_key)>0),
        CHECK((recording_id IS NULL)<>(raw_key IS NULL)),
        PRIMARY KEY(pool_id,rank),UNIQUE(pool_id,recording_id),UNIQUE(pool_id,raw_key)
    ) WITHOUT ROWID'''
    guards = conn.execute('SELECT name,sql FROM sqlite_schema WHERE tbl_name=? AND sql IS NOT NULL '
                          "AND type IN ('index','trigger') ORDER BY name", (TABLE,)).fetchall()
    conn.execute(f'DROP TRIGGER {COMPLETE}')
    _replace_table(conn, TABLE, table, expressions={'raw_key': 'NULL'})
    conn.execute(f'INSERT INTO {TABLE}(pool_id,raw_key,rank,score,intensity_band,genre_key) '
                 f'SELECT pool_id,raw_key,rank,score,intensity_band,genre_key FROM {RETIRED}')
    conn.execute(f'DROP TABLE {RETIRED}')
    for name, sql in guards:
        if not name.startswith('distinct_'):
            conn.execute(sql)
    conn.execute(f'CREATE VIEW {MARKER} AS SELECT {COLUMNS} FROM {TABLE} WHERE recording_id IS NOT NULL')
    conn.execute(f'''CREATE TRIGGER {COMPLETE} BEFORE UPDATE OF complete ON recommendation_pools
        WHEN OLD.complete=1 OR NEW.complete<>1 OR NEW.eligible_count<>(SELECT count(*) FROM {TABLE} WHERE pool_id=OLD.id)
            OR (NEW.eligible_count>0 AND (
                (SELECT min(rank) FROM {TABLE} WHERE pool_id=OLD.id)<>0 OR
                (SELECT max(rank) FROM {TABLE} WHERE pool_id=OLD.id)<>NEW.eligible_count-1))
        BEGIN SELECT RAISE(ABORT,'Recommendation pool membership is incomplete or sealed'); END''')


def validate(conn):
    if set(definitions(conn, (MARKER, RETIRED))) != {MARKER}:
        raise MigrationError('Retired recommendation membership storage remains')
