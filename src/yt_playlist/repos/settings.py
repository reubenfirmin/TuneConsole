"""SettingsRepo — small key/value app settings (e.g. the Last.fm API key)."""
from yt_playlist.repos.base import Repo, synchronized


class SettingsRepo(Repo):
    @synchronized
    def get_setting(self, key, default=None):
        row = self.conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row is not None else default

    @synchronized
    def set_setting(self, key, value) -> None:
        self.conn.execute("INSERT OR REPLACE INTO settings(key, value) VALUES (?, ?)", (key, value or ""))
        self.conn.commit()

    @synchronized
    def delete_setting(self, key) -> None:
        self.conn.execute("DELETE FROM settings WHERE key=?", (key,))
        self.conn.commit()
