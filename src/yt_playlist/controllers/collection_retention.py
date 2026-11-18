"""Coordinate one bounded file batch and its SQL ownership scan."""
import sqlite3

from yt_playlist.core.activity import work_budget, WorkBudgetExceeded
from yt_playlist.core.collection_candidates import CollectionCandidates
from yt_playlist.repos.collection_retention import OwnershipScan


class CollectionCollector:
    LIMIT = 32
    BUDGET_S = .05
    OWNER_PAGE = 2048

    def __init__(self, root):
        self.files = CollectionCandidates(root)
        self.owners = OwnershipScan()

    def prepare(self):
        if self.files.prepare(self.LIMIT):
            self.owners.reset()

    def step(self, repository):
        self.prepare()
        if not self.files.batch:
            self.files.batch = None
            return 0
        with repository.reservation() as active:
            if not active:
                return 0
            with self.files.collecting() as admitted:
                if not admitted:
                    return 0
                owned = repository.required(self.owners, [str(path) for path in self.files.batch], self.OWNER_PAGE)
                return self.files.remove(owned)

    def close(self):
        self.files.close()
        self.owners.reset()


class CollectionRetention:
    def __init__(self, database, repository, root):
        self.database, self.repository, self.root = database, repository, root

    def maintain(self):
        if not self.repository.active():
            return 0
        collector = getattr(self.database, '_collection_orphans', None)
        if collector is None:
            collector = self.database._collection_orphans = CollectionCollector(self.root)
        collector.prepare()
        before = collector.files.removed
        try:
            with work_budget(collector.BUDGET_S):
                return collector.step(self.repository)
        except WorkBudgetExceeded:
            return collector.files.removed - before
        except sqlite3.OperationalError as error:
            if getattr(error, 'sqlite_errorcode', 0) & 255 not in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
                raise
            return collector.files.removed - before
        finally:
            if collector.files.complete and collector.files.batch is None:
                self.close()

    def pending(self):
        return getattr(self.database, '_collection_orphans', None) is not None

    def close(self):
        collector = getattr(self.database, '_collection_orphans', None)
        if collector is not None:
            collector.close()
            del self.database._collection_orphans
