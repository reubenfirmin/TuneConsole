"""Cross-connection changes to collection file ownership, without history scans."""
from .schema_contract import MigrationError, tokens

TABLE = 'collection_recovery_epoch'


def definitions(conn):
    from .collection_recovery import OWNERS

    yield TABLE, (f'CREATE TABLE {TABLE} ('
                  'id INTEGER PRIMARY KEY CHECK(id=1),revision INTEGER NOT NULL)')
    for table, filename in [*((table, 'recovery_filename') for table in OWNERS),
                            ('action_backups', 'retained_filename')]:
        # Supported installed layouts can precede individual file owners.
        if not any(row[1] == filename for row in conn.execute(f'PRAGMA main.table_info({table})')):
            continue
        for event, condition in (
            ('INSERT', f'NEW.{filename} IS NOT NULL'),
            ('DELETE', f'OLD.{filename} IS NOT NULL'),
            (f'UPDATE OF {filename}', f'OLD.{filename} IS NOT NEW.{filename}'),
        ):
            name = f'collection_recovery_changed_{table}_{event.split()[0].lower()}'
            yield name, (f'CREATE TRIGGER {name} AFTER {event} ON {table} WHEN {condition} BEGIN '
                         f'UPDATE {TABLE} SET revision=revision+1 WHERE id=1; END')


def validate(conn):
    _validate(conn, definitions(conn))
    row = conn.execute(f'SELECT revision FROM {TABLE} WHERE id=1').fetchone()
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
    from .collection_recovery import OWNERS

    for table, filename in [*((table, 'recovery_filename') for table in OWNERS),
                            ('action_backups', 'retained_filename')]:
        columns = list(conn.execute(f'PRAGMA main.table_info({table})'))
        if not any(row[1] == filename for row in columns):
            continue
        keys = [row[1] for row in sorted(columns, key=lambda row: row[5]) if row[5]]
        if not keys:
            raise MigrationError('Collection owner requires a primary key: ' + table)
        changed = ' OR '.join(f'OLD.{key} IS NOT NEW.{key}' for key in keys)
        name = f'collection_recovery_changed_{table}_key'
        yield name, (f'CREATE TRIGGER {name} AFTER UPDATE OF {",".join(keys)} ON {table} '
                     f'WHEN ({changed}) AND (OLD.{filename} IS NOT NULL OR NEW.{filename} IS NOT NULL) '
                     f'BEGIN UPDATE {TABLE} SET revision=revision+1 WHERE id=1; END')


def install_keys(conn, context=None):
    _install(conn, key_definitions(conn))


def validate_keys(conn):
    _validate(conn, key_definitions(conn))
