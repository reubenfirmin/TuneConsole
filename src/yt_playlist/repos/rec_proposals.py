"""Last-good prepared recommendations and pre-encoded snapshot publication.

The snapshot publisher owns the transaction for publish_prepared_* methods. Policy
rows belong to rec_ledger and must participate in that same outer transaction.
"""
import json
import uuid

from yt_playlist.core import json_cache
from yt_playlist.repos.base import Repo, synchronized, snapshot_read
from yt_playlist.repos import proposal_cache


def encode_proposals(surface, value):
    if surface not in {'mode_bundles', 'fresh_pool'}:
        return json.dumps(value)
    from yt_playlist.repos.policy_storage import references
    return json_cache.encode(value, group_pools=surface == 'mode_bundles', references=references(value))


class RecProposalsRepo(Repo):
    def group_pools_json(self):
        """Global ranks and framing metadata, without legacy per-mode copies."""
        row = self._group_pools_json()
        if row is None:
            return None
        return json_cache.decode_groups(*row)

    @snapshot_read
    def _group_pools_json(self):
        from yt_playlist.core.db_revisions import keyed_reads

        encoded = proposal_cache.read(self.conn, 'mode_bundles', store=self._store)
        if encoded is not None:
            value = json.loads(encoded)
            if isinstance(value, dict) and value.get(json_cache.MARKER) in (
                    json_cache.FORMAT, json_cache.LEGACY_FORMAT):
                return value.get('groups'), value[json_cache.MARKER], value.get('profiles')
            value = value if isinstance(value, dict) else {}
            return json.dumps({'all': value.get('all', {}), '_meta': value.get('_meta', {})}), None, None
        with keyed_reads(self._store, 'rec_proposals', ('mode_bundles',)):
            return self.conn.execute("SELECT CASE WHEN json_extract(payload,?) IN (?,?) "
                "THEN json_extract(payload,'$.groups') ELSE "
                "json_object('all',COALESCE(json_extract(payload,'$.all'),json('{}')),"
                "'_meta',COALESCE(json_extract(payload,'$._meta'),json('{}'))) END,"
                "json_extract(payload,?),json_extract(payload,'$.profiles') FROM rec_proposals "
                "WHERE surface='mode_bundles' AND json_valid(payload)",
                ('$.' + json_cache.MARKER, json_cache.FORMAT, json_cache.LEGACY_FORMAT,
                 '$.' + json_cache.MARKER)).fetchone()

    @synchronized
    def group_checkpoint(self):
        """Small serving identity without decoding the prepared candidate lists."""
        from yt_playlist.core.db_revisions import keyed_reads

        with keyed_reads(self._store, 'rec_proposals', ('adaptive_group_preview',)):
            row = self.conn.execute("SELECT json_extract(payload,'$.schema'),"
                "json_extract(payload,'$.generation'),"
                "COALESCE(json_array_length(payload,'$.menus.wheelhouse'),0)+"
                "COALESCE(json_array_length(payload,'$.menus.explore'),0)+"
                "COALESCE(json_array_length(payload,'$.menus.comfort'),0)+"
                "COALESCE(json_array_length(payload,'$.menus.temporal'),0) FROM rec_proposals "
                "WHERE surface='adaptive_group_preview' AND json_valid(payload)").fetchone()
        return {'schema': row[0], 'generation': row[1], 'mix_regions': row[2]} if row else None

    @synchronized
    def deepseek_availability(self, credential):
        """Durable access evidence, including the previous single-credential format."""
        waits = [self.get_proposals('deepseek_host') or {},
                 self.get_proposals('deepseek_credential:' + credential) or {}] if credential else []
        usage = self.get_proposals('album_description_usage') or {}
        if usage.get('status') == 'unavailable' or (usage.get('credential') == credential
                and usage.get('status') in ('auth', 'balance', 'rate_limit')):
            waits.append({'status': usage['status'], 'retry_at': usage.get('retry_at', 0)})
        return waits

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
            if usage.get('lease_until', 0) > now:
                return {'status': 'busy', 'retry_at': usage['lease_until']}
            waits = [wait for wait in self.deepseek_availability(credential)
                     if wait.get('status') == 'auth' or wait.get('retry_at', 0) > now]
            if waits:
                return max(waits, key=lambda wait: (wait.get('status') == 'auth', wait.get('retry_at', 0)))
            day = int(now // 86400)
            count = usage.get('requests', 0) if usage.get('day') == day else 0
            if count >= daily_limit:
                return {'status': 'daily_limit', 'retry_at': (day + 1) * 86400}
            # Retain legacy credential failures when replacing the current key.
            if usage.get('credential') and usage.get('status') in ('auth', 'balance', 'rate_limit'):
                self.conn.execute('INSERT INTO rec_proposals(surface,payload,built_at) VALUES (?,?,?) '
                    'ON CONFLICT(surface) DO UPDATE SET payload=excluded.payload,built_at=excluded.built_at',
                    ('deepseek_credential:' + usage['credential'], json.dumps({
                        'status': usage['status'], 'retry_at': usage.get('retry_at', 0)}), now))
            token = uuid.uuid4().hex
            usage = {**usage, 'day': day, 'requests': count + 1, 'credential': credential,
                     'lease_until': now + 60, 'token': token, 'status': 'pending', 'retry_at': 0,
                     'send_authorized': False}
            self.conn.execute('INSERT INTO rec_proposals(surface,payload,built_at) VALUES (?,?,?) '
                'ON CONFLICT(surface) DO UPDATE SET payload=excluded.payload,built_at=excluded.built_at',
                ('album_description_usage', json.dumps(usage), now))
            return {'status': 'reserved', 'token': token}

    @synchronized
    def authorize_album_description_send(self, token, credential, daily_limit, now):
        """Validate the counted reservation after pacing, immediately before transport.

        Day rollover, expiry or takeover must not turn an old reservation into an
        uncounted current-day POST. Reject those admissions without resetting any
        accounting. A valid single-use authorization renews the lease for the
        bounded HTTP attempt, after this transaction has finished.
        """
        if self.conn.in_transaction:
            raise RuntimeError('Album copy authorization requires its own transaction')
        self.conn.execute('BEGIN IMMEDIATE')
        with self.conn:
            usage = self.get_proposals('album_description_usage') or {}
            if (usage.get('token') != token or usage.get('credential') != credential
                    or usage.get('day') != int(now // 86400)
                    or usage.get('lease_until', 0) <= now or usage.get('status') != 'pending'
                    or usage.get('send_authorized') or not 0 < usage.get('requests', 0) <= daily_limit):
                return False
            usage.update(send_authorized=True, lease_until=max(usage['lease_until'], now + 60))
            self.conn.execute('UPDATE rec_proposals SET payload=?,built_at=? WHERE surface=?',
                (json.dumps(usage), now, 'album_description_usage'))
            return True

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
                if status in ('auth', 'balance', 'rate_limit', 'unavailable'):
                    surface = ('deepseek_host' if status == 'unavailable'
                               else 'deepseek_credential:' + usage['credential'])
                    previous = self.get_proposals(surface) or {}
                    wait = {'status': status, 'retry_at': max(record['retry_at'], previous.get('retry_at', 0))}
                    self.conn.execute('INSERT INTO rec_proposals(surface,payload,built_at) VALUES (?,?,?) '
                        'ON CONFLICT(surface) DO UPDATE SET payload=excluded.payload,built_at=excluded.built_at',
                        (surface, json.dumps(wait), now))
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
        # Saving settings does not make an upstream failure disappear. A different
        # credential gets its own admission; request counts and old waits survive.

    @synchronized
    def request_album_notes(self, albums, now, *, current_only=False, visible=False):
        """Coalesce Home demand, retaining explicit visible demand for 15 minutes."""
        if getattr(self._store, 'activity_paused', False):
            return
        cache = self.get_proposals('album_notes') or {}
        queued = self.get_proposals('album_note_requests') or {}
        if current_only:
            albums = tuple(albums)
            current_ids = {album['browse_id'] for album in albums}
            queued = {browse: item for browse, item in queued.items()
                      if browse in current_ids or item.get('requested_until', 0) > now}
        for album in albums:
            browse = album['browse_id']
            cached = cache.get(browse, {})
            if ((cached.get('editorial_version') != 7 or cached.get('retry_at', 0) <= now) and album.get('artist')
                    and album.get('title')):
                requested_until = queued.get(browse, {}).get('requested_until', 0)
                if visible:
                    requested_until = now + 15 * 60
                queued[browse] = {key: album.get(key) for key in ('browse_id', 'artist', 'title', 'year', 'thumbnail')}
                if requested_until:
                    queued[browse]['requested_until'] = requested_until
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
    def publish_choice_findings(self, report, interests, now, *, bandit, group_bandit=None):
        """Atomically publish the report and any changed effective serving inputs."""
        with self.conn:
            previous = self.get_proposals('choice_interests')
            effective = lambda value: {key: item for key, item in value.items() if key not in {'at', 'watermark'}}
            rows = [('listening_choices', json.dumps(report), now),
                    ('choice_bandit', json.dumps(bandit), now)]
            if previous is None or effective(previous) != effective(interests):
                rows.append(('choice_interests', json.dumps(interests), now))
            self.conn.executemany(
                'INSERT INTO rec_proposals(surface,payload,built_at) VALUES (?,?,?) '
                'ON CONFLICT(surface) DO UPDATE SET payload=excluded.payload,built_at=excluded.built_at',
                rows)
            if group_bandit is not None:
                self.conn.execute('INSERT INTO rec_proposals(surface,payload,built_at) VALUES (?,?,?) '
                    'ON CONFLICT(surface) DO UPDATE SET payload=excluded.payload,built_at=excluded.built_at',
                    ('group_choice_bandit', json.dumps(group_bandit), now))

    def stage_prepared_bundles(self, bundles_json, fresh_json):
        """Commit bounded hidden chunks before entering the guarded publication."""
        return (proposal_cache.stage(self, 'mode_bundles', bundles_json),
                proposal_cache.stage(self, 'fresh_pool', fresh_json))

    @synchronized
    def publish_prepared_bundles(self, bundles_json, fresh_json, now) -> None:
        """Switch staged output; legacy encoded callers retain their transaction contract."""
        if any(isinstance(value, proposal_cache.PreparedCache) for value in (bundles_json, fresh_json)):
            if (not isinstance(bundles_json, proposal_cache.PreparedCache)
                    or bundles_json.surface != 'mode_bundles'
                    or not isinstance(fresh_json, proposal_cache.PreparedCache)
                    or fresh_json.surface != 'fresh_pool'):
                raise ValueError('Prepared recommendation surfaces do not match')
            proposal_cache.finish(self.conn, bundles_json, now)
            proposal_cache.finish(self.conn, fresh_json, now)
            return
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

    def put_proposals(self, surface, data, now=None) -> None:
        """Materialize a surface's proposals as JSON (the rec worker writes; routes read last-good)."""
        self._put_proposals_json(surface, encode_proposals(surface, data), now)

    @synchronized
    def _put_proposals_json(self, surface, encoded, now):
        self.conn.execute(
            "INSERT INTO rec_proposals(surface, payload, built_at) VALUES (?,?,?) "
            "ON CONFLICT(surface) DO UPDATE SET payload=excluded.payload, built_at=excluded.built_at",
            (surface, encoded, now))
        self.conn.commit()

    def get_proposals(self, surface):
        """Last materialized proposals for a surface, or None if never built."""
        payload = self._proposal_json(surface)
        # Large disposable collections are decoded after releasing the serving connection.
        return json_cache.decode(payload) if payload is not None else None

    @snapshot_read
    def has_proposals(self, surface):
        """Check dependency presence without reading or decoding a large display pool."""
        from yt_playlist.core.db_revisions import keyed_reads

        if surface in proposal_cache.SURFACES:
            build = proposal_cache.head(self.conn, surface, store=self._store)
            if build is not None:
                return bool(build[3])
        with keyed_reads(self._store, 'rec_proposals', (surface,)):
            return self.conn.execute(
                "SELECT 1 FROM rec_proposals WHERE surface=? AND payload NOT IN ('null','{}')",
                (surface,)).fetchone() is not None

    @snapshot_read
    def _proposal_json(self, surface):
        from yt_playlist.core.db_revisions import keyed_reads

        if surface in proposal_cache.SURFACES:
            encoded = proposal_cache.read(self.conn, surface, store=self._store)
            if encoded is not None:
                return encoded
        with keyed_reads(self._store, "rec_proposals", (surface,)):
            row = self.conn.execute(
                "SELECT payload FROM rec_proposals WHERE surface=?", (surface,)).fetchone()
        return row["payload"] if row else None

