"""TV preparation outcome; replacing a display pool cannot erase this state."""
from contextlib import nullcontext

from yt_playlist.core import recommendation_work
from .base import Repo, lookup_read, synchronized


class TVPreparationRepo(Repo):
    @lookup_read
    def state(self):
        if recommendation_work.available(self.conn):
            return recommendation_work.read(self.conn, 'tv_preparation')
        return self._store.rec_proposals.get_proposals('tv_preparation')

    @synchronized
    def record(self, failed, now):
        value = {'failed': failed, 'at': now}
        if recommendation_work.available(self.conn):
            unit = nullcontext() if self.conn.in_transaction else self.conn.atomic()
            with unit:
                recommendation_work.write(self.conn, 'tv_preparation', value, now)
        else:
            self._store.rec_proposals.put_proposals('tv_preparation', value, now)
