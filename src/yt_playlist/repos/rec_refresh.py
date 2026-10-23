"""A durable, coalesced notification that recommendation inputs changed."""
from yt_playlist.repos.base import Repo, synchronized

VERSION = "1"


class RecRefreshRepo(Repo):
    @synchronized
    def revision(self):
        row = self.conn.execute("SELECT revision FROM rec_change_queue WHERE kind='refresh' AND item=''").fetchone()
        return row[0] if row else None

    @synchronized
    def has_pending_changes(self):
        return (self.revision() is not None
                or self._store.settings.get_setting("rec_refresh_version") != VERSION)

    @synchronized
    def enqueue(self):
        self.conn.execute("INSERT INTO rec_change_queue(kind,item) VALUES ('refresh','') "
                          "ON CONFLICT(kind,item) DO UPDATE SET revision=revision+1")
        self.conn.commit()

    @synchronized
    def publish_notification(self):
        """Retain source changes discovered by a guarded content capture, atomically."""
        if not self.conn.in_transaction:
            raise RuntimeError('Refresh notification requires an outer transaction')
        self.conn.execute("INSERT INTO rec_change_queue(kind,item) VALUES ('refresh','') "
                          "ON CONFLICT(kind,item) DO UPDATE SET revision=revision+1")

    @synchronized
    def acknowledge(self, revision):
        """Clear only the notification captured before this fit, in its publication transaction."""
        if not self.conn.in_transaction:
            raise RuntimeError("Refresh acknowledgement requires an outer transaction")
        self.conn.execute("DELETE FROM rec_change_queue WHERE kind='refresh' AND item='' AND revision=?",
                          (revision,))
