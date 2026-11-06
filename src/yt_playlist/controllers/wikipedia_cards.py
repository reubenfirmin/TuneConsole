"""Wikipedia card admission and cache policy, independent of SQLite."""
from collections import OrderedDict
from threading import RLock

WIKI_HIT_TTL_D = 30
WIKI_MISS_TTL_D = 7
WIKI_RETRY_S = 60
WIKI_CACHE_LIMIT = 512


class WikipediaCards:
    def __init__(self, repository):
        self.repository = repository
        self._retry_after = OrderedDict()
        self._lock = RLock()

    def get(self, subject):
        return self.repository.get(subject)

    @staticmethod
    def is_fresh(row, now):
        ttl = WIKI_HIT_TTL_D if row['found'] else WIKI_MISS_TTL_D
        return now - float(row['fetched_at']) < ttl * 86400

    def should_refresh(self, subject, row, now):
        with self._lock:
            return (now >= self._retry_after.get(subject, 0)
                    and (row is None or not self.is_fresh(row, now)))

    def defer(self, subject, now, *, retry_at=0):
        with self._lock:
            self._retry_after[subject] = max(now + WIKI_RETRY_S, retry_at)
            self._retry_after.move_to_end(subject)
            while len(self._retry_after) > WIKI_CACHE_LIMIT:
                self._retry_after.popitem(last=False)

    def put(self, subject, kind, display, payload, now):
        with self._lock:
            self.repository.put(subject, kind, display, payload or {}, now,
                                found=bool(payload), limit=WIKI_CACHE_LIMIT)
            self._retry_after.pop(subject, None)
