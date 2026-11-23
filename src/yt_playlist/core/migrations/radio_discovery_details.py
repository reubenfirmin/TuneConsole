"""Keep Radio references and discovered-source fetches with their owners."""
from .. import discovery_checkpoints, radio_provenance
from ..schema_contract import MigrationError
from ..schema_inspection import definitions


NAME = 'inline_radio_seeds_origins_and_discovery_fetches'
RETIRED = ('radio_seed_subjects', 'radio_playlist_origins', 'discovery_fetch_history')
COLUMNS = {
    'radio_seed_output': "seed_subjects TEXT NOT NULL DEFAULT '[]' "
                         "CHECK(json_type(seed_subjects) IS 'array' AND json_array_length(seed_subjects)<=1000)",
    'playlist_scopes': 'radio_origin INTEGER NOT NULL DEFAULT 0 CHECK(radio_origin IN (0,1))',
    'discovery_origins': "fetch_sources TEXT NOT NULL DEFAULT '[]' CHECK(json_type(fetch_sources) IS 'array')",
}


def predecessor_objects(conn):
    return [sql for sql, in conn.execute('SELECT sql FROM sqlite_schema WHERE tbl_name IN (?,?,?,?,?,?) '
        'AND sql IS NOT NULL ORDER BY type,name', (*COLUMNS, *RETIRED))]


def _guards(conn):
    for suffix, event in (('insert', 'INSERT'), ('update', 'UPDATE OF seed_subjects')):
        conn.execute(f'''CREATE TRIGGER radio_seed_subjects_{suffix} BEFORE {event} ON radio_seed_output
            WHEN EXISTS(SELECT 1 FROM json_each(NEW.seed_subjects) s
                WHERE length(coalesce(s.value->>'video_id',''))=0
                OR CASE WHEN s.value->>'entry_id' IS NULL THEN s.value->>'resolution_reason' IS NOT 'missing_entry'
                    ELSE s.value->>'resolution_reason' IS NOT NULL OR
                        NOT EXISTS(SELECT 1 FROM track_sources t
                            WHERE t.id=s.value->>'entry_id' AND t.video_id=s.value->>'video_id') END)
            BEGIN SELECT RAISE(ABORT,'Radio seed does not match its catalog entry'); END''')
    for suffix, event, changed in (('delete', 'DELETE', ''), ('rekey', 'UPDATE OF id', 'NEW.id IS NOT OLD.id AND ')):
        conn.execute(f'''CREATE TRIGGER radio_seed_retains_entry_{suffix} BEFORE {event} ON track_sources
            WHEN {changed}EXISTS(SELECT 1 FROM radio_seed_output o,json_each(o.seed_subjects) s
                WHERE s.value->>'entry_id'=OLD.id)
            BEGIN SELECT RAISE(ABORT,'A saved Radio seed retains this entry'); END''')
        conn.execute(f'''CREATE TRIGGER radio_retains_playlist_scope_{suffix} BEFORE {event} ON playlist_scopes
            WHEN {changed}OLD.radio_origin=1
            BEGIN SELECT RAISE(ABORT,'Radio retains this playlist scope'); END''')


def install(conn, _context):
    for owner, column in COLUMNS.items():
        conn.execute(f'ALTER TABLE {owner} ADD COLUMN {column}')
    conn.execute('''UPDATE radio_seed_output AS o SET seed_subjects=(
        SELECT json_group_array(json_object('position',position,'video_id',video_id,'entry_id',entry_id,
            'resolution_reason',resolution_reason) ORDER BY position) FROM radio_seed_subjects WHERE offer_id=o.id)''')
    conn.execute('UPDATE playlist_scopes SET radio_origin=1 WHERE id IN (SELECT playlist_scope_id FROM radio_playlist_origins)')
    conn.execute('''INSERT INTO settings(key,value)
        SELECT ?,json_group_array(unresolved_provider_id ORDER BY unresolved_provider_id) FROM radio_playlist_origins
        WHERE unresolved_provider_id IS NOT NULL HAVING count(*)>0''', (radio_provenance.UNRESOLVED,))
    for kind, identifier, album in conn.execute('SELECT * FROM discovery_fetch_history ORDER BY source_type,source_id'):
        discovery_checkpoints.mark(conn, kind, identifier, album=album)
    for table in RETIRED:
        conn.execute(f'DROP TABLE {table}')
    _guards(conn)


def validate(conn, *, seed_setting=False):
    columns = {name: column for name, column in COLUMNS.items() if not seed_setting or name != 'radio_seed_output'}
    installed = definitions(conn, (*columns, *RETIRED))
    if (any(table in installed for table in RETIRED)
            or any(column not in installed.get(owner, '') for owner, column in columns.items())):
        raise MigrationError('Incompatible inline Radio or discovery references')
