"""TV demand, bounded caches and explicit provider outcome transitions."""
from yt_playlist.core import tv_recommendation_state as state


class TVState:
    ARTWORK_LIMIT = 60
    VISUAL_LIMIT = 240
    RECEIPT_LIMIT = 60
    LAUNCH_LIMIT = 60

    def __init__(self, repository):
        self.repository = repository

    def artwork_observation(self, key):
        return self.repository.artwork_observation(key)

    def request_artwork(self, key, name, now):
        with self.repository.transaction():
            return self.repository.insert_artwork_request(key, name, now, self.ARTWORK_LIMIT)

    def publish_artwork(self, key, value, now, observation, *, name=None, complete):
        state.validate('tv_artist_artwork', {key: value})
        with self.repository.transaction():
            return self.repository.replace_artwork(key, value, now, observation, name=name,
                complete=complete, limit=self.ARTWORK_LIMIT)

    def publish_visual(self, video, value, now):
        state.validate('tv_visual_checks', {video: value})
        with self.repository.transaction():
            self.repository.replace_visual(video, value, now, self.VISUAL_LIMIT)

    def static_ids(self, version):
        return self.repository.static_ids(version)

    def receipt(self, key):
        return self.repository.receipt(key)

    def complete_receipt(self, key, value, now):
        state.validate('tv_continuations', {key: value})
        address = state._address(key)
        with self.repository.transaction():
            previous = self.repository.receipt_header(key)
            if previous and previous['status'] == 'complete':
                raise ValueError('TV receipt has already completed')
            if previous and previous['target_stream'] != value['stream']:
                raise ValueError('TV rollover completion belongs to another intent')
            self.repository.complete_receipt(key, address, value, now, previous, self.RECEIPT_LIMIT)

    def launch(self, request_id):
        return self.repository.launch(request_id)

    def pending_launch(self, account_id):
        return self.repository.pending_launch(account_id)

    def begin_launch(self, request_id, account_id, stream, maximum, items, now, *, current=True):
        with self.repository.transaction():
            if self.repository.launch(request_id) is not None:
                return False
            if not items or len(items) > self.LAUNCH_LIMIT or len({item['video_id'] for item in items}) != len(items):
                raise ValueError('A TV playlist needs a bounded, distinct selection')
            if self.repository.pending_launches() >= self.LAUNCH_LIMIT:
                raise ValueError('Too many unresolved TV launches')
            self.repository.insert_launch(request_id, account_id, stream, maximum, items, now, current=current)
        return True

    def save_launch(self, request_id, *, destination=None, outcome=None):
        with self.repository.transaction():
            if destination is not None:
                self.repository.set_destination(request_id, *destination)
            if outcome is not None:
                self.repository.set_outcome(request_id, outcome)

    def complete_launch(self, request_id, confirmed):
        """The TV workflow supplies the videos proven delivered by its result or read."""
        with self.repository.transaction():
            self.repository.set_outcome(request_id, {'confirmed': confirmed})
            self.repository.complete_launch(request_id)
