"""Database-free, bounded directory work for idle file collection."""
import json
from pathlib import Path

from . import fao, file_collection, metadata_file_contract, metadata_runtime_files


def metadata_owner(root, path):
    """Inspect one legacy envelope; unknown evidence must remain retained."""
    identity = metadata_file_contract.record_id(path)
    if identity is not None:
        return ('receipt', identity)
    try:
        encoded = fao.read(path, max_bytes=metadata_runtime_files.MAX_BYTES).bytes
        body = json.loads(encoded)['body']
        kind, owner, revision = body['kind'], body['owner'], body['revision']
        if not isinstance(owner, list) or len(owner) != (2 if kind == 'alternatives' else 1):
            return None
        if Path(path).resolve() != (root / metadata_runtime_files.relative(kind, owner, revision)).resolve():
            return None
        metadata_runtime_files._verified_data(encoded, kind, owner, revision)
        return kind, owner, revision
    except (FileNotFoundError, ValueError, KeyError, TypeError):
        return None


class Orphans:
    def __init__(self, contracts):
        self.contracts = list(contracts)
        self.position = 0
        self.paths = None

    def candidates(self, now, limit=16):
        root, domain = self.contracts[self.position]
        if self.paths is None:
            self.paths = file_collection.walk_entries(root)
        for _ in range(limit):
            try:
                path = next(self.paths)
            except StopIteration:
                self.close()
                self.position = (self.position + 1) % len(self.contracts)
                break
            if path is None:
                continue
            try:
                info = fao.read(path).info
                if info.ctime_ns / 1e9 <= now - 30 * 86400:
                    yield root, domain, path, info
            except (FileNotFoundError, fao.FileChanged):
                continue

    def owner(self, root, domain, path):
        return metadata_owner(root, path) if domain == 'metadata-runtime' else None

    def collecting(self, root):
        return file_collection.collector(root)

    def remove(self, path, info):
        try:
            return fao.delete(path, expected=info.identity, missing_ok=True)
        except (FileNotFoundError, fao.FileChanged):
            return 0

    def close(self):
        if self.paths is not None:
            self.paths.close()
            self.paths = None


class Pending:
    """Crash-left unpublished bytes; FAO leases protect active stages."""
    def __init__(self, roots):
        self.paths = self._paths(roots)
        self.complete = False

    def _paths(self, roots):
        for root in set(roots):
            yield from file_collection.walk_entries(root, pending=True)

    def step(self, now, limit=16):
        removed = 0
        for _ in range(limit):
            try:
                path = next(self.paths)
            except StopIteration:
                self.complete = True
                break
            if path is None or not path.name.startswith('.fao-pending-'):
                continue
            try:
                if path.stat().st_ctime <= now - 7 * 86400:
                    removed += fao.delete(path, missing_ok=True)
            except FileNotFoundError:
                pass
        return removed

    def close(self):
        self.paths.close()
