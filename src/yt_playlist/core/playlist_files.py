"""Saved playlist and canvas file access, including installed original parts."""
import hashlib

from . import recommendation_outputs
from .collection_recovery import RecoveryFiles
from .schema_contract import MigrationError


class PlaylistFiles(RecoveryFiles):
    def read_original(self, header, parts):
        if len(parts) != header['part_count'] or [row['position'] for row in parts] != list(range(len(parts))):
            raise MigrationError('Prepared collection original has incomplete parts')
        values = [recommendation_outputs.read(row['filename'], row['content_digest'], row['byte_size'],
            surface=header['source'], chunk_key='original:' + str(row['position'])) for row in parts]
        value = None if header['source_kind'] == 'null' else ''.join(values)
        if value is None and values != [None]:
            raise MigrationError('Prepared collection original has an invalid null source')
        raw = value.encode() if value is not None else b''
        if len(raw) != header['raw_size'] or hashlib.sha256(raw).hexdigest() != header['raw_digest']:
            raise MigrationError('Prepared collection original differs from its captured bytes')
        return value
