"""Replace selected labels with canonical references, retaining unresolved input.

This finite transformation is shared by the frozen bootstrap DDL and installed
dependencies. It changes only the three genre owners and their known readers;
it never rewrites provider observations or historical recovery documents.
"""
import re
from pathlib import Path
import sqlite3

from yt_playlist.core import genre_classifications, genre_identity
from .runner import MigrationError

TABLES = ('recordings', 'albums', 'discovered_artists')
COLUMN = ('genre_key TEXT REFERENCES genre_catalog(key) '
          'CHECK(genre_key IS NULL OR unresolved_genre IS NULL)')


def sql_key(label):
    return ('(SELECT CASE WHEN count(DISTINCT key)=1 THEN min(key) END FROM ('
            'SELECT COALESCE(d.genre_key,c.key) key FROM genre_catalog c LEFT JOIN genre_redirects d ON d.retired_key=c.key '
            f'WHERE c.key={label} OR c.name={label} COLLATE NOCASE '
            'UNION SELECT COALESCE(d.genre_key,a.genre_key) FROM genre_alias a '
            f'LEFT JOIN genre_redirects d ON d.retired_key=a.genre_key WHERE a.label={label} COLLATE NOCASE))')


def transform(sql):
    head = sql.lstrip()
    table = re.match(r'CREATE TABLE\s+(\w+)\s*\(', head, re.I)
    if table and table[1] in TABLES:
        sql, count = re.subn(r'\bgenre\s+TEXT\b', 'unresolved_genre TEXT', sql, count=1, flags=re.I)
        if count:
            closing = sql.rfind(')')
            sql = sql[:closing] + ',' + COLUMN + sql[closing:]
        return sql
    if head.startswith('CREATE VIEW catalog_tracks '):
        sql = sql.replace('r.genre END AS genre', genre_classifications.expression('r') + ' END AS genre')
    elif head.startswith('CREATE VIEW discovered_albums '):
        sql = sql.replace('a.genre,', genre_classifications.expression('a') + ' AS genre,')
    elif head.startswith('CREATE VIEW published_discovery_legacy_discovered_albums '):
        sql = sql.replace('a.genre genre,', genre_classifications.expression('a') + ' genre,')
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


def statements():
    pending = ''
    for line in Path(__file__).with_name('schema_genre_classifications.sql').read_text().splitlines(True):
        pending += line
        if sqlite3.complete_statement(pending):
            yield pending
            pending = ''
    if pending.strip():
        raise MigrationError('Incomplete genre classification schema')


def apply(conn, context):
    original = {}
    for table in TABLES:
        identity = genre_classifications.OWNERS[table]
        original[table] = [(row[0], row[1], *genre_identity.resolve(conn, row[1]))
                           for row in conn.execute(f'SELECT {identity},genre FROM {table}')]
    dependent = list(conn.execute("SELECT type,name,sql FROM sqlite_schema "
                                  "WHERE type IN ('view','trigger') ORDER BY rowid"))
    for kind, name, _ in reversed(dependent):
        conn.execute(f'DROP {kind} "' + name.replace('"', '""') + '"')
    for table in TABLES:
        conn.execute(f'ALTER TABLE {table} RENAME COLUMN genre TO unresolved_genre')
        conn.execute(f'ALTER TABLE {table} ADD COLUMN {COLUMN}')
        identity = genre_classifications.OWNERS[table]
        conn.executemany(f'UPDATE {table} SET genre_key=?,unresolved_genre=NULL WHERE {identity}=?',
                         [(key, subject) for subject, label, key, reason in original[table] if key is not None])
    for sql in statements():
        conn.execute(sql)
    for table, owner in (('recordings', 'recording_id'), ('albums', 'album_id'), ('discovered_artists', 'artist_id')):
        conn.executemany(f'INSERT INTO genre_classification_legacy({owner},raw_label,genre_key,resolution_reason) '
                         'VALUES (?,?,?,?)',
                         [(subject, label, key, reason) for subject, label, key, reason in original[table] if label is not None])
    for _, _, sql in dependent:
        conn.execute(transform(sql))
    if conn.execute('PRAGMA foreign_key_check').fetchone():
        raise MigrationError('Genre classification cutover changed an owner reference')
