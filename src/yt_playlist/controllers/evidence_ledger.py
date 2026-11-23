"""Commit listening evidence identities, publish bytes, then select ready records."""
import hashlib
import json
from contextlib import nullcontext

from yt_playlist.core import evidence_contract, playback_bindings
from yt_playlist.core.choice_checkpoints import validate as validate_checkpoint
from yt_playlist.core.evidence_inventory import Inventory
from yt_playlist.core.evidence_ledger import EvidenceConflict
from yt_playlist.core.evidence_paths import MAX_BYTES, record_key, record_id, maintenance_path
from yt_playlist.core.evidence_record_files import EvidenceRecordFiles
from yt_playlist.core.evidence_retirement import Retirements, BATCH
from yt_playlist.core import fao


class EvidenceLedger:
    def __init__(self, repository, directory, byte_budget, *, opportunity_projection=None, files=None):
        self.repository = repository
        self.files = files if files is not None else EvidenceRecordFiles(directory, byte_budget)
        self.directory = self.files.directory
        self.opportunity_projection = opportunity_projection

    @property
    def byte_budget(self):
        return self.files.byte_budget

    @byte_budget.setter
    def byte_budget(self, value):
        self.files.byte_budget = value

    def project_opportunity(self, value):
        return self.opportunity_projection(value) if self.opportunity_projection else value

    def _view(self, keys=None):
        return self.files.with_references(self.repository.references(keys))

    def _path(self, kind, identity):
        return self._view([record_key(kind, identity)])._path(kind, identity)

    def paths(self, kind):
        return iter(self._view().paths(kind))

    def size(self, path):
        return fao.read(path).info.logical_bytes

    def read(self, path):
        with self.files.retained():
            return self._view()._read_bytes(path)

    def owns(self, kind, identity, path):
        return path == self._path(kind, identity)

    def read_path(self, path, *, opportunity_inventory=None):
        """Read a verified file, or project its retained offer metadata for checkpointing."""
        with self.files.retained():
            identity = record_id(path)
            files = (self.files.with_references(self.repository.reference_for_file(identity))
                     if identity is not None else self.files)
            kind = files.path_kind(path)
            if kind == 'orphan':
                return None
            value = (opportunity_inventory.cached(path, kind)
                     if kind == 'opportunity' and opportunity_inventory is not None else None)
            if value is None or value.get('at') is None:
                value = json.loads(files._read_bytes(path))
            return self.project_opportunity(value) if kind == 'opportunity' else value

    def checkpoint_inventory(self):
        return Inventory(self.files, path=maintenance_path(self.directory, 1, 'inventory'))

    def has_checkpoint(self, identity, inventory):
        with self.files.retained():
            files = self._view([record_key('checkpoint', identity)])
            cached = inventory.cached(files._path('checkpoint', identity), 'checkpoint')
            if cached is not None:
                return cached['source_context'] == identity and cached['acknowledged']
            return validate_checkpoint(files.get('checkpoint', identity), identity) is not None

    def get(self, kind, identity):
        with self.files.retained():
            value = self._view([record_key(kind, identity)]).get(kind, identity)
            return self.project_opportunity(value) if kind == 'opportunity' else value

    def contains(self, kind, identity):
        with self.files.retained():
            return self._view([record_key(kind, identity)]).contains(kind, identity)

    def generation(self):
        return self.files.generation()

    def records(self, kind):
        with self.files.retained():
            values = self._view().records(kind)
            return [projected for value in values if (projected := self.project_opportunity(value)) is not None] \
                if kind == 'opportunity' else values

    def artifact(self, value):
        identity = hashlib.sha256(evidence_contract.canonical(value)).hexdigest()
        self.put('artifact', identity, value)
        return identity

    def put(self, kind, identity, value, *, artifact=None):
        key = record_key(kind, identity)
        records = {key: value}
        if artifact is not None:
            artifact_key = 'artifact', hashlib.sha256(evidence_contract.canonical(artifact)).hexdigest()
            records[artifact_key] = artifact
        dependencies = [('artifact', component) for component in evidence_contract.artifact_components(
            value if kind == 'artifact' else artifact)]
        encoded = {key: evidence_contract.canonical(value) for key, value in records.items()}
        if any(not 0 < len(data) <= MAX_BYTES for data in encoded.values()):
            raise ValueError('Listening evidence exceeds its record byte budget')
        # The existing domain publication lock serializes immutable identity and
        # capacity decisions. No file operation runs inside either SQL unit.
        with self.files._budget() as budget:
            previous = self.repository.references([*records, *dependencies])
            files = self.files.with_references(previous)
            missing = {}
            for record, data in encoded.items():
                path = files._path(*record)
                if path.is_file():
                    if files._read_bytes(path) != data:
                        raise EvidenceConflict(f'Conflicting {record[0]} evidence: {record[1]}')
                else:
                    missing[record] = hashlib.sha256(data).hexdigest(), len(data)
            if key not in missing:
                return files.put(kind, identity, value, artifact=artifact, _budget_state=budget)
            reserved = self.repository.reserve(missing)
            files = self.files.with_references({**previous, **reserved})
            result = files.put(kind, identity, value, artifact=artifact, _budget_state=budget)
            if kind == 'response' and playback_bindings.coordinates(value) is not None:
                self.repository.complete(reserved, previous, playback=value)
            else:
                self.repository.complete(reserved, previous)
            return result

    def maintain(self, *args, pending_protection=None, retire_references=None, progress=None, **kwargs):
        with self.files._budget() as budget:
            files = self._view()
            files.inventory_path = maintenance_path(self.directory, 1, 'inventory')
            files.retirement_path = maintenance_path(self.directory, 1, 'retirements')
            inventory = Inventory(files, progress=progress).scan()
            projected = {entry['data']['id']: self.project_opportunity(entry['data'])
                         for path, entry in inventory.items() if files.path_kind(path) == 'opportunity'}
            files.opportunity_projection = lambda row: projected.get(row['id']) if row is not None else None
            protection = pending_protection() if pending_protection is not None else nullcontext(((), ()))
            with protection as pending:
                result = files.maintain(*args, **kwargs, progress=progress, _budget_state=budget,
                    _inventory=inventory, _defer_reconciliation=True, pending_protection=lambda: nullcontext(pending))
                if '_retirement_offers' not in result:
                    return result
                offers, checkpoints = sorted(result.pop('_retirement_offers')), sorted(result.pop('_retirement_checkpoints'))
                intent = Retirements(self.directory, path=files.retirement_path)
                if intent.files:
                    paths = {reference[0]: path for path, reference in files.owned_paths.items()}
                    self.repository.retire_files({identity for identity in intent.files & paths.keys()
                                                  if not paths[identity].exists()})
                    intent.files.clear()
                    intent.save()
                if not offers and not checkpoints and retire_references is not None:
                    retire_references([], [])
                while offers or checkpoints:
                    batch_offers, batch_checkpoints = offers[:BATCH], checkpoints[:BATCH]
                    if retire_references is not None:
                        retire_references(batch_offers, batch_checkpoints)
                    intent.reconcile(batch_offers, batch_checkpoints, None)
                    del offers[:BATCH]
                    del checkpoints[:BATCH]
                result['deferred'] = bool(intent.offers or intent.checkpoints or result.pop('_unlink_deferred'))
                return result
