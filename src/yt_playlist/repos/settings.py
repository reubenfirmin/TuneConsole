"""SettingsRepo: small key/value app settings (e.g. the Last.fm API key)."""
from yt_playlist.repos.base import Repo, synchronized
from yt_playlist.core.db_revisions import setting_reads


class SettingsRepo(Repo):
    @synchronized
    def publish_values(self, values):
        """Publish serialized settings in the caller's atomic transaction."""
        if not self.conn.in_transaction:
            raise RuntimeError('Prepared settings require an outer transaction')
        self.conn.executemany('INSERT INTO settings(key,value) VALUES (?,?) '
            'ON CONFLICT(key) DO UPDATE SET value=excluded.value WHERE value IS NOT excluded.value',
            values.items())

    @synchronized
    def get_setting(self, key, default=None):
        with setting_reads(self._store, (key,)):
            row = self.conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row is not None else default

    @synchronized
    def set_setting(self, key, value) -> None:
        self.conn.execute("INSERT INTO settings(key,value) VALUES (?,?) ON CONFLICT(key) "
                          "DO UPDATE SET value=excluded.value WHERE value IS NOT excluded.value",
                          (key, value or ""))
        self.conn.commit()

    @synchronized
    def delete_setting(self, key) -> None:
        self.conn.execute("DELETE FROM settings WHERE key=?", (key,))
        self.conn.commit()
