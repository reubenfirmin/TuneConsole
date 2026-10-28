"""WikiRepo: cache of Wikipedia summary cards for the 'into recently' Home card.

One row per subject (a transient facet key, e.g. 'artist:khruangbin' or 'genre:shoegaze').
found=1 rows hold a resolved summary; found=0 rows are a negative cache so a subject with no good
Wikipedia page is not re-fetched on every Home load. Rows go stale after a TTL (longer for hits than
misses) and are then refreshed by the recommendation worker.
"""
from collections import OrderedDict

from yt_playlist.repos.base import Repo, synchronized

WIKI_HIT_TTL_D = 30
WIKI_MISS_TTL_D = 7
WIKI_RETRY_S = 60
WIKI_CACHE_LIMIT = 512


class WikiRepo(Repo):
    def __init__(self, db):
        super().__init__(db)
        self._retry_after = OrderedDict()

    @synchronized
    def should_refresh(self, subject, row, now) -> bool:
        return (now >= self._retry_after.get(subject, 0)
                and (row is None or not self.is_fresh(row, now)))

    @synchronized
    def defer(self, subject, now, *, retry_at=0) -> None:
        """Keep content intact; demand retries also respect the vendor's admission deadline."""
        self._retry_after[subject] = max(now + WIKI_RETRY_S, retry_at)
        self._retry_after.move_to_end(subject)
        while len(self._retry_after) > WIKI_CACHE_LIMIT:
            self._retry_after.popitem(last=False)

    @synchronized
    def get(self, subject) -> dict | None:
        row = self.conn.execute("SELECT * FROM wiki_cards WHERE subject=?", (subject,)).fetchone()
        return dict(row) if row is not None else None

    @synchronized
    def put(self, subject, kind, display, payload, now) -> None:
        self._retry_after.pop(subject, None)
        found = 1 if payload else 0
        p = payload or {}
        self.conn.execute(
            "INSERT OR REPLACE INTO wiki_cards"
            "(subject, kind, display, title, extract, thumbnail, url, found, fetched_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (subject, kind, display, p.get("title"), p.get("extract"),
             p.get("thumbnail"), p.get("url"), found, float(now)))
        self.conn.execute("DELETE FROM wiki_cards WHERE subject NOT IN "
                          "(SELECT subject FROM wiki_cards ORDER BY fetched_at DESC, subject LIMIT ?)",
                          (WIKI_CACHE_LIMIT,))
        self.conn.commit()

    def is_fresh(self, row, now) -> bool:
        ttl_d = WIKI_HIT_TTL_D if row["found"] else WIKI_MISS_TTL_D
        return (float(now) - float(row["fetched_at"])) < ttl_d * 86400.0
