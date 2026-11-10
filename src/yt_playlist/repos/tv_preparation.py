"""TV preparation outcome; replacing a display pool cannot erase this state."""
from yt_playlist.core import recommendation_work
from .base import Repo, lookup_read, synchronized


class TVPreparationRepo(Repo):
    @lookup_read
    def available(self):
        return recommendation_work.available(self.conn)

    @lookup_read
    def state(self):
        return recommendation_work.read(self.conn, 'tv_preparation')

    @synchronized
    def record(self, value, row, now):
        with self.conn.atomic():
            if recommendation_work.prepared_owner(self.conn):
                import json
                self.conn.execute('INSERT INTO rec_proposals(surface,payload,built_at) VALUES (?,?,?) '
                    'ON CONFLICT(surface) DO UPDATE SET payload=excluded.payload,built_at=excluded.built_at',
                    ('tv_preparation', json.dumps(value), now))
            else:
                self.conn.execute('INSERT INTO tv_preparation_state VALUES (1,?,?,?) ON CONFLICT(id) DO UPDATE SET '
                    'failed=excluded.failed,observed_at=excluded.observed_at,updated_at=excluded.updated_at', row)
