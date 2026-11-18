"""Coordinate SQL ownership with database-free file inspection and deletion."""
from yt_playlist.core.managed_file_candidates import Orphans, Pending


class ManagedFileRetention:
    def __init__(self, database, repository, contracts, pending_roots):
        self.database, self.repository = database, repository
        self.contracts, self.pending_roots = contracts, pending_roots

    def maintain(self, now):
        work = getattr(self.database, '_managed_file_orphans', None)
        if work is None:
            work = self.database._managed_file_orphans = Orphans(self.contracts)
        removed = 0
        for root, domain, path, info in work.candidates(now):
            with work.collecting(root) as admitted:
                if not admitted:
                    continue
                owner = work.owner(root, domain, path)
                with self.repository.reservation(domain):
                    if not self.repository.required(domain, path, owner):
                        removed += work.remove(path, info)
        if self.pending_roots:
            pending = getattr(self.database, '_managed_file_pending', None)
            if pending is None:
                pending = self.database._managed_file_pending = Pending(self.pending_roots)
            removed += pending.step(now)
            if pending.complete:
                pending.close()
                del self.database._managed_file_pending
        return removed

    def close(self):
        for name in ('_managed_file_orphans', '_managed_file_pending'):
            work = getattr(self.database, name, None)
            if work is not None:
                work.close()
                delattr(self.database, name)
