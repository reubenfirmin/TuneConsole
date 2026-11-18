"""Explicit file operations for installed collection backups and restoration."""
import hashlib
import json
from pathlib import Path

from . import collection_backup as codec, fao
from .collection_files import MAX_BYTES, RecoveryFiles, encode, read
from .evidence_ledger import canonical
from .policy_documents import _publish_encoded


class BackupFiles:
    def __init__(self, installed, backup):
        self.installed, self.backup = Path(installed), Path(backup)

    def distinct(self):
        if self.backup.resolve() == self.installed.resolve():
            raise ValueError('Collection backup requires a separate directory')

    def manifest(self, value):
        encoded = codec.encode(value)
        return _publish_encoded(self.backup, encoded, hashlib.sha256(encoded).hexdigest())

    def load(self, path):
        path = Path(path)
        encoded = fao.read(path, max_bytes=MAX_BYTES).bytes
        if hashlib.sha256(encoded).hexdigest() != path.stem:
            raise ValueError('Collection backup manifest does not match its digest')
        document = json.loads(encoded)
        if not isinstance(document, dict) or document.get('format') != codec.FORMAT or canonical(document) != encoded:
            raise ValueError('Unsupported collection backup')
        return document

    def chunk(self, digest):
        return self.load(self.backup / digest[:2] / (digest + '.json'))

    def validate(self, record):
        self.read(record)

    def read(self, record):
        return read(self.backup / record['relative_filename'], record['digest'], record['bytes'],
                    kind=record['kind'], owner=record['id'], with_age=True)

    def copy(self, reference):
        table, identity, kind, filename, digest, size = reference
        value, age = read(filename, digest, size, kind=kind, owner=identity, with_age=True)
        copied = _publish_encoded(self.backup, encode(kind, identity, value), digest, age=age)
        return {'table': table, 'id': identity, 'kind': kind, 'digest': digest, 'bytes': size,
                'relative_filename': str(Path(copied).relative_to(self.backup))}

    def restore(self, record, identity):
        value, age = self.read(record)
        if identity is not None:
            return RecoveryFiles(self.installed).publish_reserved(identity, record['kind'], record['id'], value,
                expected=(record['digest'], record['bytes']), age=age)[0]
        # Explicit backup recovery retains installed historical paths. Runtime
        # publication continues to reserve SQL IDs before writing new files.
        encoded = encode(record['kind'], record['id'], value)
        return _publish_encoded(self.installed, encoded, record['digest'], age=age)
