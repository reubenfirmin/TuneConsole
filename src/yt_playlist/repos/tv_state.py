"""SQL persistence for TV artwork, measurements, receipts and initial intents."""
from contextlib import contextmanager

from yt_playlist.core import tv_launch_state, tv_recommendation_state as state
from yt_playlist.repos.base import Repo, synchronized, lookup_read


class TVStateRepo(Repo):
    @contextmanager
    def transaction(self):
        with self._lock, self.conn.atomic():
            yield

    @lookup_read
    def artwork_observation(self, key):
        request = self.conn.execute('SELECT id FROM tv_artist_requests WHERE artist_key=?', (key,)).fetchone()
        result = self.conn.execute('SELECT id FROM tv_artist_artwork WHERE artist_key=?', (key,)).fetchone()
        return request[0] if request else None, result[0] if result else None

    @synchronized
    def insert_artwork_request(self, key, name, now, limit):
        state._state(self.conn, 'tv_artwork_request_state', now)
        artist = state._artist(self.conn, name)
        if artist is None:
            return False
        self.conn.execute('INSERT INTO tv_artist_requests(artist_key,display_name,artist_id,requested_at) VALUES (?,?,?,?) '
            'ON CONFLICT(artist_key) DO NOTHING', (key, name, artist, now))
        self.conn.execute('DELETE FROM tv_artist_requests WHERE id NOT IN '
            '(SELECT id FROM tv_artist_requests ORDER BY id DESC LIMIT ?)', (limit,))
        return True

    @synchronized
    def replace_artwork(self, key, value, now, observation, *, request_to_complete, limit):
        request, artwork = self.artwork_observation(key)
        if (observation[0] is not None and request != observation[0]) or artwork != observation[1]:
            return False
        state._state(self.conn, 'tv_artwork_state', now)
        artist = state._artist(self.conn, key)
        self.conn.execute('DELETE FROM tv_artist_artwork WHERE artist_key=?', (key,))
        identity = self.conn.execute('INSERT INTO tv_artist_artwork('
            'artist_key,artist_id,resolution_reason,field_mask,retry_at,at) VALUES (?,?,?,?,?,?) RETURNING id',
            (key, artist, None if artist else 'missing_artist', state._mask(value, state.ARTWORK),
             value.get('retry_at'), value.get('at'))).fetchone()[0]
        self.conn.executemany('INSERT INTO tv_artist_images VALUES (?,?,?)',
            [(identity, position, url) for position, url in enumerate(value.get('images', []))])
        self.conn.execute('DELETE FROM tv_artist_artwork WHERE id NOT IN '
            '(SELECT id FROM tv_artist_artwork ORDER BY id DESC LIMIT ?)', (limit,))
        if request_to_complete is not None:
            self.conn.execute('DELETE FROM tv_artist_requests WHERE artist_key=? AND id=?', (key, request_to_complete))
        return True

    @synchronized
    def replace_visual(self, video, value, now, limit):
        state._state(self.conn, 'tv_visual_state', now)
        self.conn.execute('DELETE FROM tv_visual_results WHERE video_id=?', (video,))
        fields = state.VISUAL[:-1]
        identity = self.conn.execute('INSERT INTO tv_visual_results(video_id,field_mask,' + ','.join(fields) + ') '
            'VALUES (' + ','.join('?' for _ in range(8)) + ') RETURNING id',
            (video, state._mask(value, state.VISUAL), *(value.get(field) for field in fields))).fetchone()[0]
        self.conn.executemany('INSERT INTO tv_visual_samples VALUES (?,?,?)',
            [(identity, position, moment) for position, moment in enumerate(value.get('sample_times', []))])
        self.conn.execute('DELETE FROM tv_visual_results WHERE id NOT IN '
            '(SELECT id FROM tv_visual_results ORDER BY id DESC LIMIT ?)', (limit,))

    @lookup_read
    def static_ids(self, version):
        return {row[0] for row in self.conn.execute(
            "SELECT video_id FROM tv_visual_results WHERE version=? AND verdict='static'", (version,))}

    @lookup_read
    def receipt(self, key):
        return state.receipt(self.conn, key)

    @lookup_read
    def receipt_header(self, key):
        row = self.conn.execute('SELECT id,status,target_stream FROM tv_continuation_receipts WHERE request_key=?', (key,)).fetchone()
        return dict(row) if row else None

    @lookup_read
    def pending_receipts(self):
        return self.conn.execute("SELECT count(*) FROM tv_continuation_receipts WHERE status='pending'").fetchone()[0]

    @synchronized
    def insert_receipt(self, key, address, stream, now):
        source, input_stream, request = address
        resolved = state._playlist(self.conn, source)
        self.conn.execute('INSERT INTO tv_continuation_receipts(request_key,source_playlist_id,source_ref,'
            'source_resolution,input_stream,request_id,status,target_stream,created_at) '
            "VALUES (?,?,?,?,?,?,'pending',?,?)",
            (key, source, resolved, None if resolved else 'missing_playlist', input_stream, request, stream, now))

    @synchronized
    def complete_receipt(self, key, address, value, now, identity, limit):
        source, stream, request = address
        source_ref, target = state._playlist(self.conn, source), state._playlist(self.conn, value['playlist_id'])
        if identity is not None:
            self.conn.execute("UPDATE tv_continuation_receipts SET status='complete',target_playlist_id=?,target_ref=?,"
                'target_resolution=?,built_at=? WHERE id=?',
                (value['playlist_id'], target, None if target else 'missing_playlist', now, identity))
        else:
            identity = self.conn.execute('INSERT INTO tv_continuation_receipts(request_key,source_playlist_id,source_ref,'
                'source_resolution,input_stream,request_id,status,target_playlist_id,target_ref,target_resolution,target_stream,built_at) '
                "VALUES (?,?,?,?,?,?,'complete',?,?,?,?,?) RETURNING id",
                (key, source, source_ref, None if source_ref else 'missing_playlist', stream, request,
                 value['playlist_id'], target, None if target else 'missing_playlist', value['stream'], now)).fetchone()[0]
        state._insert_items(self.conn, 'tv_continuation_items', 'receipt_id', identity, value.get('items', []))
        self.conn.execute("DELETE FROM tv_continuation_receipts WHERE status='complete' AND id NOT IN "
            "(SELECT id FROM tv_continuation_receipts WHERE status='complete' ORDER BY id DESC LIMIT ?)", (limit,))

    @lookup_read
    def launch(self, request_id):
        return tv_launch_state.read(self.conn, request_id)

    @lookup_read
    def pending_launch(self, account_id):
        row = self.conn.execute("SELECT request_id FROM tv_launches WHERE owner_id=? AND current=1 "
            "AND state='pending' LIMIT 1", (account_id,)).fetchone()
        return row[0] if row else None

    @lookup_read
    def pending_launches(self):
        return self.conn.execute("SELECT count(*) FROM tv_launches WHERE state='pending'").fetchone()[0]

    @synchronized
    def insert_launch(self, request_id, account_id, stream, maximum, items, now):
        self.conn.execute('UPDATE tv_launches SET current=0 WHERE owner_id=? AND current=1', (account_id,))
        self.conn.execute('INSERT INTO tv_launches(request_id,account_id,owner_id,stream,max_minutes,created_at) '
            'VALUES (?,?,?,?,?,?)', (request_id, account_id, account_id, stream, maximum, now))
        fields = tv_launch_state.FIELDS
        self.conn.executemany('INSERT INTO tv_launch_videos(request_id,position,video_id,' + ','.join(fields) + ') '
            'VALUES (' + ','.join('?' for _ in range(3 + len(fields))) + ')', [
            (request_id, index, item['video_id'], *(item.get('group' if field == 'source_group' else field)
                for field in fields)) for index, item in enumerate(items)])

    @synchronized
    def set_destination(self, request_id, remote_id, title):
        tv_launch_state.destination(self.conn, request_id, remote_id, title)

    @synchronized
    def set_outcome(self, request_id, values):
        tv_launch_state.outcome(self.conn, request_id, values)

    @synchronized
    def complete_launch(self, request_id):
        self.conn.execute("UPDATE tv_launches SET state='complete' WHERE request_id=?", (request_id,))
