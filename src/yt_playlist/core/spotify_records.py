"""Keyed import and transfer records, including their retained file references."""
import json

from . import worker_settings

IMPORT = 'spotify_import_record:'
TRANSFER = 'spotify_transfer_record:'
REFERENCE = ('recovery_filename', 'recovery_digest', 'recovery_bytes')
ARCHIVE_PREFIXES = {'spotify_import_jobs': IMPORT, 'spotify_transfer_receipts': TRANSFER}


def read(conn, key):
    raw = worker_settings.read(conn, key)
    return json.loads(raw) if raw is not None else None


def save(conn, key, value):
    worker_settings.save(conn, key, json.dumps(value) if value is not None else None)


def matching(conn, prefix):
    return conn.execute('SELECT key,value FROM settings WHERE key GLOB ? ORDER BY key', (prefix + '*',))


def clear(conn, prefix):
    conn.execute('DELETE FROM settings WHERE key GLOB ?', (prefix + '*',))


def next_identity(conn, prefix):
    return int(worker_settings.read(conn, prefix + 'next_id', '1'))


def note_identity(conn, prefix, identity):
    if identity >= next_identity(conn, prefix):
        worker_settings.save(conn, prefix + 'next_id', str(identity + 1))


def transfer_prefix(kind, account):
    if kind not in ('playlist', 'album') or (account is not None and type(account) is not int):
        raise ValueError('Unsupported Spotify transfer owner')
    return f'{TRANSFER}{kind}:{account}:'


def transfer_key(kind, account, fingerprint):
    return transfer_prefix(kind, account) + fingerprint


def references(conn, prefix):
    return conn.execute("""SELECT key,value->>'id',value->>'recovery_filename',
        value->>'recovery_digest',value->>'recovery_bytes' FROM settings
        WHERE key GLOB ? AND key<>? ORDER BY key""", (prefix + '*', prefix + 'next_id'))
