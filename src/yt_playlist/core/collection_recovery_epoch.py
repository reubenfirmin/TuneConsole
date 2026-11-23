"""Cross-connection changes to collection file ownership, without history scans."""
from .schema_contract import MigrationError, tokens
from .action_backup_inventory import COLUMN as BACKUP_INVENTORY

TABLE = 'collection_recovery_epoch'


def _owners():
    from .collection_owners import OWNERS, OWNER_KEYS, PLAYLIST_DOCUMENTS

    # The migration runner still validates earlier installed layouts.
    for table in (*OWNERS, 'sync_attempts', 'action_drafts', 'road_trip_drafts', 'playlist_gc_attempts'):
        yield table, table, 'recovery_filename', OWNER_KEYS.get(table)
    yield 'artists', 'artists', 'discovery_recovery_filename', None
    yield 'action_backups', 'action_backups', 'retained_filename', None
    yield 'action_backup_inventory', 'actions', BACKUP_INVENTORY, None
    for prefix in PLAYLIST_DOCUMENTS.values():
        yield f'playlist_{prefix}', 'playlist_scopes', f'{prefix}_recovery_filename', f'{prefix}_id'


def _columns(conn, table):
    if conn.execute("SELECT 1 FROM sqlite_schema WHERE type='table' AND name=?", (table,)).fetchone():
        return list(conn.execute(f'PRAGMA main.table_info({table})'))
    return ()


def _slot(conn):
    if any(row[1] == 'ownership_revision' for row in _columns(conn, 'collection_file_writes')):
        return 'collection_file_writes', 'ownership_revision', 0
    return TABLE, 'revision', 1


def revision(conn):
    row = conn.execute('SELECT ownership_revision FROM main.collection_file_writes WHERE id=0').fetchone()
    if row is None:
        raise ValueError('Missing collection recovery ownership revision')
    return row[0]


def definitions(conn):
    from .collection_owners import TASTE_CONTROLS
    from .spotify_records import ARCHIVE_PREFIXES

    target, revision_column, slot = _slot(conn)
    if target == TABLE:
        yield TABLE, (f'CREATE TABLE {TABLE} ('
                      'id INTEGER PRIMARY KEY CHECK(id=1),revision INTEGER NOT NULL)')
    for owner, table, filename, _ in _owners():
        # Supported installed layouts can precede individual file owners.
        if not any(row[1] == filename for row in _columns(conn, table)):
            continue
        present = "<> '[]'" if filename == BACKUP_INVENTORY else 'IS NOT NULL'
        for event, condition in (
            ('INSERT', f'NEW.{filename} {present}'),
            ('DELETE', f'OLD.{filename} {present}'),
            (f'UPDATE OF {filename}', f'OLD.{filename} IS NOT NEW.{filename}'),
        ):
            name = f'collection_recovery_changed_{owner}_{event.split()[0].lower()}'
            yield name, (f'CREATE TRIGGER {name} AFTER {event} ON {table} WHEN {condition} BEGIN '
                         f'UPDATE {target} SET {revision_column}={revision_column}+1 WHERE id={slot}; END')
    setting_owners = {}
    if target != TABLE and not any(_columns(conn, table) for table in TASTE_CONTROLS):
        setting_owners['taste'] = TASTE_CONTROLS.values()
    if target != TABLE and any(row[1] == 'sync_progress' for row in _columns(conn, 'accounts')):
        from .sync_state import ATTEMPT
        setting_owners['sync'] = (ATTEMPT,)
    for owner, owner_keys in setting_owners.items():
        keys = ','.join(f"'{key}'" for key in owner_keys)
        yield from _setting_definitions(owner, f'IN ({keys})', target, revision_column, slot)
    if target != TABLE and not any(_columns(conn, table) for table in ARCHIVE_PREFIXES):
        yield from _setting_definitions('spotify', "GLOB 'spotify_*_record:*'", target, revision_column, slot)


def _setting_definitions(owner, condition, target, revision_column, slot):
    old = f"CASE WHEN OLD.key {condition} THEN OLD.value->>'recovery_filename' END"
    new = f"CASE WHEN NEW.key {condition} THEN NEW.value->>'recovery_filename' END"
    for event, changed in (
        ('INSERT', f'({new}) IS NOT NULL'),
        ('DELETE', f'({old}) IS NOT NULL'),
        ('UPDATE OF key,value', f'({old}) IS NOT ({new})'),
    ):
        name = f'collection_recovery_changed_{owner}_settings_{event.split()[0].lower()}'
        yield name, (f'CREATE TRIGGER {name} AFTER {event} ON settings WHEN {changed} BEGIN '
                     f'UPDATE {target} SET {revision_column}={revision_column}+1 WHERE id={slot}; END')


def validate(conn):
    _validate(conn, definitions(conn))
    table, column, slot = _slot(conn)
    row = conn.execute(f'SELECT {column} FROM {table} WHERE id={slot}').fetchone()
    if row is None or type(row[0]) is not int:
        raise MigrationError('Missing collection recovery ownership revision')


def _validate(conn, declarations):
    for name, sql in declarations:
        row = conn.execute('SELECT sql FROM main.sqlite_schema WHERE name=?', (name,)).fetchone()
        if row is None or tokens(row[0]) != tokens(sql):
            raise MigrationError('Incompatible collection recovery epoch: ' + name)


def install(conn, context=None):
    # DDL and one seed row only. In particular, do not index or rewrite retained
    # filenames to admit a populated library or an unfinished preparation.
    _install(conn, definitions(conn))


def _install(conn, declarations):
    for name, sql in declarations:
        row = conn.execute('SELECT sql FROM main.sqlite_schema WHERE name=?', (name,)).fetchone()
        if row is None:
            conn.execute(sql)
            if name == TABLE:
                conn.execute(f'INSERT INTO {TABLE} VALUES (1,0)')
        elif tokens(row[0]) != tokens(sql):
            raise MigrationError('Incompatible collection recovery epoch: ' + name)


def key_definitions(conn):
    target, revision_column, slot = _slot(conn)
    for owner, table, filename, owner_key in _owners():
        columns = _columns(conn, table)
        if not any(row[1] == filename for row in columns):
            continue
        keys = [row[1] for row in sorted(columns, key=lambda row: row[5]) if row[5]]
        if not keys:
            raise MigrationError('Collection owner requires a primary key: ' + table)
        if owner_key and owner_key not in keys:
            keys.append(owner_key)
        changed = ' OR '.join(f'OLD.{key} IS NOT NEW.{key}' for key in keys)
        present = "<> '[]'" if filename == BACKUP_INVENTORY else 'IS NOT NULL'
        name = f'collection_recovery_changed_{owner}_key'
        yield name, (f'CREATE TRIGGER {name} AFTER UPDATE OF {",".join(keys)} ON {table} '
                     f'WHEN ({changed}) AND (OLD.{filename} {present} OR NEW.{filename} {present}) '
                     f'BEGIN UPDATE {target} SET {revision_column}={revision_column}+1 WHERE id={slot}; END')


def install_keys(conn, context=None):
    _install(conn, key_definitions(conn))


def validate_keys(conn):
    _validate(conn, key_definitions(conn))
