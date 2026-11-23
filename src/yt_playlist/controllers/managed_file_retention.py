"""Coordinate SQL ownership with database-free file inspection and deletion."""
from yt_playlist.core import fao, file_map
from yt_playlist.core.managed_file_candidates import Orphans, Pending


class ManagedFileRetention:
    def __init__(self, database, repository, contracts, pending_roots, *, legacy_playback=None):
        self.database, self.repository = database, repository
        self.contracts, self.pending_roots = contracts, pending_roots
        self.legacy_playback = legacy_playback

    def maintain(self, now):
        work = getattr(self.database, '_managed_file_orphans', None)
        if work is None:
            work = self.database._managed_file_orphans = Orphans(self.contracts)
        removed = 0
        for root, domain, path, info in work.candidates(now):
            if domain == 'legacy-playback':
                # Startup adopts these bodies under the same file fence.
                with work.collecting(root) as admitted:
                    if admitted and not self.legacy_playback.exists():
                        removed += work.remove(path, info)
                continue
            if domain == 'retired-archive-journals':
                removed += self._collect_archive_page(work, root, domain, path, info)
                continue
            # Policy publishers hold the serving writer while pinning files.
            # Reserve the writer before attempting the nonblocking file fence.
            with self.repository.reservation(domain), work.collecting(root) as admitted:
                if not admitted:
                    continue
                owner = work.owner(root, domain, path)
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

    def _collect_archive_page(self, work, root, domain, path, info):
        if path.relative_to(root).parts[0] != 'workflow':
            with work.collecting(root) as admitted:
                return work.remove(path, info) if admitted else 0
        with self.repository.reservation(domain), work.collecting(root) as admitted:
            if not admitted:
                return 0
            publication = self.repository.workflow_publication()
            try:
                if publication is not None and file_map.contains_page(
                        root / 'workflow', publication, path.stem, file_map.Budget()):
                    return 0
            except (FileNotFoundError, fao.FileBudgetExceeded):
                return 0
            return work.remove(path, info)

    def close(self):
        for name in ('_managed_file_orphans', '_managed_file_pending'):
            work = getattr(self.database, name, None)
            if work is not None:
                work.close()
                delattr(self.database, name)
