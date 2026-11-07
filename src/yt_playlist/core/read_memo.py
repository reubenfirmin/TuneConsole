"""Bounded reuse of detached read results against their actual SQLite dependencies."""
from collections import OrderedDict
from copy import deepcopy
from threading import Lock

from yt_playlist.core.db_revisions import DERIVED_TABLES


class ReadMemo:
    def __init__(self, limit=32):
        self._entries = OrderedDict()
        self._lock = Lock()
        self._limit = limit

    def calculate(self, name, prepare, *, epoch, serial, unchanged, reads, tables, views):
        if epoch is None:
            return prepare()
        with self._lock:
            entry = self._entries.get(name)
        if entry is not None:
            previous_epoch, previous_serial, dependencies, value = entry
            # Never reuse a newer reader's result in an older pinned view. Seek
            # consumed inputs outside the memo lock to avoid lock-order cycles.
            if (previous_epoch == epoch and previous_serial <= serial
                    # Equal serials describe the same pinned inputs. Later
                    # writer changes cannot change either reader's snapshot.
                    and (previous_serial == serial or unchanged(dependencies, previous_serial))):
                reads.include(dependencies)
                with self._lock:
                    if self._entries.get(name) is entry:
                        self._entries.move_to_end(name)
                return deepcopy(value)
        # Calculation and database reads must not hold a shared cache/serving lock.
        with reads.capture() as dependencies:
            value = prepare()
        # Unknown tables cannot be tracked safely. View definitions are protected
        # by the schema epoch; their underlying table reads are also captured.
        if any(table not in tables and table not in views and table not in DERIVED_TABLES
               and not table.startswith("sqlite_")
               for table, _ in dependencies):
            return value
        detached = deepcopy(value)
        with self._lock:
            self._entries[name] = (epoch, serial, frozenset(dependencies), detached)
            self._entries.move_to_end(name)
            while len(self._entries) > self._limit:
                self._entries.popitem(last=False)
        return value
