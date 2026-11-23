"""Checkpoint verified album files before the schema transaction."""
import logging

from .. import album_description_files, album_note_outputs, fao, recommendation_outputs
from .runner import PreparationChanged

log = logging.getLogger(__name__)

PAID_KEY = "'migration.90.paid:' || json_array(original.outcome_filename,original.outcome_digest)"
NOTE_KEY = "'migration.90.note:' || json_array(r.browse_id,r.filename,r.content_digest,r.byte_size)"

_PAID = f'''SELECT {PAID_KEY},original.outcome_filename,original.outcome_digest
FROM album_description_requests original LEFT JOIN settings s ON s.key=({PAID_KEY})
WHERE original.outcome_filename IS NOT NULL AND s.key IS NULL'''
_NOTES = f'''SELECT {NOTE_KEY},r.browse_id,r.filename,r.content_digest,r.byte_size
FROM album_note_outputs r LEFT JOIN settings s ON s.key=({NOTE_KEY})
WHERE s.key IS NULL'''


def _applied(conn, version):
    return conn.execute('SELECT 1 FROM schema_changes WHERE version=?', (version,)).fetchone()


def _read_paid(filename, digest):
    try:
        return album_description_files.read(filename, digest)
    except (OSError, ValueError, fao.FileChanged) as error:
        log.warning('Paid album description body could not be imported from %s: %s', filename, error)
        return None


def _encode_note(browse, filename, digest, size):
    try:
        note = recommendation_outputs.read(filename, digest, size, surface='album_notes', chunk_key=browse)
    except (OSError, ValueError):
        # A missing or unreadable note can be regenerated.
        return None
    return album_note_outputs.encode(browse, note)


def prepare(conn, version):
    while True:
        conn.execute('BEGIN')
        try:
            if _applied(conn, version):
                return
            paid = conn.execute(_PAID + ' ORDER BY original.id LIMIT 8').fetchall()
            notes = [] if paid else conn.execute(_NOTES + ' ORDER BY r.id LIMIT 8').fetchall()
        finally:
            conn.rollback()
        if not paid and not notes:
            return

        # File verification can take arbitrarily long. Hold no SQLite locks
        # until the exact receipts and their prepared values are known.
        values = [(key, _read_paid(filename, digest)) for key, filename, digest in paid]
        values.extend((key, _encode_note(*receipt)) for key, *receipt in notes)
        conn.execute('BEGIN IMMEDIATE')
        with conn:
            if _applied(conn, version):
                return
            conn.executemany('INSERT INTO settings VALUES (?,?) ON CONFLICT(key) DO NOTHING', values)


def require_current(conn):
    for query in (_PAID, _NOTES):
        if conn.execute(query + ' LIMIT 1').fetchone():
            raise PreparationChanged()


def clear(conn):
    conn.execute("DELETE FROM settings WHERE key GLOB 'migration.90.*'")
