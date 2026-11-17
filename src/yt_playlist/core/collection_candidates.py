"""Database-free inventory, reader admission and collection-file deletion."""
from itertools import islice
from pathlib import Path
import time

from . import fao, file_collection
from .activity import check_cancellable_work
from .collection_files import record_id
from .action_backup_paths import record_id as backup_record_id


class CollectionCandidates:
    def __init__(self, root):
        self.root = Path(root)
        self.paths = file_collection.walk_entries(root, max_depth=2)
        self.batch = None
        self.complete = False
        self.removed = 0

    def prepare(self, limit):
        if self.batch is not None or self.complete:
            return False
        entries = list(islice(self.paths, limit))
        self.complete = len(entries) < limit
        self.batch = [path for path in entries if path is not None and path.suffix == '.json']
        return True

    def collecting(self):
        return file_collection.collector(self.root)

    def remove(self, owned):
        removed = 0
        for path in self.batch:
            check_cancellable_work()
            if str(path) in owned:
                continue
            info = None
            if record_id(path) is not None or backup_record_id(path) is not None:
                try:
                    info = fao.read(path).info
                except FileNotFoundError:
                    continue
                if info.ctime_ns / 1e9 > time.time() - 30 * 86400:
                    continue
            try:
                deleted = fao.delete(path, expected=info.identity if info else None, missing_ok=True)
            except fao.FileChanged:
                continue
            removed += deleted
            self.removed += deleted
        self.batch = None
        return removed

    def close(self):
        self.paths.close()
        self.batch = None
        self.complete = True
