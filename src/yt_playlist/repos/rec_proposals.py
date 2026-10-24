"""Last-good prepared recommendations and pre-encoded snapshot publication.

The snapshot publisher owns the transaction for publish_prepared_* methods. Policy
rows belong to rec_ledger and must participate in that same outer transaction.
"""
import json
import uuid

from yt_playlist.repos.base import Repo, synchronized


class RecProposalsRepo(Repo):
    @synchronized
    def claim_album_description(self, identity, credential, daily_limit, now):
        """Atomically count a paid attempt before sending it; crashes remain conservative.

        Paid results are independent of the 128-item Home display projection and
        have no automatic expiry or eviction. One short global lease also keeps
        overlapping workers from requesting the same release twice.
        """
        if self.conn.in_transaction:
            raise RuntimeError('Album copy reservations require their own transaction')
        self.conn.execute('BEGIN IMMEDIATE')
        with self.conn:
            record = self.get_proposals('album_description:' + identity) or {}
            if record.get('description') or (record.get('status') in ('unknown', 'invalid_response')
                                           and record.get('retry_at', 0) > now):
                return {'status': 'cached', 'record': record}
            usage = self.get_proposals('album_description_usage') or {}
            same_key = usage.get('credential') == credential
            if usage.get('lease_until', 0) > now:
                return {'status': 'busy', 'retry_at': usage['lease_until']}
            if same_key and (usage.get('status') == 'auth' or usage.get('retry_at', 0) > now):
                return {'status': usage['status'], 'retry_at': max(now + 3600, usage.get('retry_at', 0))}
            day = int(now // 86400)
            count = usage.get('requests', 0) if usage.get('day') == day else 0
            if count >= daily_limit:
                return {'status': 'daily_limit', 'retry_at': (day + 1) * 86400}
            token = uuid.uuid4().hex
            usage = {**usage, 'day': day, 'requests': count + 1, 'credential': credential,
                     'lease_until': now + 60, 'token': token, 'status': 'pending', 'retry_at': 0}
            self.conn.execute('INSERT INTO rec_proposals(surface,payload,built_at) VALUES (?,?,?) '
                'ON CONFLICT(surface) DO UPDATE SET payload=excluded.payload,built_at=excluded.built_at',
                ('album_description_usage', json.dumps(usage), now))
            return {'status': 'reserved', 'token': token}

    @synchronized
    def finish_album_description(self, identity, token, record, now):
        """Keep successful prose indefinitely; never replace it with a later error."""
        if self.conn.in_transaction:
            raise RuntimeError('Album copy publication requires its own transaction')
        self.conn.execute('BEGIN IMMEDIATE')
        with self.conn:
            current = self.get_proposals('album_description:' + identity) or {}
            usage = self.get_proposals('album_description_usage') or {}
            if not current.get('description') and (record.get('description') or usage.get('token') == token):
                self.conn.execute('INSERT INTO rec_proposals(surface,payload,built_at) VALUES (?,?,?) '
                    'ON CONFLICT(surface) DO UPDATE SET payload=excluded.payload,built_at=excluded.built_at',
                    ('album_description:' + identity, json.dumps(record), now))
            if usage.get('token') == token:
                status = record['status']
                usage.update(status=status, lease_until=0, token=None,
                    retry_at=record.get('retry_at', 0) if status not in ('ready', 'unknown', 'invalid_response') else 0)
                self.conn.execute('UPDATE rec_proposals SET payload=?,built_at=? WHERE surface=?',
                    (json.dumps(usage), now, 'album_description_usage'))

    @synchronized
    def wake_album_descriptions(self, now):
        """New credentials/settings wake missing copy without discarding paid results or counts."""
        notes = self.get_proposals('album_notes') or {}
        for note in notes.values():
            if (note.get('description') or {}).get('kind') != 'generated':
                note['retry_at'] = 0
        self.put_proposals('album_notes', notes, now)
        usage = self.get_proposals('album_description_usage') or {}
        if usage and usage.get('lease_until', 0) <= now:
            usage.update(status='configured', retry_at=0)
            self.put_proposals('album_description_usage', usage, now)

    @synchronized
    def request_album_notes(self, albums, now, *, current_only=False):
        """Coalesce visible-release lookups without losing concurrent requests."""
        cache = self.get_proposals('album_notes') or {}
        queued = self.get_proposals('album_note_requests') or {}
        if current_only:
            albums = tuple(albums)
            visible = {album['browse_id'] for album in albums}
            queued = {browse: item for browse, item in queued.items() if browse in visible}
        for album in albums:
            browse = album['browse_id']
            cached = cache.get(browse, {})
            if ((cached.get('editorial_version') != 6 or cached.get('retry_at', 0) <= now) and album.get('artist')
                    and album.get('title')):
                queued[browse] = {key: album.get(key) for key in ('browse_id', 'artist', 'title', 'year', 'thumbnail')}
        self.put_proposals('album_note_requests', dict(list(queued.items())[-24:]), now)

    @synchronized
    def publish_album_note(self, browse, note, now):
        cache = self.get_proposals('album_notes') or {}
        cache.pop(browse, None)
        cache[browse] = note
        self.put_proposals('album_notes', dict(list(cache.items())[-128:]), now)
        queued = self.get_proposals('album_note_requests') or {}
        queued.pop(browse, None)
        self.put_proposals('album_note_requests', queued, now)

    @synchronized
    def publish_choice_findings(self, report, interests, now, *, bandit):
        """Publish scorecard and serving interpretation at the same source watermark."""
        with self.conn:
            self.conn.executemany(
                'INSERT INTO rec_proposals(surface,payload,built_at) VALUES (?,?,?) '
                'ON CONFLICT(surface) DO UPDATE SET payload=excluded.payload,built_at=excluded.built_at',
                [('listening_choices', json.dumps(report), now),
                 ('choice_interests', json.dumps(interests), now),
                 ('choice_bandit', json.dumps(bandit), now)])

    @synchronized
    def publish_prepared_bundles(self, bundles_json, fresh_json, now) -> None:
        """Write already encoded output; the snapshot publisher owns validation and commit."""
        self.conn.executemany(
            "INSERT INTO rec_proposals(surface,payload,built_at) VALUES (?,?,?) "
            "ON CONFLICT(surface) DO UPDATE SET payload=excluded.payload,built_at=excluded.built_at",
            [("mode_bundles", bundles_json, now), ("fresh_pool", fresh_json, now)])

    @synchronized
    def publish_prepared_home(self, payload, now, *, surface="home_prepared") -> None:
        """Publish pre-encoded Home output; the snapshot publisher owns the transaction."""
        self.conn.execute(
            "INSERT INTO rec_proposals(surface,payload,built_at) VALUES (?,?,?) "
            "ON CONFLICT(surface) DO UPDATE SET payload=excluded.payload,built_at=excluded.built_at",
            (surface, payload, now))

    @synchronized
    def remember_home(self, payload, held_choice, now) -> None:
        with self.conn:
            self.publish_prepared_home(payload, now, surface="home_tuned")
            self.publish_prepared_home(held_choice, now, surface="home_choice")

    @synchronized
    def put_proposals(self, surface, data, now=None) -> None:
        """Materialize a surface's proposals as JSON (the rec worker writes; routes read last-good)."""
        self.conn.execute(
            "INSERT INTO rec_proposals(surface, payload, built_at) VALUES (?,?,?) "
            "ON CONFLICT(surface) DO UPDATE SET payload=excluded.payload, built_at=excluded.built_at",
            (surface, json.dumps(data), now))
        self.conn.commit()

    def get_proposals(self, surface):
        """Last materialized proposals for a surface, or None if never built."""
        payload = self._proposal_json(surface)
        # Large disposable collections are decoded after releasing the serving connection.
        return json.loads(payload) if payload is not None else None

    @synchronized
    def _proposal_json(self, surface):
        from yt_playlist.core.db_revisions import keyed_reads

        with keyed_reads(self._store, "rec_proposals", (surface,)):
            row = self.conn.execute(
                "SELECT payload FROM rec_proposals WHERE surface=?", (surface,)).fetchone()
        return row["payload"] if row else None

