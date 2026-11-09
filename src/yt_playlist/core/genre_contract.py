"""Genre owner names and compatible reads without preparation dependencies."""
import re
from functools import lru_cache
from pathlib import Path
import sqlite3

from . import fao
from .schema_contract import MigrationError


OWNERS = {'recordings': 'id', 'albums': 'id', 'discovered_artists': 'artist_id'}
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


def definition_name(sql):
    match = re.match(r'CREATE (?:UNIQUE )?(?:TABLE|VIEW|TRIGGER|INDEX)\s+"?(\w+)', sql.lstrip(), re.I)
    return match[1] if match else None


def installed_definitions(names):
    """Finite repair programs shared with the retained schema definition files."""
    for name, sql in _installed_definitions():
        if name in names:
            yield name, sql


@lru_cache(maxsize=1)
def _installed_definitions():
    """Parse shipped reference SQL once; installed databases stay live inputs."""
    root = Path(__file__).with_name('migrations')
    definitions = []
    for name in ('schema.sql', 'schema_metadata_evidence.sql',
                 'schema_genre_classifications.sql', 'schema_recipe_memberships.sql'):
        pending = ''
        source = fao.read(root / name, max_bytes=4 * 1024 * 1024).bytes.decode('utf-8')
        for line in source.splitlines(keepends=True):
            pending += line
            if sqlite3.complete_statement(pending):
                if definition_name(pending) is not None:
                    sql = classified_definition(pending).strip().rstrip(';')
                    definitions.append((definition_name(sql), sql))
                pending = ''
        if pending.strip():
            raise MigrationError('Incomplete installed genre definition: ' + name)
    return tuple(definitions)


def available(conn):
    return any(row[1] == 'genre_key' for row in conn.execute('PRAGMA table_info(recordings)'))


def expression(alias):
    return (f'COALESCE((SELECT g.name FROM genre_catalog g WHERE g.key={alias}.genre_key),'
            f'{alias}.unresolved_genre)')


def publication_table(name):
    return 'published_genre_' + name


def selected(conn):
    if available(conn) or not conn.execute(
            'SELECT 1 FROM main.sqlite_schema WHERE name=?', (publication_table('head'),)).fetchone():
        return None
    row = conn.execute('SELECT revision FROM '+publication_table('head')+' WHERE active=1').fetchone()
    return row[0] if row else None


def published_expression(conn, alias, owner):
    identity = selected(conn)
    if identity is None:
        return alias+'.genre'
    column = OWNERS[owner]
    return ('(SELECT CASE WHEN p.disposition=\'resolved\' THEN g.name ELSE p.raw_label END FROM main.'+
        publication_table('rows')+' p LEFT JOIN main.genre_catalog g ON g.key=p.genre_key WHERE p.stage='+str(identity)+
        ' AND p.owner='+repr(owner)+' AND p.source_key=json_array('+alias+'.'+column+'))')


def sql_key(label):
    return ('(SELECT CASE WHEN count(DISTINCT key)=1 THEN min(key) END FROM ('
            'SELECT COALESCE(d.genre_key,c.key) key FROM genre_catalog c LEFT JOIN genre_redirects d ON d.retired_key=c.key '
            f'WHERE c.key={label} OR c.name={label} COLLATE NOCASE '
            'UNION SELECT COALESCE(d.genre_key,a.genre_key) FROM genre_alias a '
            f'LEFT JOIN genre_redirects d ON d.retired_key=a.genre_key WHERE a.label={label} COLLATE NOCASE))')


@lru_cache(maxsize=2048)
def classified_definition(sql):
    """Adapt a retained label definition to the current genre owner contract."""
    head = sql.lstrip()
    table = re.match(r'CREATE TABLE\s+(\w+)\s*\(', head, re.I)
    if table and table[1] in OWNERS:
        sql, count = re.subn(r'\bgenre\s+TEXT\b', 'unresolved_genre TEXT', sql, count=1, flags=re.I)
        if count:
            closing = sql.rfind(')')
            column = ('genre_key TEXT REFERENCES genre_catalog(key) '
                      'CHECK(genre_key IS NULL OR unresolved_genre IS NULL)')
            sql = sql[:closing] + ',' + column + sql[closing:]
        return sql
    if head.startswith('CREATE VIEW catalog_tracks '):
        sql = sql.replace('r.genre END AS genre', expression('r') + ' END AS genre')
    elif head.startswith('CREATE VIEW discovered_albums '):
        sql = sql.replace('a.genre,', expression('a') + ' AS genre,')
    elif head.startswith('CREATE VIEW published_discovery_legacy_discovered_albums '):
        sql = sql.replace('a.genre genre,', expression('a') + ' genre,')
    elif head.startswith('CREATE VIEW entry_field_choices '):
        sql = sql.replace('ELSE o.text_value END',
            'ELSE COALESCE((SELECT name FROM genre_catalog WHERE key=COALESCE('
            '(SELECT genre_key FROM genre_redirects WHERE retired_key=o.genre_key),o.genre_key)),o.text_value) END')
    elif head.startswith(('CREATE TRIGGER catalog_tracks_update ', 'CREATE TRIGGER tracks_update ')):
        before = 'genre=CASE WHEN NEW.genre IS NOT OLD.genre THEN NEW.genre ELSE genre END,'
        key = sql_key('NEW.genre')
        after = (f'genre_key=CASE WHEN NEW.genre IS NOT OLD.genre THEN {key} ELSE genre_key END,'
                 'unresolved_genre=CASE WHEN NEW.genre IS NOT OLD.genre THEN '
                 f'CASE WHEN {key} IS NULL THEN NEW.genre END ELSE unresolved_genre END,')
        sql = sql.replace(before, after)
    elif head.startswith(('CREATE TRIGGER rec_capture_recordings_update ',
                          'CREATE TRIGGER rec_content_recordings_update ')):
        sql = sql.replace('OLD.genre IS NOT NEW.genre',
                          'OLD.genre_key IS NOT NEW.genre_key OR OLD.unresolved_genre IS NOT NEW.unresolved_genre')
    return sql
