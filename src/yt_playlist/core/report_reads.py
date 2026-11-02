"""Read a selected report and its exact retained file parts."""
import hashlib

from . import recommendation_outputs as files
from .schema_contract import MigrationError


def active(conn):
    if conn.execute("SELECT 1 FROM main.sqlite_schema WHERE name='recommendation_report_publications' AND type='table'").fetchone():
        return False
    if not conn.execute("SELECT 1 FROM main.sqlite_schema WHERE name='published_reports'").fetchone():
        return False
    return conn.execute('SELECT 1 FROM published_reports p JOIN prepare_recommendation_reports_state s ON s.id=p.id '
                        'WHERE p.id=1 AND p.prepared_revision=s.input_revision').fetchone() is not None


def selected(conn, surface):
    return conn.execute('SELECT g.* FROM published_reports p JOIN published_report_heads h '
        'ON h.revision=p.prepared_revision JOIN prepared_report_generations g '
        'ON g.generation=h.generation AND g.surface=h.surface WHERE p.id=1 AND h.surface=?', (surface,)).fetchone()


def read_original(conn, row):
    parts = conn.execute('SELECT position,filename,digest,byte_size FROM prepared_report_parts '
        'WHERE generation=? AND surface=? ORDER BY position LIMIT 65',
        (row['generation'], row['surface'])).fetchall()
    if [part[0] for part in parts] != list(range(row['part_count'])):
        raise MigrationError('Prepared report parts are incomplete')
    raw = ''.join(files.read(*part[1:], surface=row['surface'], chunk_key=str(part[0])) for part in parts)
    if len(raw.encode()) != row['total_bytes'] or hashlib.sha256(raw.encode()).hexdigest() != row['content_digest']:
        raise MigrationError('Prepared report differs from its original digest')
    return raw
