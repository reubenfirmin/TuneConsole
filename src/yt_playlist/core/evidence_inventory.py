"""Restartable retention metadata; immutable evidence remains the source of truth.

Save small prefixes before the worker's deadline. Revalidate every pathname on
each turn, so continuing publications and renewed preparation leases are seen
before deletion. The cache contains no captured model arrays or deletion intent.
"""
import json

from . import fao
from .activity import check_cancellable_work
from .evidence_contract import artifact_components, canonical

SAVE_EVERY = 8


def _metadata(kind, value):
    if kind == 'artifact':
        return list(artifact_components(value))
    if kind == 'opportunity':
        # Keep enough original metadata to apply the current SQL projection on
        # every turn. A prepared legacy menu can acquire a different context.
        result = {key: value[key] for key in
                  ('id', 'context', 'observed_at', 'format', 'precision') if key in value}
        result['at'] = value.get('at')
        if 'options' in value:
            result['options'] = [{'id': option['id']} for option in value['options']]
        return result
    if kind == 'response':
        return {key: value[key] for key in ('id', 'opportunity_id', 'kind', 'observed_at')}
    if kind == 'report':
        return value['report']['at']
    if kind == 'checkpoint':
        from .choice_checkpoints import validate
        identity = value.get('source_context')
        return {'source_context': identity, 'acknowledged': bool(validate(value, identity))}
    return None


class Inventory:
    def __init__(self, ledger, *, progress=None, path=None):
        self.ledger = ledger
        self.progress = progress if progress is not None else {}
        self.path = path or getattr(ledger, 'inventory_path', ledger.directory / '.retention-inventory')
        try:
            saved = json.loads(fao.read(self.path).bytes)
            if not isinstance(saved, dict) or not isinstance(saved.get('entries'), dict):
                raise ValueError('Invalid retention inventory cache')
            self.entries = saved['entries'] if saved.get('schema') == 2 else {}
        except (FileNotFoundError, fao.CorruptFile, ValueError, KeyError, TypeError):
            # This is a reconstructible cache, never proof that evidence can
            # be retired. Damaged required evidence still fails its own read.
            self.entries = {}
        self.progress['files_saved'] = len(self.entries)
        self.dirty = 0

    def _entry(self, key, kind, info):
        entry = self.entries.get(key)
        if entry and entry['identity'] == list(info.identity) and entry.get('kind') == kind:
            if kind == 'opportunity' and 'at' not in entry['data']:
                return None
            return entry
        return None

    def cached(self, path, kind):
        key = str(path.relative_to(self.ledger.directory))
        if key not in self.entries:
            return None
        try:
            entry = self._entry(key, kind, fao.read(path).info)
        except FileNotFoundError:
            return None
        return entry['data'] if entry else None

    def _save(self):
        if self.dirty:
            fao.write(self.path, canonical({'schema': 2, 'entries': self.entries}))
            self.progress['files_saved'] = len(self.entries)
            self.dirty = 0

    def scan(self):
        found = {}
        paths = self.ledger.paths()
        self.progress['files_total'] = len(paths)
        for path in paths:
            check_cancellable_work()
            key = str(path.relative_to(self.ledger.directory))
            try:
                info = fao.read(path).info
                kind = self.ledger.path_kind(path)
                entry = self._entry(key, kind, info)
                if entry is None:
                    value = json.loads(self.ledger._read_bytes(path)) if kind != 'orphan' else None
                    entry = dict(identity=list(info.identity), size=info.logical_bytes,
                                 mtime=info.mtime_ns / 1e9, kind=kind, data=_metadata(kind, value))
                    self.entries[key] = entry
                    self.dirty += 1
                    if self.dirty >= SAVE_EVERY:
                        self._save()
                found[path] = entry
                self.progress['files_checked'] = len(found)
            except FileNotFoundError:
                continue
        stale = set(self.entries) - {str(path.relative_to(self.ledger.directory)) for path in found}
        for key in stale:
            del self.entries[key]
        self.dirty += bool(stale)
        self._save()
        return found
