"""Stored Wikipedia card facts and explicitly bounded cache writes."""
from yt_playlist.repos.base import Repo, synchronized


class WikiRepo(Repo):
    @synchronized
    def get(self, subject) -> dict | None:
        row = self.conn.execute("SELECT * FROM wiki_cards WHERE subject=?", (subject,)).fetchone()
        return dict(row) if row is not None else None

    @synchronized
    def put(self, subject, kind, display, payload, now, *, found, limit) -> None:
        p = payload
        self.conn.execute(
            "INSERT OR REPLACE INTO wiki_cards"
            "(subject, kind, display, title, extract, thumbnail, url, found, fetched_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (subject, kind, display, p.get("title"), p.get("extract"),
             p.get("thumbnail"), p.get("url"), int(found), float(now)))
        self.conn.execute("DELETE FROM wiki_cards WHERE subject NOT IN "
                          "(SELECT subject FROM wiki_cards ORDER BY fetched_at DESC, subject LIMIT ?)",
                          (limit,))
        self.conn.commit()
