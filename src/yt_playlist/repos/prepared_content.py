"""Guarded, restartable content inputs; no provider calls or fitting on the writer."""
from contextlib import ExitStack
import hashlib
import json

from yt_playlist.core.db_revisions import column_group_reads, keyed_reads
from yt_playlist.core import embeddings, prepared_inputs, metadata_evidence
from yt_playlist.repos.base import Repo, synchronized


class PreparedContentRepo(Repo):
    @synchronized
    def pending(self, limit=32):
        return [(r['identity_key'], r['revision']) for r in self.conn.execute(
            'SELECT identity_key,revision FROM rec_content_pending ORDER BY position LIMIT ?', (limit,))]

    @synchronized
    def has_pending(self):
        return self.conn.execute('SELECT 1 FROM rec_content_pending LIMIT 1').fetchone() is not None

    @synchronized
    def counters(self):
        return tuple(self.conn.execute(
            'SELECT content_changes,evidence_changes FROM rec_preparation WHERE id=1').fetchone())

    @synchronized
    def progress(self):
        return {'pending': self.conn.execute('SELECT COUNT(*) FROM rec_content_pending').fetchone()[0],
                'prepared': self.conn.execute('SELECT (SELECT COUNT(*) FROM prepared_recording_inputs) + '
                    "(SELECT COUNT(*) FROM unresolved_preparation_inputs WHERE reason='empty_retraction')").fetchone()[0],
                'library_vectors': embeddings.count(self.conn, 'rec_content_vectors'),
                'discovered_vectors': embeddings.count(self.conn, 'rec_discovered_content_vectors')}

    @synchronized
    def changed_since(self, revision):
        return self.conn.execute('SELECT (SELECT COUNT(*) FROM prepared_recording_inputs WHERE input_revision>?) + '
            "(SELECT COUNT(*) FROM unresolved_preparation_inputs WHERE reason='empty_retraction' AND input_revision>?)",
            (revision, revision)).fetchone()[0]

    @synchronized
    def capture(self, pending):
        """Read only these identities and declare every dependent base-table key.

        Counter keys match the TEMP write triggers, including both sides of a
        reassociation. Queries remain constrained even when a catalog view
        authorizes columns which its caller did not select.
        """
        keys = [key for key, _revision in pending]
        if not keys:
            return []
        qs = ','.join('?' for _ in keys)
        with ExitStack() as guards:
            guards.enter_context(keyed_reads(self._store, 'recordings', keys))
            recordings = [r[0] for r in self.conn.execute(
                f'SELECT id FROM recordings WHERE identity_key IN ({qs})', keys)]
            guards.enter_context(keyed_reads(self._store, 'catalog_entries', recordings))
            rqs = ','.join('?' for _ in recordings)
            entries = self.conn.execute(
                f'SELECT id,video_id FROM catalog_entries WHERE recording_id IN ({rqs})', recordings).fetchall()
            entry_ids = [r['id'] for r in entries]
            from yt_playlist.core import metadata_runtime
            choices = (('metadata_runtime_choices',) if metadata_runtime.active(self.conn) else
                       ('metadata_choice_heads', 'metadata_observations') if metadata_evidence.available(self.conn)
                       else ('entry_field_choices',))
            for table in ('unresolved_items', *choices, 'library_memberships'):
                guards.enter_context(keyed_reads(self._store, table, entry_ids))
            guards.enter_context(keyed_reads(self._store, 'youtube_items',
                                             [r['video_id'] for r in entries if r['video_id']]))
            for table in ('discovery_memberships', 'discovery_sources'):
                guards.enter_context(keyed_reads(self._store, table, recordings))
            guards.enter_context(keyed_reads(self._store, 'prepared_recording_inputs', recordings))
            guards.enter_context(keyed_reads(self._store, 'unresolved_preparation_inputs', keys))
            library = {r[0] for r in self.conn.execute(
                f'SELECT DISTINCT identity_key FROM tracks WHERE identity_key IN ({qs})', keys)}
            # Match the established full-input aggregation, including its empty
            # categorical-value and duplicate-entry semantics.
            content = {r['identity_key']: (r['genre'], r['year']) for r in self.conn.execute(
                'SELECT identity_key,MIN(genre) genre,MIN(mb_year) year FROM tracks '
                f"WHERE identity_key IN ({qs}) AND (genre<>'' OR mb_year<>'') GROUP BY identity_key", keys)}
            cols = self._store.rec_query.AUDIO_COLS
            sel = ','.join(f'MIN({c}) AS {c}' for c in cols)
            where = ' OR '.join(f'{c} IS NOT NULL' for c in cols)
            audio = {r['identity_key']: {c: r[c] for c in cols if r[c] not in (None, '')}
                     for r in self.conn.execute(f'SELECT identity_key,{sel} FROM tracks '
                         f'WHERE identity_key IN ({qs}) AND ({where}) GROUP BY identity_key', keys)}
            discovered = self._store.discovery.discovered_tracks_by_keys(keys)
            previous = dict(self.conn.execute(
                'SELECT r.identity_key,p.digest FROM prepared_recording_inputs p '
                f'JOIN recordings r ON r.id=p.recording_id WHERE r.identity_key IN ({qs})', keys))
            # An admission must promote an accepted raw retraction even when
            # both cohorts are still empty. Compare raw digests only while the
            # identity has no current owner.
            previous.update(self.conn.execute('SELECT p.raw_key,p.digest FROM unresolved_preparation_inputs p '
                f'WHERE p.raw_key IN ({qs}) AND NOT EXISTS(SELECT 1 FROM recordings r '
                'WHERE r.identity_key=p.raw_key AND NOT EXISTS(SELECT 1 FROM recording_redirects d '
                'WHERE d.retired_id=r.id))', keys))
            result = []
            for key, revision in pending:
                genre, year = content.get(key, (None, None))
                year = year[:4] if year and year[:4].isdigit() else None
                d = discovered.get(key)
                payload = {'library': ({'genre': genre, 'year': year, 'audio': audio.get(key, {})}
                                        if key in library else None),
                           'discovered': ({'genre': d.get('genre'), 'year': d.get('year'),
                                           'audio': d.get('audio', {})} if d else None)}
                raw = json.dumps(payload, sort_keys=True, separators=(',', ':'))
                digest = hashlib.sha256(raw.encode()).hexdigest()
                result.append((key, revision, raw, digest, previous.get(key) != digest))
            return result

    @synchronized
    def publish_capture(self, rows, now):
        if not self.conn.in_transaction:
            raise RuntimeError('Content capture requires guarded publication')
        changed = 0
        serial = self.counters()[0]
        for key, revision, payload, digest, different in rows:
            if different:
                changed += 1
                prepared_inputs.upsert(self.conn, key, json.loads(payload), digest, now, serial + changed)
            self.conn.execute('DELETE FROM rec_content_pending WHERE identity_key=? AND revision=?', (key, revision))
        self.conn.execute('UPDATE rec_preparation SET content_changes=content_changes+? WHERE id=1', (changed,))
        if changed:
            self._store.rec_refresh.publish_notification()

    @synchronized
    def rotate(self, pending):
        """Yield a hot batch's place without dropping arrivals or acknowledging it."""
        with self.conn:
            for key, _revision in pending:
                self.conn.execute('UPDATE rec_content_pending SET position='
                    '(SELECT COALESCE(MAX(position),0)+1 FROM rec_content_pending) WHERE identity_key=?', (key,))

    @synchronized
    def corpus(self, limit, *, max_bytes=None):
        """Check row and decoded-byte budgets before fetching accepted inputs."""
        with column_group_reads(self._store,'recordings','identity'):
            return self._corpus(limit,max_bytes=max_bytes)

    @synchronized
    def _corpus(self, limit, *, max_bytes=None):
        count = self.conn.execute('SELECT COUNT(*) FROM (SELECT 1 FROM prepared_recording_inputs UNION ALL '
                                  "SELECT 1 FROM unresolved_preparation_inputs WHERE reason='empty_retraction' LIMIT ?)",
                                  (limit + 1,)).fetchone()[0]
        if count > limit:
            return None
        if max_bytes is not None:
            estimate = prepared_inputs.byte_bound()
            size = self.conn.execute('SELECT COALESCE(SUM(bytes),0) FROM ('
                f'SELECT {estimate}+4*length(CAST(r.identity_key AS BLOB))+512 bytes '
                'FROM prepared_recording_inputs p JOIN recordings r ON r.id=p.recording_id UNION ALL '
                f'SELECT {estimate}+4*length(CAST(p.raw_key AS BLOB))+512 bytes '
                "FROM unresolved_preparation_inputs p WHERE p.reason='empty_retraction')").fetchone()[0]
            if size > max_bytes:
                return None
        columns = ','.join('p.' + field for field in prepared_inputs.COLUMNS)
        return [(r['identity_key'], prepared_inputs.payload(r), r['digest']) for r in self.conn.execute(
            f'SELECT r.identity_key,{columns} FROM prepared_recording_inputs p '
            'JOIN recordings r ON r.id=p.recording_id UNION ALL '
            f'SELECT p.raw_key identity_key,{columns} FROM unresolved_preparation_inputs p '
            "WHERE p.reason='empty_retraction' ORDER BY identity_key LIMIT ?", (limit,))]

    @synchronized
    def missing_previous_inputs(self):
        """Bootstrap a previously fitted catalog without shrinking its usable cohort."""
        return self.conn.execute(
            'SELECT EXISTS(SELECT 1 FROM recording_embeddings v '
            'LEFT JOIN prepared_recording_inputs p ON p.recording_id=v.recording_id '
            "WHERE v.space IN ('content_library','content_discovery') AND p.recording_id IS NULL)").fetchone()[0]

    @synchronized
    def missing_unqueued(self, limit=32):
        """Find lost bootstrap work, including old vectors needing a retraction.

        Each indexed anti-join returns at most one batch. Already queued work
        keeps its revision and position; no accepted input is discarded.
        """
        return [row[0] for row in self.conn.execute(
            'SELECT r.identity_key FROM recordings r '
            'LEFT JOIN prepared_recording_inputs p ON p.recording_id=r.id '
            'LEFT JOIN rec_content_pending q ON q.identity_key=r.identity_key '
            'WHERE p.recording_id IS NULL AND q.identity_key IS NULL AND NOT EXISTS('
            'SELECT 1 FROM recording_redirects d WHERE d.retired_id=r.id) '
            'ORDER BY r.identity_key LIMIT ?', (limit,))]

    @synchronized
    def enqueue_missing(self, keys):
        """Called only inside the maintenance snapshot's guarded publication."""
        if not self.conn.in_transaction:
            raise RuntimeError('Preparation repair requires guarded publication')
        self.conn.executemany('INSERT INTO rec_content_pending(identity_key) VALUES (?) '
                              'ON CONFLICT(identity_key) DO NOTHING', ((key,) for key in keys))

    @synchronized
    def vector_storage_bytes(self):
        """Bound old serialized vectors and Python lookup overhead during publication prep."""
        with column_group_reads(self._store,'recordings','identity'):
            return self.conn.execute('SELECT COALESCE(SUM(length(e.vector)+4*length(r.identity_key)+512),0) '
                'FROM recording_embeddings e JOIN recordings r ON r.id=e.recording_id '
                "WHERE e.space IN ('content_library','content_discovery')").fetchone()[0]

    @synchronized
    def serial(self):
        return self.conn.execute('SELECT MAX(COALESCE((SELECT MAX(input_revision) FROM prepared_recording_inputs),0),'
            'COALESCE((SELECT MAX(input_revision) FROM unresolved_preparation_inputs),0))').fetchone()[0]
