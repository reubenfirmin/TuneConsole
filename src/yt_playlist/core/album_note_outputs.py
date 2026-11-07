"""Disposable release-note files; paid prose belongs to album_descriptions."""
from . import album_note_requests, recommendation_outputs
from .recommendation_work import finite

MAX_NOTES = 128


def prepare(conn, value, at):
    if at is not None:
        finite(at)
    if not isinstance(value, dict) or len(value) > MAX_NOTES:
        raise ValueError('Album notes must be a bounded release map')
    files = []
    for browse, note in value.items():
        if not isinstance(browse, str) or not browse or not isinstance(note, dict):
            raise ValueError('Album note output requires a release ID and an object')
        encoded = recommendation_outputs.encode('album_notes', browse, note)
        files.append((browse, *recommendation_outputs.publish(recommendation_outputs.directory(conn), encoded)))
    return files


def _insert(conn, browse, filename, digest, size, at):
    reference, reason = album_note_requests._reference(conn, browse)
    conn.execute('INSERT INTO album_note_outputs '
                 '(browse_id,provider,album_provider_id,unresolved_reason,filename,content_digest,byte_size,built_at) '
                 "VALUES (?,'youtube',?,?,?,?,?,?)", (browse, reference, reason, filename, digest, size, at))


def replace(conn, value, at):
    album_note_requests._transaction(conn)
    files = prepare(conn, value, at)
    conn.execute('DELETE FROM album_note_outputs')
    for browse, filename, digest, size in files:
        _insert(conn, browse, filename, digest, size, at)


def publish(conn, browse, note, at):
    album_note_requests._transaction(conn)
    files = prepare(conn, {browse: note}, at)
    conn.execute('DELETE FROM album_note_outputs WHERE browse_id=?', (browse,))
    conn.execute('DELETE FROM album_note_outputs WHERE id IN '
                 '(SELECT id FROM album_note_outputs ORDER BY id DESC LIMIT -1 OFFSET ?)', (MAX_NOTES - 1,))
    _insert(conn, *files[0], at)


def read(conn):
    result = {}
    for row in conn.execute('SELECT browse_id,filename,content_digest,byte_size FROM album_note_outputs ORDER BY id'):
        result[row[0]] = recommendation_outputs.read(*row[1:], surface='album_notes', chunk_key=row[0])
    return result or None
