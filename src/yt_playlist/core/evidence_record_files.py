"""Listening evidence access using detached SQL receipts; no database calls."""
import hashlib
from . import fao, policy_documents
from .evidence_ledger import EvidenceLedger
from .evidence_paths import record_id, record_key, record_path, MAX_BYTES


class EvidenceRecordFiles(EvidenceLedger):
    def __init__(self, directory, byte_budget, *, references=None, opportunity_projection=None):
        super().__init__(directory, byte_budget, opportunity_projection=opportunity_projection)
        self.references = references or {}
        self.owned_paths = {record_path(self.directory, kind, reference[0]): reference
                            for (kind, key), reference in self.references.items()}

    def with_references(self, references, *, opportunity_projection=None):
        return type(self)(self.directory, self._byte_budget, references=references,
                          opportunity_projection=opportunity_projection)

    @property
    def byte_budget(self):
        if callable(self._byte_budget):
            self._byte_budget = self._byte_budget()
        return self._byte_budget

    @byte_budget.setter
    def byte_budget(self, value):
        self._byte_budget = value

    def retained(self):
        return policy_documents.retained_reader(self.directory)

    def _path(self, kind, identity):
        reference = self.references.get(record_key(kind, identity))
        return record_path(self.directory, kind, reference[0]) if reference else super()._path(kind, identity)

    def _read_bytes(self, path):
        reference = self.owned_paths.get(path)
        if reference is None:
            return fao.read(path, max_bytes=MAX_BYTES).bytes
        with fao.read(path, max_bytes=MAX_BYTES, digest=reference[1]) as stream:
            encoded = stream.read(reference[2] + 1)
        if len(encoded) != reference[2]:
            raise ValueError('Listening evidence differs from its SQL receipt')
        return encoded

    def _publish(self, path, encoded, kind, identity, *, age=None):
        reference = self.owned_paths.get(path)
        if reference is not None and (hashlib.sha256(encoded).hexdigest(), len(encoded)) != reference[1:]:
            raise ValueError('Listening evidence changed after reservation')
        return super()._publish(path, encoded, kind, identity, age=age)

    def path_kind(self, path):
        if record_id(path) is not None and path not in self.owned_paths:
            return 'orphan'
        return super().path_kind(path)

    def paths(self, kind=None):
        paths = super().paths(kind)
        return [path for path in paths if self.path_kind(path) != 'orphan'] if kind is not None else paths
