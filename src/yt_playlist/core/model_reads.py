"""Read selected encoder artifacts and fingerprints without loading their jobs."""
import hashlib

from . import model_contract, recommendation_outputs as files
from .schema_contract import MigrationError

NAME = 'content-models'
HEAD = 'published_content_model_heads'
LINKS = 'published_content_model_encoders'
TOKENS = 'published_content_model_fingerprints'


def native(conn):
    return conn.execute("SELECT 1 FROM main.sqlite_schema WHERE name='taste_model_state'").fetchone() is not None


def installed(conn):
    return (conn.execute('SELECT 1 FROM main.sqlite_schema WHERE name=?', (HEAD,)).fetchone() is not None
            and conn.execute(f'SELECT 1 FROM {HEAD} WHERE name=?', (NAME,)).fetchone() is not None)


def selected(conn):
    if native(conn) or not conn.execute('SELECT 1 FROM main.sqlite_schema WHERE name=?', (HEAD,)).fetchone():
        return None
    row = conn.execute(f'SELECT artifact_revision,fingerprint_revision FROM {HEAD} WHERE name=? AND active=1', (NAME,)).fetchone()
    return tuple(row) if row else None


def read_original(conn, row):
    parts = conn.execute('SELECT position,filename,digest,byte_size FROM prepared_content_encoder_parts '
        'WHERE source=? AND generation=? ORDER BY position LIMIT 257', (row['source'], row['generation'])).fetchall()
    if [part[0] for part in parts] != list(range(row['part_count'])):
        raise MigrationError('Prepared content encoder original parts are incomplete')
    raw = ''.join(files.read(*part[1:], surface=row['source'], chunk_key=str(part[0])) for part in parts)
    if len(raw.encode()) != row['raw_size'] or hashlib.sha256(raw.encode()).hexdigest() != row['raw_digest']:
        raise MigrationError('Prepared content encoder differs from its original digest')
    return None if row['source_kind'] == 'null' else raw


def read(conn, revisions, key, default):
    if key in model_contract.FINGERPRINTS:
        row = conn.execute(f'SELECT raw_value FROM {TOKENS} WHERE artifact_revision=? AND fingerprint_revision=? AND source=?',
                           (*revisions, key)).fetchone()
        return row[0] if row and row[0] is not None else default
    row = conn.execute(f'SELECT g.* FROM {LINKS} l JOIN prepared_content_encoder_generations g USING(source,generation) '
                       'WHERE l.artifact_revision=? AND l.fingerprint_revision=? AND l.source=?', (*revisions, key)).fetchone()
    return read_original(conn, row) or default if row is not None else default
