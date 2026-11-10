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
                  ('id', 'context', 'observed_at', 'comparison', 'monitor', 'format', 'precision') if key in value}
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
    def __init__(self, ledger, *, progress=None):
        self.ledger = ledger
        self.progress = progress if progress is not None else {}
        self.path = ledger.directory / '.retention-inventory'
        try:
            saved = json.loads(fao.read(self.path).bytes)
            if not isinstance(saved, dict) or not isinstance(saved.get('entries'), dict):
                raise ValueError('Invalid retention inventory cache')
            self.entries = saved['entries'] if saved.get('schema') == 1 else {}
        except (FileNotFoundError, fao.CorruptFile, ValueError, KeyError, TypeError):
            # This is a reconstructible cache, never proof that evidence can
            # be retired. Damaged required evidence still fails its own read.
            self.entries = {}
        self.progress['files_saved'] = len(self.entries)
        self.dirty = 0

    def _save(self):
        if self.dirty:
            fao.write(self.path, canonical({'schema': 1, 'entries': self.entries}))
            self.progress['files_saved'] = len(self.entries)
            self.dirty = 0

    def scan(self):
        found = {}
        paths = sorted(self.ledger.directory.glob('*/*/*.json'))
        self.progress['files_total'] = len(paths)
        for path in paths:
            check_cancellable_work()
            key = str(path.relative_to(self.ledger.directory))
            try:
                info = fao.read(path).info
                entry = self.entries.get(key)
                if entry is None or entry['identity'] != list(info.identity):
                    value = json.loads(fao.read(path).bytes)
                    entry = dict(identity=list(info.identity), size=info.logical_bytes,
                                 mtime=info.mtime_ns / 1e9, data=_metadata(path.parts[-3], value))
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
