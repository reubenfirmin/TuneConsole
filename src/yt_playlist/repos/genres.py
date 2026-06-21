"""GenreRepo — the editable genre whitelist plus the distinct genres collected from tracks."""
from yt_playlist.repos.base import Repo, synchronized


class GenreRepo(Repo):
    @synchronized
    def get_genre_whitelist(self) -> list:
        rows = self.conn.execute("SELECT name FROM genre_whitelist").fetchall()
        return sorted((r["name"] for r in rows), key=str.lower)

    @synchronized
    def set_genres(self, names) -> None:
        with self.conn:
            self.conn.execute("DELETE FROM genre_whitelist")
            self.conn.executemany("INSERT OR IGNORE INTO genre_whitelist(name) VALUES (?)",
                                  [(n,) for n in names])

    @synchronized
    def add_genre(self, name) -> None:
        self.conn.execute("INSERT OR IGNORE INTO genre_whitelist(name) VALUES (?)", (name,))
        self.conn.commit()

    @synchronized
    def remove_genre(self, name) -> None:
        self.conn.execute("DELETE FROM genre_whitelist WHERE name=?", (name,))
        self.conn.commit()

    @synchronized
    def all_genres(self) -> list:
        """Every distinct non-blank genre we've collected, case-insensitively alpha-sorted."""
        rows = self.conn.execute(
            "SELECT DISTINCT genre FROM tracks WHERE genre IS NOT NULL AND genre <> ''").fetchall()
        return sorted((r["genre"] for r in rows), key=str.lower)
