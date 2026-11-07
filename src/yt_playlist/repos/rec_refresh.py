"""A durable, coalesced notification that recommendation inputs changed."""
from yt_playlist.repos.base import Repo, synchronized

class RecRefreshRepo(Repo):
    @synchronized
    def revision(self):
        row = self.conn.execute("SELECT revision FROM rec_change_queue WHERE kind='refresh' AND item=''").fetchone()
        return row[0] if row else None

    @synchronized
    def enqueue(self):
        self.conn.execute("INSERT INTO rec_change_queue(kind,item) VALUES ('refresh','') "
                          "ON CONFLICT(kind,item) DO UPDATE SET revision=revision+1")
        self.conn.commit()

    @synchronized
    def publish_notification(self):
        """Commit this notification, or join a legacy publisher of this row."""
        with self.conn.atomic(join=True):
            self.conn.execute("INSERT INTO rec_change_queue(kind,item) VALUES ('refresh','') "
                              "ON CONFLICT(kind,item) DO UPDATE SET revision=revision+1")

    @synchronized
    def acknowledge(self, revision):
        """Commit acknowledgement of only the notification captured before this fit."""
        with self.conn.atomic():
            self.conn.execute("DELETE FROM rec_change_queue WHERE kind='refresh' AND item='' AND revision=?",
                              (revision,))
