"""SQL persistence for TV artwork, measurements, receipts and initial intents."""
from contextlib import contextmanager

from yt_playlist.core import tv_artwork_state, tv_continuations, tv_launch_actions, tv_recommendation_state as state
from yt_playlist.repos.base import Repo, synchronized, lookup_read


class TVStateRepo(Repo):
    @contextmanager
    def transaction(self):
        with self._lock, self.conn.atomic():
            yield

    @lookup_read
    def artwork_observation(self, key):
        return tv_artwork_state.revision(self.conn, key)

    @synchronized
    def insert_artwork_request(self, key, name, now, limit):
        return tv_artwork_state.request(self.conn, key, name, now, limit=limit)

    @synchronized
    def replace_artwork(self, key, value, now, observation, *, name=None, complete, limit):
        return tv_artwork_state.publish(self.conn, key, value, now, name=name, expected=observation,
                                        complete=complete, limit=limit)

    @synchronized
    def replace_visual(self, video, value, now, limit):
        state.publish_visual(self.conn, video, value, now, limit=limit)

    @lookup_read
    def static_ids(self, version):
        return {video for video, result in (state.read(self.conn, 'tv_visual_checks') or {}).items()
                if result.get('version') == version and result.get('verdict') == 'static'}

    @lookup_read
    def receipt(self, key):
        return state.receipt(self.conn, key)

    @lookup_read
    def receipt_header(self, key):
        return tv_continuations.header(self.conn, key, state._address(key)[0])

    @synchronized
    def complete_receipt(self, key, address, value, now, previous, limit):
        state.write_receipt(self.conn, key, address, value, now, previous, limit)

    @lookup_read
    def launch(self, request_id):
        return tv_launch_actions.read(self.conn, request_id)

    @lookup_read
    def pending_launch(self, account_id):
        return tv_launch_actions.pending(self.conn, account_id)

    @lookup_read
    def pending_launches(self):
        return tv_launch_actions.pending_count(self.conn)

    @synchronized
    def insert_launch(self, request_id, account_id, stream, maximum, items, now, *, current=True):
        tv_launch_actions.insert(self.conn, request_id, account_id, stream, maximum, items, now, current=current)

    @synchronized
    def set_destination(self, request_id, remote_id, title):
        tv_launch_actions.destination(self.conn, request_id, remote_id, title)

    @synchronized
    def set_outcome(self, request_id, values):
        tv_launch_actions.outcome(self.conn, request_id, values)

    @synchronized
    def complete_launch(self, request_id):
        tv_launch_actions.complete(self.conn, request_id)
