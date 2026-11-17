"""Collection export/restore commands over SQL ownership and explicit files."""
from pathlib import Path

from yt_playlist.core import collection_backup as plan
from yt_playlist.core.activity import check_cancellable_work
from yt_playlist.core.document_paths import record_id


class CollectionRecovery:
    def __init__(self, repository, files):
        self.repository, self.files = repository, files

    def export(self):
        if not self.repository.pinned:
            raise ValueError('Collection export requires a pinned database snapshot')
        self.files.distinct()
        records, chunks = [], []
        for reference in self.repository.references():
            check_cancellable_work()
            records.append(self.files.copy(reference))
            if len(records) == 512:
                chunks.append(Path(self.files.manifest({'type': 'chunk', 'records': records})).stem)
                records = []
        if records:
            chunks.append(Path(self.files.manifest({'type': 'chunk', 'records': records})).stem)
        return self.files.manifest({'type': 'manifest', 'chunks': chunks})

    def restore(self, manifest):
        records = {}
        for digest in plan.chunks(self.files.load(manifest)):
            check_cancellable_work()
            plan.add_records(records, self.files.chunk(digest))
        for record in records.values():
            self.files.validate(record)
        # Preserve the existing single-owner writer fence through restoration.
        # It prevents replacing a document after its SQL owner changed. This
        # command introduces no shared or cross-repository transaction protocol.
        with self.repository.restoring():
            retained = list(self.repository.references())
            plan.validate_owners(records, retained)
            identities = {}
            for table, owner, kind, filename, digest, size in retained:
                identity = record_id(filename)
                if identity is not None:
                    plan.validate_identity(self.repository.identity(identity), kind=kind, owner=owner,
                                           digest=digest, size=size)
                    identities[table, owner] = identity
            restored = {}
            for key, record in records.items():
                check_cancellable_work()
                restored[key] = self.files.restore(record, identities.get(key))
            self.repository.replace(restored)
