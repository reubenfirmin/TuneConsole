"""Upgrade the exact earlier genre schemas installed during the schema work.

Some libraries reached v86/v88 before the redirect adapters and the complete
axis archive were added. Accept only those known definitions on admission;
the next numbered transaction installs the current definitions and constraints.
"""
import re
from functools import lru_cache

from . import v086_genre_classifications


ADDITIONS = frozenset((
    'genre_redirect_current_insert', 'genre_redirect_current_update', 'genre_redirect_retained',
    'ix_genre_catalog_name', 'ix_genre_alias_label', 'ix_genre_alias_owner',
    'recipe_artist_position_update', 'recipe_unresolved_artist_position_update',
    'recipe_genre_position_update', 'recipe_unresolved_genre_position_update',
))
REPLACEMENTS = ('catalog_tracks_update', 'tracks_update', 'entry_field_choices')
ARCHIVE_COLUMNS = ('weight REAL', 'lean_value REAL', 'score REAL', 'updated_at REAL',
                   'last_graduated_day TEXT', 'source TEXT', 'factor REAL',
                   'new_weight REAL', 'created_at REAL')
LEGACY_KEY = ('(SELECT CASE WHEN count(DISTINCT key)=1 THEN min(key) END FROM ('
              'SELECT key FROM genre_catalog WHERE key=NEW.genre OR name=NEW.genre COLLATE NOCASE '
              'UNION SELECT genre_key FROM genre_alias WHERE label=NEW.genre COLLATE NOCASE))')


def _name(sql):
    match = re.match(r'CREATE (?:UNIQUE )?(?:TABLE|VIEW|TRIGGER|INDEX)\s+"?(\w+)', sql.lstrip(), re.I)
    return match[1] if match else None


def legacy_statements(statements):
    """The finite installed predecessor, including the original NULL loophole."""
    for sql in statements:
        name = _name(sql)
        if name in ADDITIONS:
            continue
        if name in ('catalog_tracks_update', 'tracks_update'):
            sql = sql.replace(v086_genre_classifications.sql_key('NEW.genre'), LEGACY_KEY)
        elif name == 'entry_field_choices':
            sql = sql.replace('key=COALESCE((SELECT genre_key FROM genre_redirects '
                              'WHERE retired_key=o.genre_key),o.genre_key)', 'key=o.genre_key')
        elif name == 'taste_axes':
            sql = sql.replace("family_key IS NULL AND value IS NOT NULL AND length(value)>0",
                              "family_key IS NULL AND length(value)>0")
        elif name == 'taste_axis_legacy':
            for column in ARCHIVE_COLUMNS:
                sql = sql.replace(' ' + column + ',\n', '')
        yield sql


@lru_cache(maxsize=6)
def _contracts(version):
    """Reuse fixed reference DDL; live installed schemas are checked each time."""
    from . import current
    statements = tuple(current.statements(version=version))
    return (current._schema_contract(statements),
            current._schema_contract(legacy_statements(statements)))


def validate_predecessor(conn, version, *, foreign_keys=True, data=True):
    from . import current
    reference, earlier = _contracts(version)
    # Admission adapts only this call's expected map to the observed finite
    # predecessor. Never retain another database's accepted replacements.
    expected = reference.copy()
    objects = {name: (kind, sql) for name, kind, sql in conn.execute(
        "SELECT name,type,sql FROM sqlite_schema WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%'").fetchall()}
    for name in (*REPLACEMENTS, 'taste_axes', 'taste_axis_legacy'):
        if name not in earlier or name not in objects:
            continue
        kind, sql = objects[name]
        actual = (kind, current._table(conn, name, sql) if kind == 'table' else current._tokens(sql))
        if actual == earlier[name]:
            expected[name] = earlier[name]
    for name in ADDITIONS:
        if name not in objects:
            expected.pop(name, None)
    current._validate(conn, expected, version, foreign_keys=foreign_keys, data=data)


def apply(conn, context):
    from . import current
    statements = {_name(sql): sql for sql in current.statements()}
    for name in REPLACEMENTS:
        kind = 'VIEW' if name == 'entry_field_choices' else 'TRIGGER'
        conn.execute(f'DROP {kind} {name}')
        conn.execute(statements[name])
    for name in sorted(ADDITIONS):
        if conn.execute('SELECT 1 FROM sqlite_schema WHERE name=?', (name,)).fetchone() is None:
            conn.execute(statements[name])
    sql = conn.execute("SELECT sql FROM sqlite_schema WHERE name='taste_axes'").fetchone()[0]
    if current._checks(sql) != current._checks(statements['taste_axes']):
        retained_triggers = list(conn.execute("SELECT sql FROM sqlite_schema WHERE type='trigger' "
            "AND tbl_name='taste_axes' ORDER BY rowid"))
        indexes = list(conn.execute("SELECT sql FROM sqlite_schema WHERE type='index' "
                                    "AND tbl_name='taste_axes' AND sql IS NOT NULL"))
        conn.execute(statements['taste_axes'].replace('CREATE TABLE taste_axes', 'CREATE TABLE upgraded_taste_axes', 1))
        conn.execute('INSERT INTO upgraded_taste_axes SELECT * FROM taste_axes')
        conn.execute('DROP TABLE taste_axes')
        conn.execute('ALTER TABLE upgraded_taste_axes RENAME TO taste_axes')
        for (index,) in indexes:
            conn.execute(index)
        for (trigger,) in retained_triggers:
            conn.execute(trigger)
    columns = {row[1] for row in conn.execute('PRAGMA table_info(taste_axis_legacy)')}
    for declaration in ARCHIVE_COLUMNS:
        if declaration.split()[0] not in columns:
            conn.execute('ALTER TABLE taste_axis_legacy ADD COLUMN ' + declaration)
