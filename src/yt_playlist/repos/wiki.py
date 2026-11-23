"""Stored Wikipedia card facts and explicitly bounded cache writes."""

from yt_playlist.core import wiki_cache
from yt_playlist.repos.base import Repo, synchronized


class WikiRepo(Repo):
    def __init__(self, db):
        super().__init__(db)
        self._path = wiki_cache.path_for(db._db_path) if db._db_path is not None else None
        self._cards = getattr(db.conn, '_wiki_cards', {})
        if self._path is None:
            db.conn._wiki_cards = self._cards
        self._stamp = None

    def _read(self):
        if self._path is None:
            return self._cards
        try:
            stamp = self._path.stat()
        except OSError:
            self._stamp, self._cards = None, wiki_cache.read(self._path)
        else:
            if stamp != self._stamp:
                self._cards = wiki_cache.read(self._path)
                self._stamp = stamp
        return self._cards

    @synchronized
    def get(self, subject) -> dict | None:
        row = self._read().get(subject)
        return dict(row) if row is not None else None

    @synchronized
    def put(self, subject, kind, display, payload, now, *, found, limit) -> None:
        card = dict(subject=subject, kind=kind, display=display, found=int(found), fetched_at=float(now))
        card.update((name, payload.get(name)) for name in ('title', 'extract', 'thumbnail', 'url'))
        if 'matched_genres' in payload:
            card['matched_genres'] = payload['matched_genres']
        # Serialize file updates across independent connections to the same library.
        with self.conn.atomic(join=True):
            cards = {**self._read(), subject: card}
            if 0 <= limit < len(cards):
                retained = sorted(cards, key=lambda key: (-cards[key]['fetched_at'], key))[:limit]
                cards = {key: cards[key] for key in retained}
            if self._path is None:
                self._cards.clear()
                self._cards.update(cards)
            else:
                wiki_cache.write(self._path, cards)
                self._stamp = None
