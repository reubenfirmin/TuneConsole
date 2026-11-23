"""A durable, coalesced notification that recommendation inputs changed."""
from yt_playlist.core.content_preparation_state import ACKNOWLEDGED, FIT_VERSION, REFRESH_REVISION
from yt_playlist.core.recommendation_invalidation import queue_refresh
from yt_playlist.core.worker_settings import number, numbers, read, save
from yt_playlist.repos.base import Repo, synchronized


class RecRefreshRepo(Repo):
    @synchronized
    def version(self):
        return read(self.conn, FIT_VERSION)

    @synchronized
    def save_version(self, version):
        with self.conn.atomic(join=True):
            save(self.conn, FIT_VERSION, version)

    @synchronized
    def complete_fit(self, version, revision):
        with self.conn.atomic(join=True):
            save(self.conn, FIT_VERSION, version)
            self._acknowledge(revision)

    @synchronized
    def revision(self):
        revision, acknowledged = numbers(self.conn, REFRESH_REVISION, ACKNOWLEDGED)
        return revision if revision != acknowledged else None

    @synchronized
    def enqueue(self):
        queue_refresh(self.conn)
        self.conn.commit()

    @synchronized
    def publish_notification(self):
        """Commit this notification, or join the publisher's transaction."""
        with self.conn.atomic(join=True):
            queue_refresh(self.conn)

    @synchronized
    def acknowledge(self, revision):
        """Commit acknowledgement of only the notification captured before this fit."""
        with self.conn.atomic():
            self._acknowledge(revision)

    def _acknowledge(self, revision):
        if revision is not None and number(self.conn, REFRESH_REVISION) == revision:
            save(self.conn, ACKNOWLEDGED, revision)
