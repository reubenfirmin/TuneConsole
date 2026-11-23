"""Move the legacy content-mode rows and their playlist reference to taste groups."""
from .. import legacy_mode_payloads as modes, mode_retention
from ..domain_contracts import finite, flag, integer
from ..schema_contract import MigrationError
from ..schema_inspection import definitions
from . import account_storage, control_settings_storage, track_source_cutover


NAME = 'fold_legacy_mode_payloads_into_groups'
RETIRED = 'taste_modes'
PREDECESSOR_KEY = 'legacy_mode_storage_predecessor_statements'
ALLOCATION = control_settings_storage.ALLOCATION_TRIGGERS[1:]
PLAYLIST_REFERENCE = 'REFERENCES taste_modes(mode_id)'
GROUP_REFERENCE = 'REFERENCES taste_groups(legacy_mode_id)'
PICK_GUARDS = ('playlist_legacy_mode_pick_insert', 'playlist_legacy_mode_pick_update')


def predecessor_objects(conn):
    return [sql for sql, in conn.execute('SELECT sql FROM sqlite_schema WHERE sql IS NOT NULL '
        "AND (tbl_name IN ('taste_modes','taste_groups') OR name='playlists') ORDER BY type,name")]


def _guards(conn):
    for operation, identities in (('INSERT', 'coalesce(NEW.legacy_mode_id,0)'),
                                  ('DELETE', 'coalesce(OLD.legacy_mode_id,0)'),
                                  ('UPDATE OF legacy_mode_id', 'coalesce(OLD.legacy_mode_id,0),coalesce(NEW.legacy_mode_id,0)')):
        name = 'allocated_taste_mode_' + operation.split()[0].lower()
        conn.execute(f'''CREATE TRIGGER {name} BEFORE {operation} ON taste_groups
            BEGIN UPDATE settings SET value=max(CAST(value AS INTEGER),{identities})
            WHERE key='{mode_retention.HIGH_WATER}'
            AND max(CAST(value AS INTEGER),{identities})>CAST(value AS INTEGER); END''')
    conn.execute('''CREATE TRIGGER complete_taste_mode_reactivation BEFORE UPDATE ON taste_groups
        WHEN NEW.mode_active=1 AND length(NEW.mode_centroid)=0 AND OLD.mode_payload_retired_at IS NOT NULL
        BEGIN SELECT RAISE(ABORT,'Retired mode reactivation requires a new payload'); END''')
    conn.execute('''CREATE TRIGGER published_taste_mode_reactivation AFTER UPDATE ON taste_groups
        WHEN NEW.mode_active=1 AND NEW.mode_payload_retired_at IS NOT NULL
        BEGIN UPDATE taste_groups SET mode_payload_retired_at=NULL WHERE id=NEW.id; END''')
    for name, event in zip(PICK_GUARDS, ('INSERT', 'UPDATE OF picked_mode_id'), strict=True):
        conn.execute(f'''CREATE TRIGGER {name} BEFORE {event} ON playlists
            WHEN NEW.picked_mode_id IS NOT NULL AND NOT EXISTS(
                SELECT 1 FROM taste_groups WHERE legacy_mode_id=NEW.picked_mode_id AND mode_label IS NOT NULL)
            BEGIN SELECT RAISE(ABORT,'FOREIGN KEY constraint failed'); END''')


def _retarget_playlists(conn):
    sql = definitions(conn, ('playlists',))['playlists']
    related = [statement for statement, in conn.execute("SELECT sql FROM sqlite_schema WHERE tbl_name='playlists' "
        "AND sql IS NOT NULL AND type IN ('index','trigger') ORDER BY type,name")]
    serial = account_storage._sequence(conn, 'playlists')
    track_source_cutover._replace_table(conn, 'playlists', sql.replace(PLAYLIST_REFERENCE, GROUP_REFERENCE))
    account_storage._preserve_sequence(conn, 'playlists', serial)
    for statement in related:
        conn.execute(statement)


def install(conn, _context):
    columns = {'label': 'TEXT', 'families': 'TEXT', 'centroid': 'BLOB',
               'size': f'INTEGER CHECK(mode_size IS NULL OR ({integer("mode_size")}))', 'rep_keys': 'TEXT',
               'first_seen': f'REAL CHECK({finite("mode_first_seen")})',
               'last_seen': f'REAL CHECK({finite("mode_last_seen")})', 'space': 'TEXT',
               'active': f'INTEGER CHECK({flag("mode_active", nullable=True)})'}
    for field, declaration in columns.items():
        conn.execute(f'ALTER TABLE taste_groups ADD COLUMN mode_{field} {declaration}')
    present = ' AND '.join(f'mode_{field} IS NOT NULL' for field in modes.FIELDS)
    absent = ' AND '.join(f'mode_{field} IS NULL' for field in modes.FIELDS)
    conn.execute('ALTER TABLE taste_groups ADD COLUMN mode_payload_retired_at REAL '
                 f'CHECK((mode_payload_retired_at IS NULL AND {absent}) OR (legacy_mode_id IS NOT NULL AND {present}))')
    source_fields = ','.join(modes.PAYLOAD_FIELDS)
    fields = ','.join(f'mode_{field}' for field in modes.PAYLOAD_FIELDS)
    updates = ','.join(f'mode_{field}=excluded.mode_{field}' for field in modes.PAYLOAD_FIELDS)
    conn.execute(f'''INSERT INTO taste_groups(external_key,legacy_mode_id,created_at,{fields})
        SELECT 'legacy:'||mode_id,mode_id,first_seen,{source_fields} FROM taste_modes WHERE true
        ON CONFLICT(legacy_mode_id) DO UPDATE SET {updates}''')
    _retarget_playlists(conn)
    conn.execute('DROP TABLE taste_modes')
    conn.execute(modes.view_sql())
    _guards(conn)
    conn.execute('UPDATE settings SET value=max(CAST(value AS INTEGER),'
                 'coalesce((SELECT max(legacy_mode_id) FROM taste_groups),0)) WHERE key=?',
                 (mode_retention.HIGH_WATER,))


def validate(conn):
    expected = {modes.VIEW, 'playlists', *ALLOCATION, *PICK_GUARDS}
    observed = definitions(conn, (*expected, RETIRED))
    if (set(observed) != expected or GROUP_REFERENCE not in observed['playlists']
            or observed[modes.VIEW] != modes.view_sql()):
        raise MigrationError('Legacy mode payloads were not folded into taste groups')
