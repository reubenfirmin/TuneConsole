"""Last-good prepared recommendations and pre-encoded snapshot publication.

The snapshot publisher owns the transaction for publish_prepared_* methods. Policy
rows belong to rec_ledger and must participate in that same outer transaction.
"""
import json
import uuid
from contextlib import nullcontext

from yt_playlist.core import (album_note_outputs, album_note_requests, choice_learning_state, home_outputs,
                              json_cache, recommendation_reports, tv_recommendation_state)
from yt_playlist.repos.base import Repo, synchronized, snapshot_read, lookup_read
from yt_playlist.repos import proposal_cache, proposal_storage


def encode_proposals(surface, value):
    if surface not in {'mode_bundles', 'fresh_pool'}:
        return json.dumps(value)
    from yt_playlist.repos.policy_storage import references
    return json_cache.encode(value, group_pools=surface == 'mode_bundles', references=references(value))


class RecProposalsRepo(Repo):
    @snapshot_read
    def tv_artwork_observation(self, key):
        """Capture the request and result revisions before optional provider I/O."""
        if not self._store._tv_recommendations:
            return None, None
        request = self.conn.execute('SELECT id FROM tv_artist_requests WHERE artist_key=?', (key,)).fetchone()
        result = self.conn.execute('SELECT id FROM tv_artist_artwork WHERE artist_key=?', (key,)).fetchone()
        return request[0] if request else None, result[0] if result else None

    @synchronized
    def request_tv_artwork(self, key, name, now):
        with self.conn.atomic():
            if self._store._tv_recommendations:
                return tv_recommendation_state.request_artwork(self.conn, key, name, now)
            requests = self.get_proposals('tv_artist_artwork_requests') or {}
            if key not in requests:
                requests[key] = name
                self.put_proposals('tv_artist_artwork_requests', dict(list(requests.items())[-60:]), now)
            return True

    @synchronized
    def publish_tv_artwork(self, key, value, now, observation, *, complete):
        with self.conn.atomic():
            if self._store._tv_recommendations:
                return tv_recommendation_state.publish_artwork(self.conn, key, value, now,
                    expected_request=observation[0], expected_artwork=observation[1], complete=complete)
            cache = self.get_proposals('tv_artist_artwork') or {}
            cache.pop(key, None)
            cache[key] = value
            self.put_proposals('tv_artist_artwork', dict(list(cache.items())[-60:]), now)
            if complete:
                requests = self.get_proposals('tv_artist_artwork_requests') or {}
                requests.pop(key, None)
                self.put_proposals('tv_artist_artwork_requests', requests, now)
            return True

    @synchronized
    def publish_tv_visual(self, video, value, now):
        with self.conn.atomic():
            if self._store._tv_recommendations:
                tv_recommendation_state.publish_visual(self.conn, video, value, now)
                return
            cache = self.get_proposals('tv_visual_checks') or {}
            cache.pop(video, None)
            cache[video] = value
            self.put_proposals('tv_visual_checks', dict(list(cache.items())[-240:]), now)

    @snapshot_read
    def tv_static_ids(self, version):
        if self._store._tv_recommendations:
            return {row[0] for row in self.conn.execute(
                "SELECT video_id FROM tv_visual_results WHERE version=? AND verdict='static'", (version,))}
        return {video for video, value in (self.get_proposals('tv_visual_checks') or {}).items()
                if isinstance(value, dict) and value.get('version') == version and value.get('verdict') == 'static'}

    @snapshot_read
    def tv_receipt(self, key):
        if self._store._tv_recommendations:
            return tv_recommendation_state.receipt(self.conn, key)
        return (self.get_proposals('tv_continuations') or {}).get(key)

    @lookup_read
    def tv_launch(self, request_id):
        from yt_playlist.core import tv_launch_state
        return tv_launch_state.read(self.conn, request_id)

    @lookup_read
    def pending_tv_launch(self, account_id):
        row = self.conn.execute("SELECT request_id FROM tv_launches WHERE owner_id=? AND current=1 "
                                "AND state='pending' LIMIT 1", (account_id,)).fetchone()
        return row[0] if row else None

    def begin_tv_launch(self, request_id, account_id, stream, maximum, items, now):
        from yt_playlist.core import tv_launch_state
        from yt_playlist.core.db_writes import atomic_write
        with atomic_write(self._store):
            return tv_launch_state.begin(self.conn, request_id, account_id, stream, maximum, items, now)

    def save_tv_launch(self, request_id, *, destination=None, outcome=None, complete=False):
        from yt_playlist.core import tv_launch_state
        from yt_playlist.core.db_writes import atomic_write
        with atomic_write(self._store):
            if destination is not None:
                tv_launch_state.destination(self.conn, request_id, *destination)
            if outcome is not None:
                tv_launch_state.outcome(self.conn, request_id, outcome)
            if complete:
                tv_launch_state.complete(self.conn, request_id)

    @synchronized
    def begin_tv_rollover(self, key, stream, now):
        if not self._store._tv_recommendations:
            return True
        with self.conn.atomic():
            return tv_recommendation_state.begin_rollover(self.conn, key, stream, now)

    @synchronized
    def complete_tv_receipt(self, key, value, now):
        with self.conn.atomic():
            if self._store._tv_recommendations:
                tv_recommendation_state.complete_receipt(self.conn, key, value, now)
                return
            receipts = self.get_proposals('tv_continuations') or {}
            receipts[key] = value
            self.put_proposals('tv_continuations', dict(list(receipts.items())[-60:]), now)

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

    @lookup_read
    def group_checkpoint(self):
        """Small serving identity without decoding the prepared candidate lists."""
        from yt_playlist.core.db_revisions import keyed_reads
        from yt_playlist.core.preparation import home_publication

        with keyed_reads(self._store, 'rec_proposals', ('adaptive_group_preview',)):
            handled, header = home_publication.header(self.conn, 'adaptive_group_preview')
            if handled:
                return dict(zip(('schema', 'generation', 'mix_regions'),
                    (header['schema_version'], header['edition_key'], header['mix_regions']))) if header is not None else None
        if self._store._home_display_files:
            return home_outputs.checkpoint(self.conn)
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
        if getattr(self._store, 'activity_paused', False) or getattr(self._store, '_read_only', False):
            return
        if self._store._album_note_demand:
            with nullcontext() if self.conn.in_transaction else self.conn.atomic():
                cache = self.get_proposals('album_notes') or {}
                album_note_requests.request(self.conn, albums, cache, now,
                                             current_only=current_only, visible=visible)
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
    def publish_album_note(self, browse, note, now, *, expected_request=None):
        if getattr(self._store, 'activity_paused', False) or getattr(self._store, '_read_only', False):
            return False
        if self._store._album_note_demand:
            with nullcontext() if self.conn.in_transaction else self.conn.atomic():
                if expected_request is not None and not album_note_requests.matches(self.conn, browse, expected_request):
                    return False
                album_note_outputs.publish(self.conn, browse, note, now)
                album_note_requests.complete(self.conn, browse, expected_request)
            return True
        cache = self.get_proposals('album_notes') or {}
        cache.pop(browse, None)
        cache[browse] = note
        self.put_proposals('album_notes', dict(list(cache.items())[-128:]), now)
        queued = self.get_proposals('album_note_requests') or {}
        queued.pop(browse, None)
        self.put_proposals('album_note_requests', queued, now)

    @snapshot_read
    def next_album_note_request(self):
        if self._store._album_note_demand:
            return album_note_requests.next_request(self.conn)
        queued = self.get_proposals('album_note_requests') or {}
        return (next(iter(queued.values())), None) if queued else (None, None)

    @synchronized
    def publish_choice_findings(self, report, interests, now, *, bandit, group_bandit=None):
        """Atomically publish the report and any changed effective serving inputs."""
        with nullcontext() if self.conn.in_transaction else self.conn.atomic():
            previous = self.get_proposals('choice_interests')
            effective = lambda value: {key: item for key, item in value.items() if key not in {'at', 'watermark'}}
            self.publish_prepared_home(json.dumps(report), now, surface='listening_choices')
            self.publish_prepared_home(json.dumps(bandit), now, surface='choice_bandit')
            if previous is None or effective(previous) != effective(interests):
                self.publish_prepared_home(json.dumps(interests), now, surface='choice_interests')
            if group_bandit is not None:
                self.publish_prepared_home(json.dumps(group_bandit), now, surface='group_choice_bandit')

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
    def publish_prepared_home(self, payload, now, *, surface="home_prepared", input_revision=None) -> None:
        """Publish pre-encoded Home output; the snapshot publisher owns the transaction."""
        proposal_storage.publish(self._store, surface, payload, now, input_revision=input_revision)

    def put_proposals(self, surface, data, now=None) -> None:
        """Materialize a surface's proposals as JSON (the rec worker writes; routes read last-good)."""
        self.put_encoded(surface, encode_proposals(surface, data), now)

    @synchronized
    def put_encoded(self, surface, encoded, now):
        with self.conn:
            proposal_storage.publish(self._store, surface, encoded, now, standalone=True)

    def get_proposals(self, surface):
        """Last materialized proposals for a surface, or None if never built."""
        payload = self._proposal_json(surface)
        # Large disposable collections are decoded after releasing the serving connection.
        return json_cache.decode(payload) if payload is not None else None

    def proposal_fields(self, surface, fields):
        """Read a small prepared header without restoring unrelated song profiles."""
        payload = self._proposal_json(surface)
        return json_cache.decode(payload, keys=set(fields)) if payload is not None else None

    @snapshot_read
    def choice_refresh_inputs(self):
        """Read current choice identities and decay evidence through their installed owner."""
        surfaces = ('choice_interests', 'choice_bandit')
        if self._store._choice_learning:
            return tuple(choice_learning_state.refresh_input(self.conn, surface, store=self._store) or {}
                         for surface in surfaces)
        return tuple(self.get_proposals(surface) or {} for surface in surfaces)

    def fresh_candidates(self):
        """Project the existing ranked Fresh inputs before restoring musical profiles."""
        def select(value):
            return {mode: {'fresh': [{name: item[name] for name in
                    ('key', 'video_id', 'title', 'artist', 'album', 'thumbnail', 'genre', 'score') if name in item}
                    for item in bucket.get('fresh', [])]}
                for mode, bucket in (value or {}).items() if not mode.startswith('_')}
        payload = self._proposal_json('mode_bundles')
        return json_cache.decode(payload, project=select) if payload is not None else {}

    @snapshot_read
    def has_proposals(self, surface):
        """Check dependency presence without reading or decoding a large display pool."""
        return proposal_storage.read(self._store, surface, presence=True)

    @snapshot_read
    def _proposal_json(self, surface):
        return proposal_storage.read(self._store, surface)

    @snapshot_read
    def choice_usage(self):
        """Serving counts are independent of optional diagnostic output files."""
        if self._store._recommendation_reports or recommendation_reports.prepared_owner(self.conn):
            return recommendation_reports.choice_usage(self.conn)
        return (self.get_proposals('listening_choices') or {}).get('home_format') or {}

    @snapshot_read
    def report_checkpoint(self, surface):
        if surface not in recommendation_reports.SURFACES:
            raise ValueError('Unknown recommendation report')
        if self._store._recommendation_reports or recommendation_reports.prepared_owner(self.conn):
            return recommendation_reports.checkpoint(self.conn, surface)
        value = self.get_proposals(surface)
        if value is None:
            return None
        refresh = value.get('refresh') or {}
        return {'at': value.get('at', value.get('built_at')), 'schema': value.get('schema', value.get('version')),
                'watermark': value.get('watermark'), 'report_id': value.get('report_id'),
                'generation': refresh.get('generation'), 'inputs': refresh.get('inputs', value.get('inputs')),
                'next_at': refresh.get('next_at')}

