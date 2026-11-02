"""Read retained Radio publications without loading their preparation workers."""
import hashlib
from pathlib import Path

from . import fao, recommendation_outputs as files
from .schema_contract import MigrationError


ORIGIN_KEYS = ('radio_session_playlist_ids','radio_playlist_ytm','radio_playlist_a_ytm','radio_playlist_b_ytm')


def session_selected(conn):
    if conn.execute("SELECT 1 FROM main.sqlite_schema WHERE name='radio_sessions'").fetchone():
        return None
    if not conn.execute("SELECT 1 FROM main.sqlite_schema WHERE name='published_radio_head'").fetchone():
        return None
    row = conn.execute("SELECT revision FROM published_radio_head WHERE name='radio-session' AND active=1").fetchone()
    return row[0] if row else None


def session_value(conn, revision, default=None):
    row = conn.execute('SELECT * FROM prepared_radio_sessions WHERE generation=?', (revision,)).fetchone()
    if row is None:
        raise MigrationError('Published Radio checkpoint is missing')
    if not row['source_present']:
        return default
    if row['filename'] is None:
        return None
    with fao.open_reader(row['filename']) as stream:
        encoded = stream.read(row['byte_size'] + 1)
    if (len(encoded) != row['byte_size'] or hashlib.sha256(encoded).hexdigest() != row['digest']
            or Path(row['filename']).stem != row['digest']):
        raise MigrationError('Prepared Radio file does not match its digest')
    return encoded.decode('utf-8')


def origin_selected(conn):
    if conn.execute("SELECT 1 FROM sqlite_schema WHERE name='radio_playlist_origins'").fetchone():
        return None
    if not conn.execute("SELECT 1 FROM main.sqlite_schema WHERE name='published_radio_origin_head'").fetchone():
        return None
    row = conn.execute("SELECT revision FROM published_radio_origin_head WHERE name='radio-origins' AND active=1").fetchone()
    return row[0] if row else None


def origin_value(conn, source, revision, default=None):
    row = conn.execute('SELECT g.* FROM published_radio_origin_sources l JOIN prepared_radio_origin_generations '
        'g USING(source,generation) WHERE l.revision=? AND l.source=?', (revision,source)).fetchone()
    if row is None:
        return default
    parts = conn.execute('SELECT position,filename,digest,byte_size FROM prepared_radio_origin_original_parts '
        'WHERE source=? AND generation=? ORDER BY position LIMIT 257', (row['source'],row['generation'])).fetchall()
    if [part[0] for part in parts] != list(range(row['part_count'])):
        raise MigrationError('Prepared Radio origin original parts are incomplete')
    raw = ''.join(files.read(*part[1:], surface='radio_origin', chunk_key=f'{source}:{part[0]}') for part in parts)
    if len(raw.encode()) != row['raw_size'] or hashlib.sha256(raw.encode()).hexdigest() != row['raw_digest']:
        raise MigrationError('Prepared Radio origin differs from its original digest')
    return None if row['source_kind'] == 'null' else raw


def origin_ids(conn, revision):
    order = 'CASE o.source '+' '.join('WHEN '+repr(source)+' THEN '+str(index) for index,source in enumerate(ORIGIN_KEYS))+' END'
    rows = conn.execute('SELECT o.provider_playlist_id FROM published_radio_origin_sources l JOIN prepared_radio_origins '
        'o USING(source,generation) WHERE l.revision=? ORDER BY '+order+',o.position', (revision,))
    return list(dict.fromkeys(row[0] for row in rows))
