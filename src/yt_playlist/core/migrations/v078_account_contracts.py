"""Account flags, active-master uniqueness and durable scope ownership.

The runner disables FK actions for the atomic account table replacement,
then verifies and restores enforcement before committing.
Empty setup and temporary zero-master states remain legal.
"""
from .runner import MigrationError
from yt_playlist.core.road_trip_contract import validate_recipe_values


COLUMNS = ('id', 'label', 'credential_ref', 'brand_account_id', 'is_master', 'last_auth_ok', 'active')
ACCOUNT_TABLE = (
    'CREATE TABLE identities (id INTEGER PRIMARY KEY,label TEXT NOT NULL,credential_ref TEXT NOT NULL,'
    'brand_account_id TEXT,is_master INTEGER NOT NULL DEFAULT 0 '
    "CHECK(typeof(is_master)='integer' AND is_master IN (0,1)),last_auth_ok REAL,"
    "active INTEGER NOT NULL DEFAULT 1 CHECK(typeof(active)='integer' AND active IN (0,1)),UNIQUE(label))"
)
RECIPE_COLUMNS = ('id', 'name', 'own_pct', 'artists', 'genres', 'blacklist_genres', 'target_minutes',
                  'last_playlist_id', 'created_at', 'updated_at', 'familiarity_pct')
RECIPE_TABLE = (
    'CREATE TABLE road_trip_recipes (id INTEGER PRIMARY KEY AUTOINCREMENT,name TEXT NOT NULL,'
    "own_pct INTEGER NOT NULL CHECK(typeof(own_pct)='integer' AND own_pct BETWEEN 0 AND 100),"
    'artists TEXT NOT NULL,genres TEXT NOT NULL,blacklist_genres TEXT NOT NULL,'
    "target_minutes INTEGER NOT NULL CHECK(typeof(target_minutes)='integer' AND target_minutes BETWEEN 15 AND 720),"
    'last_playlist_id TEXT,created_at REAL NOT NULL '
    "CHECK(typeof(created_at) IN ('integer','real') AND created_at BETWEEN -1.7976931348623157e308 AND 1.7976931348623157e308),"
    "updated_at REAL NOT NULL CHECK(typeof(updated_at) IN ('integer','real') AND "
    'updated_at BETWEEN -1.7976931348623157e308 AND 1.7976931348623157e308),'
    "familiarity_pct INTEGER NOT NULL DEFAULT 50 CHECK(typeof(familiarity_pct)='integer' AND familiarity_pct BETWEEN 0 AND 100))"
)


def statements():
    yield 'CREATE UNIQUE INDEX ix_account_active_master ON identities(is_master) WHERE active=1 AND is_master=1'
    for table in ('playlist_membership_refs', 'playlist_library_revisions'):
        for event in ('INSERT', 'UPDATE'):
            # Shared lists may have many observing accounts. An account-scoped
            # system list may only be observed under its exact owning account.
            yield (f'CREATE TRIGGER owned_{table}_{event.lower()} BEFORE {event} ON {table} '
                   'WHEN NEW.scope_ref IS NOT NULL AND NOT EXISTS(SELECT 1 FROM playlist_scopes s '
                   "WHERE s.ref=NEW.scope_ref AND (s.kind<>'account' OR s.identity_id=NEW.identity_id)) "
                   "BEGIN SELECT RAISE(ABORT,'Playlist scope belongs to another account'); END")
    yield ('CREATE TRIGGER immutable_playlist_scope_identity BEFORE UPDATE OF ref,provider_id,identity_id,kind '
           'ON playlist_scopes WHEN NEW.ref IS NOT OLD.ref OR NEW.provider_id IS NOT OLD.provider_id '
           'OR NEW.identity_id IS NOT OLD.identity_id OR NEW.kind IS NOT OLD.kind '
           "BEGIN SELECT RAISE(ABORT,'Durable playlist scope identity is immutable'); END")


def _preflight(conn):
    if tuple(row[1] for row in conn.execute('PRAGMA table_info(identities)')) != COLUMNS:
        raise MigrationError('Unexpected account columns; original account table retained')
    if tuple(row[1] for row in conn.execute('PRAGMA table_info(road_trip_recipes)')) != RECIPE_COLUMNS:
        raise MigrationError('Unexpected recipe columns; original recipes retained')
    for row in conn.execute('SELECT id,own_pct,familiarity_pct,target_minutes,created_at,updated_at FROM road_trip_recipes'):
        try:
            validate_recipe_values(row[1], row[2], row[3], row[4])
            validate_recipe_values(row[1], row[2], row[3], row[5])
        except ValueError as exc:
            raise MigrationError(f'Invalid stored recipe {row[0]}; original recipe retained: {exc}') from exc
    invalid = conn.execute("SELECT id FROM identities WHERE typeof(active)<>'integer' OR active NOT IN (0,1) "
                           "OR typeof(is_master)<>'integer' OR is_master NOT IN (0,1) LIMIT 1").fetchone()
    if invalid:
        raise MigrationError(f'Invalid stored account flags: account {invalid[0]}')
    masters = [row[0] for row in conn.execute('SELECT id FROM identities WHERE active=1 AND is_master=1 LIMIT 2')]
    if len(masters) > 1:
        raise MigrationError(f'Multiple active master accounts: {masters}')
    for table in ('playlist_membership_refs', 'playlist_library_revisions'):
        invalid = conn.execute(f'SELECT m.scope_ref,m.identity_id FROM {table} m JOIN playlist_scopes s '
            "ON s.ref=m.scope_ref WHERE s.kind='account' AND s.identity_id IS NOT m.identity_id LIMIT 1").fetchone()
        if invalid:
            raise MigrationError(f'Invalid {table} account/scope pair: {tuple(invalid)!r}')


def _replace(conn, table, columns, definition):
    indexes = [row[0] for row in conn.execute("SELECT sql FROM sqlite_schema WHERE type='index' "
                                            'AND tbl_name=? AND sql IS NOT NULL', (table,))]
    sequence = conn.execute('SELECT seq FROM sqlite_sequence WHERE name=?', (table,)).fetchone()
    temporary = table + '_contract_replacement'
    conn.execute(definition.replace('CREATE TABLE ' + table, 'CREATE TABLE ' + temporary, 1))
    fields = ','.join(columns)
    conn.execute(f'INSERT INTO {temporary}({fields}) SELECT {fields} FROM {table}')
    differences = ' OR '.join(f'old.{field} IS NOT new.{field}' for field in columns)
    if (conn.execute(f'SELECT count(*) FROM {table}').fetchone()[0] !=
            conn.execute(f'SELECT count(*) FROM {temporary}').fetchone()[0]
            or conn.execute(f'SELECT 1 FROM {table} old LEFT JOIN {temporary} new '
                            f'ON old.id=new.id WHERE new.id IS NULL OR {differences} LIMIT 1').fetchone()):
        raise MigrationError(f'Contract replacement changed original {table} values')
    conn.execute('DROP TABLE ' + table)
    conn.execute(f'ALTER TABLE {temporary} RENAME TO {table}')
    if sequence:
        # Deleted recipe IDs must stay retired as well as all remaining IDs.
        conn.execute('UPDATE sqlite_sequence SET seq=max(seq,?) WHERE name=?', (sequence[0], table))
    for sql in indexes:
        conn.execute(sql)


def apply(conn, context):
    if conn.execute('PRAGMA foreign_keys').fetchone()[0]:
        raise MigrationError('Account table replacement requires the migration runner')
    _preflight(conn)
    # Preserve installed dependent SQL exactly. Removing it temporarily avoids
    # SQLite validating a view while its original account table is absent.
    dependent = list(conn.execute("SELECT type,name,sql FROM sqlite_schema "
                                  "WHERE type IN ('view','trigger') ORDER BY rowid"))
    for kind, name, _ in reversed(dependent):
        quoted = '"' + name.replace('"', '""') + '"'
        conn.execute(f'DROP {kind} {quoted}')
    _replace(conn, 'identities', COLUMNS, ACCOUNT_TABLE)
    _replace(conn, 'road_trip_recipes', RECIPE_COLUMNS, RECIPE_TABLE)
    for _, _, sql in dependent:
        conn.execute(sql)
    for sql in statements():
        conn.execute(sql)


def validate(conn, *, foreign_keys=True, data=True):
    from . import current
    current._validate(conn, current._schema_contract((ACCOUNT_TABLE, RECIPE_TABLE)), 78,
                      foreign_keys=foreign_keys, data=data)
    from ..schema_inspection import objects
    definitions = {name: sql for name, (_kind, sql) in objects(conn).items()}
    for sql in statements():
        name = sql.split()[3 if sql.startswith('CREATE UNIQUE') else 2]
        actual = definitions.get(name)
        if actual is None or current._tokens(actual) != current._tokens(sql):
            raise MigrationError(f'Incompatible account/scope contract: {name}')
