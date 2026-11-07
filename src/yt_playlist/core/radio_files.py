"""Radio checkpoint files, including supported installed publications."""
import hashlib
from pathlib import Path

from . import fao
from .collection_recovery import RecoveryFiles
from .schema_contract import MigrationError


class RadioFiles(RecoveryFiles):
    def read_prepared(self, header, default=None):
        if header is None:
            raise MigrationError('Published Radio checkpoint is missing')
        if not header['source_present']:
            return default
        if header['filename'] is None:
            return None
        with fao.read(header['filename']) as stream:
            encoded = stream.read(header['byte_size'] + 1)
        if (len(encoded) != header['byte_size'] or hashlib.sha256(encoded).hexdigest() != header['digest']
                or Path(header['filename']).stem != header['digest']):
            raise MigrationError('Prepared Radio file does not match its digest')
        return encoded.decode('utf-8')
