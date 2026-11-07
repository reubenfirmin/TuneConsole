"""Retained receipt identities and byte verification, independent of journals."""
import hashlib
import json
from pathlib import Path

from . import fao
from .evidence_ledger import canonical


FORMAT = 'legacy-evidence-1'
MAX_BYTES = 8 * 1024 * 1024
DOMAINS = {
    'catalog': 'legacy_catalog_evidence',
    'artist': 'artist_identity_evidence',
    'album': 'album_identity_evidence',
    'playlist': 'playlist_legacy_evidence',
    'recommendation': 'recommendation_legacy_evidence',
    'workflow': 'workflow_legacy_evidence',
    'metadata': 'metadata_legacy_evidence',
    'discovery': 'discovery_identity_evidence',
}
SUBJECTS = {
    'catalog': {'legacy_id': 'legacy_track_refs(legacy_id)', 'entry_id': 'catalog_entries(id)', 'recording_id': 'recordings(id)'},
    'artist': {'artist_id': 'artists(id)'},
    'album': {'album_id': 'albums(id)'},
    'playlist': {'playlist_scope_id': 'playlist_scopes(ref)'},
    'recommendation': {'recording_id': 'recordings(id)'},
    'workflow': {},
    'metadata': {'entry_id': 'catalog_entries(id)', 'recording_id': 'recordings(id)'},
    'discovery': {'recording_id': 'recordings(id)'},
}


def subjects(conn, domain, kind, key, payload):
    """Only exact retained keys establish a subject; names never do."""
    result = {}
    try:
        body = json.loads(payload)
    except (ValueError, TypeError):
        body = None
    if domain == 'catalog' and kind == 'library':
        row = conn.execute('SELECT legacy_id,entry_id,recording_id FROM legacy_track_refs '
                           'WHERE CAST(legacy_id AS TEXT)=?', (key,)).fetchone()
        if row:
            result.update(zip(('legacy_id', 'entry_id', 'recording_id'), row))
    for column, owner in SUBJECTS[domain].items():
        if column in result:
            continue
        table, field = owner[:-1].split('(')
        if table == 'playlist_scopes' and not any(row[1]=='ref' for row in conn.execute('PRAGMA table_info(playlist_scopes)')):
            field = 'id'
        value = body.get(column) if isinstance(body, dict) else None
        if column == 'artist_id' and kind == 'ambiguous_album_artist':
            value = key
        if value is not None:
            row = conn.execute(f'SELECT {field} FROM {table} WHERE {field}=?', (value,)).fetchone()
            if row:
                result[column] = row[0]
    return result


def header(domain):
    if domain not in DOMAINS:
        raise ValueError('Unknown legacy evidence owner')
    return f'TuneConsole {domain} retained evidence 1\n'.encode()


def valid_digest(value):
    return isinstance(value, str) and len(value) == 64 and not set(value) - set('0123456789abcdef')


def read(domain, filename, digest, size, offset, *, kind='original', with_age=False):
    if (not valid_digest(digest) or type(size) is not int or not 0 < size <= MAX_BYTES
            or type(offset) is not int or offset < 0):
        raise ValueError('Invalid retained evidence range')
    path = Path(filename)
    if not valid_digest(path.stem):
        raise ValueError('Invalid retained evidence filename')
    with fao.read(path) as stream:
        age = stream.info
        if offset:
            expected_header = header(domain)
            if offset < len(expected_header) or stream.read(len(expected_header)) != expected_header:
                raise ValueError('Retained evidence pack has a different owner')
            stream.seek(offset)
        encoded = stream.read(size + (not offset))
    if (len(encoded) != size or hashlib.sha256(encoded).hexdigest() != digest
            or not offset and path.stem != digest):
        raise ValueError('Retained evidence content changed')
    value = json.loads(encoded)
    if (not isinstance(value, dict) or set(value) != {'format', 'owner', 'kind', 'value'}
            or value['format'] != FORMAT or value['owner'] != domain or value['kind'] != kind
            or canonical(value) != encoded):
        raise ValueError('Unsupported retained evidence format')
    result = encoded, value['value']
    return (result, age) if with_age else result
