"""Keep each recipe's optional draft and ordered selections on its recipe row."""
from .. import collection_recovery_epoch
from ..schema_contract import MigrationError
from ..schema_inspection import objects


NAME = 'fold_road_trip_draft_storage'
MARKER = 'uq_road_trip_draft_id'
RETIRED = ('road_trip_drafts', 'road_trip_draft_entries')
COLUMNS = {
    'draft_id': 'TEXT',
    'draft_account_id': 'INTEGER REFERENCES accounts(id)',
    'draft_revision': 'TEXT CHECK(length(draft_revision)>0)',
    'draft_state': "TEXT CHECK(draft_state IN ('building','ready','saved','failed'))",
    'draft_updated_at': 'REAL CHECK(abs(draft_updated_at)<=1.7976931348623157e308)',
    'recovery_filename': 'TEXT',
    'recovery_digest': 'TEXT CHECK(length(recovery_digest)=64)',
    'recovery_bytes': 'INTEGER CHECK(typeof(recovery_bytes)=\'integer\' AND recovery_bytes BETWEEN 1 AND 16777216'
                      ' OR recovery_bytes IS NULL)',
    'draft_selections': """TEXT CHECK(
        (draft_id IS NULL AND draft_account_id IS NULL AND draft_revision IS NULL
         AND draft_state IS NULL AND draft_updated_at IS NULL AND recovery_filename IS NULL
         AND recovery_digest IS NULL AND recovery_bytes IS NULL AND draft_selections IS NULL)
        OR
        (draft_id IS NOT NULL AND draft_revision IS NOT NULL AND draft_state IS NOT NULL
         AND draft_updated_at IS NOT NULL AND recovery_filename IS NOT NULL AND recovery_digest IS NOT NULL
         AND recovery_bytes IS NOT NULL AND draft_selections IS NOT NULL AND json_type(draft_selections)='array')
    )""",
}


def predecessor_objects(conn):
    return [sql for sql, in conn.execute(
        "SELECT sql FROM sqlite_schema WHERE tbl_name IN ('road_trip_recipes',?,?) "
        "AND sql IS NOT NULL ORDER BY type,name", RETIRED)]


def install(conn, _context):
    for name, declaration in COLUMNS.items():
        conn.execute(f'ALTER TABLE road_trip_recipes ADD COLUMN {name} {declaration}')
    fields = ','.join(COLUMNS)
    values = """d.id,d.account_id,d.revision,d.state,d.updated_at,
        d.recovery_filename,d.recovery_digest,d.recovery_bytes,
        (SELECT json_group_array(json_array(e.selection_state,e.video_id) ORDER BY e.selection_state,e.position)
         FROM road_trip_draft_entries e WHERE e.draft_id=d.id)"""
    conn.execute(f'UPDATE road_trip_recipes AS r SET ({fields})=({values}) '
                 'FROM road_trip_drafts AS d WHERE d.recipe_id=r.id')
    conn.execute(f'INSERT INTO road_trip_recipes(name,own_pct,target_minutes,created_at,updated_at,{fields}) '
                 f"SELECT 'Recovered Road Trip',50,60,d.updated_at,d.updated_at,{values} FROM road_trip_drafts d "
                 'WHERE d.recipe_id IS NULL ORDER BY d.updated_at,d.id')
    conn.execute(f'CREATE UNIQUE INDEX {MARKER} ON road_trip_recipes(draft_id) WHERE draft_id IS NOT NULL')
    conn.execute('DROP TABLE road_trip_draft_entries')
    conn.execute('DROP TABLE road_trip_drafts')
    collection_recovery_epoch.install(conn)
    collection_recovery_epoch.install_keys(conn)


def validate(conn):
    installed = objects(conn, (MARKER, *RETIRED))
    if set(installed) != {MARKER}:
        raise MigrationError('Road Trip drafts were not folded into recipes')
