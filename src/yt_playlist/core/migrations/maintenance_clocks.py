"""Move sync and enrichment worker stamps to their existing owners."""
from ..schema_contract import MigrationError, tokens
from ..schema_inspection import definitions


NAME = 'own_sync_and_enrichment_clocks'
OWNERS = ('sync_attempts', 'metadata_runtime_state')
ENRICHMENT_CLOCK = """enrichment_caught_up_at REAL CHECK(
    enrichment_caught_up_at IS NULL OR
    (typeof(enrichment_caught_up_at)='real' AND abs(enrichment_caught_up_at)<=1.7976931348623157e308))"""
PLAYBACK_CLOCK = """last_plays_sync_at REAL CHECK(
    last_plays_sync_at IS NULL OR
    (typeof(last_plays_sync_at)='real' AND abs(last_plays_sync_at)<=1.7976931348623157e308))"""
IMPORT_CHECKPOINT = """verified_imports_repaired TEXT CHECK(verified_imports_repaired IS NULL OR
    (length(verified_imports_repaired)=64 AND verified_imports_repaired NOT GLOB '*[^0-9a-f]*'))"""


def predecessor_objects(conn):
    schema = definitions(conn, OWNERS)
    return [schema[owner] for owner in OWNERS]


def install(conn, _context):
    conn.execute(f'ALTER TABLE metadata_runtime_state ADD COLUMN {ENRICHMENT_CLOCK}')
    conn.execute("""UPDATE metadata_runtime_state SET enrichment_caught_up_at=(
        SELECT nullif(value,'') FROM settings WHERE key='enrich_caught_up_at'
    ) WHERE id=1""")
    conn.execute(f'ALTER TABLE sync_attempts ADD COLUMN {PLAYBACK_CLOCK}')
    conn.execute(f'ALTER TABLE sync_attempts ADD COLUMN {IMPORT_CHECKPOINT}')
    conn.execute("""INSERT INTO sync_attempts(id,revision,phase,last_plays_sync_at,verified_imports_repaired)
        SELECT 1,1,'running',max(nullif(value,'')) FILTER(WHERE key='last_plays_sync_at'),
            max(nullif(value,'')) FILTER(WHERE key='repair_phantom_verified_import_seen')
        FROM settings WHERE key IN ('last_plays_sync_at','repair_phantom_verified_import_seen') HAVING count(*)>0
        ON CONFLICT(id) DO UPDATE SET last_plays_sync_at=excluded.last_plays_sync_at,
            verified_imports_repaired=excluded.verified_imports_repaired
    """)
    conn.execute("DELETE FROM settings WHERE key IN "
                 "('enrich_caught_up_at','last_plays_sync_at','repair_phantom_verified_import_seen')")


def validate(conn):
    schema = definitions(conn, (*OWNERS, 'sync_cursors'))
    sync = schema.get('sync_attempts', schema.get('sync_cursors'))
    if ('enrichment_caught_up_at' not in tokens(schema.get('metadata_runtime_state'))
            or not {'last_plays_sync_at', 'verified_imports_repaired'} <= set(tokens(sync))):
        raise MigrationError('Incomplete sync and enrichment clock storage')
