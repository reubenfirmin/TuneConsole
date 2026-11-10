"""TV demand, bounded caches and explicit provider outcome transitions."""
from yt_playlist.core import tv_recommendation_state as state


class TVState:
    ARTWORK_LIMIT = 60
    VISUAL_LIMIT = 240
    RECEIPT_LIMIT = 60
    LAUNCH_LIMIT = 60

    def __init__(self, repository, proposals, *, installed):
        self.repository = repository
        self.proposals = proposals
        self.installed = installed

    def _remember(self, surface, key, value, limit, now):
        cache = self.proposals.get_proposals(surface) or {}
        cache.pop(key, None)
        cache[key] = value
        self.proposals.put_proposals(surface, dict(list(cache.items())[-limit:]), now)

    def artwork_observation(self, key):
        return self.repository.artwork_observation(key) if self.installed else (None, None)

    def request_artwork(self, key, name, now):
        if self.installed:
            with self.repository.transaction():
                return self.repository.insert_artwork_request(key, name, now, self.ARTWORK_LIMIT)
        requests = self.proposals.get_proposals('tv_artist_artwork_requests') or {}
        if key not in requests:
            requests[key] = name
            self.proposals.put_proposals('tv_artist_artwork_requests',
                dict(list(requests.items())[-self.ARTWORK_LIMIT:]), now)
        return True

    def publish_artwork(self, key, value, now, observation, *, complete):
        if self.installed:
            state.validate('tv_artist_artwork', {key: value})
            with self.repository.transaction():
                return self.repository.replace_artwork(key, value, now, observation,
                    request_to_complete=observation[0] if complete else None, limit=self.ARTWORK_LIMIT)
        self._remember('tv_artist_artwork', key, value, self.ARTWORK_LIMIT, now)
        if complete:
            requests = self.proposals.get_proposals('tv_artist_artwork_requests') or {}
            requests.pop(key, None)
            self.proposals.put_proposals('tv_artist_artwork_requests', requests, now)
        return True

    def publish_visual(self, video, value, now):
        if self.installed:
            state.validate('tv_visual_checks', {video: value})
            with self.repository.transaction():
                self.repository.replace_visual(video, value, now, self.VISUAL_LIMIT)
        else:
            self._remember('tv_visual_checks', video, value, self.VISUAL_LIMIT, now)

    def static_ids(self, version):
        if self.installed:
            return self.repository.static_ids(version)
        return {video for video, value in (self.proposals.get_proposals('tv_visual_checks') or {}).items()
                if isinstance(value, dict) and value.get('version') == version and value.get('verdict') == 'static'}

    def receipt(self, key):
        if self.installed:
            return self.repository.receipt(key)
        return (self.proposals.get_proposals('tv_continuations') or {}).get(key)

    def begin_rollover(self, key, stream, now):
        if not self.installed:
            return True
        address = state._address(key)
        with self.repository.transaction():
            if self.repository.receipt_header(key):
                return False
            if self.repository.pending_receipts() >= self.RECEIPT_LIMIT:
                raise ValueError('Too many unconfirmed TV playlist creations')
            self.repository.insert_receipt(key, address, stream, now)
        return True

    def complete_receipt(self, key, value, now):
        if not self.installed:
            receipts = self.proposals.get_proposals('tv_continuations') or {}
            receipts[key] = value
            self.proposals.put_proposals('tv_continuations',
                dict(list(receipts.items())[-self.RECEIPT_LIMIT:]), now)
            return
        state.validate('tv_continuations', {key: value})
        address = state._address(key)
        with self.repository.transaction():
            previous = self.repository.receipt_header(key)
            if previous and previous['status'] == 'complete':
                raise ValueError('TV receipt has already completed')
            if previous and previous['target_stream'] != value['stream']:
                raise ValueError('TV rollover completion belongs to another intent')
            self.repository.complete_receipt(key, address, value, now,
                previous['id'] if previous else None, self.RECEIPT_LIMIT)

    def launch(self, request_id):
        return self.repository.launch(request_id)

    def pending_launch(self, account_id):
        return self.repository.pending_launch(account_id)

    def begin_launch(self, request_id, account_id, stream, maximum, items, now):
        with self.repository.transaction():
            if self.repository.launch(request_id) is not None:
                return False
            if not items or len(items) > self.LAUNCH_LIMIT or len({item['video_id'] for item in items}) != len(items):
                raise ValueError('An initial TV launch needs a bounded, distinct selection')
            if self.repository.pending_launches() >= self.LAUNCH_LIMIT:
                raise ValueError('Too many unresolved TV launches')
            self.repository.insert_launch(request_id, account_id, stream, maximum, items, now)
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
