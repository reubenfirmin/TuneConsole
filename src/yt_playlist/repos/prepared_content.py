"""Guarded, restartable content inputs; no provider calls or fitting on the writer."""
from contextlib import contextmanager, ExitStack

from yt_playlist.core.db_revisions import column_group_reads, keyed_reads
from yt_playlist.core import embeddings, prepared_inputs, metadata_evidence
from yt_playlist.repos.base import Repo, synchronized


class PreparedContentRepo(Repo):
    @synchronized
    def requeue_recording(self, recording_id):
        with self.conn.atomic(join=True):
            self.conn.execute('INSERT INTO rec_content_pending(identity_key) '
                'SELECT identity_key FROM recordings WHERE id=? '
                'ON CONFLICT(identity_key) DO UPDATE SET revision=revision+1', (recording_id,))

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

    @contextmanager
    def capture_reads(self, keys):
        """Declare dependent base-table keys for a bounded catalog read.

        Counter keys match the TEMP write triggers, including both sides of a
        reassociation. Queries remain constrained even when a catalog view
        authorizes columns which its caller did not select.
        """
        qs = ','.join('?' for _ in keys)
        with self._lock, ExitStack() as guards:
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
            yield

    @synchronized
    def capture_facts(self, keys, *, mask_numeric=False):
        qs = ','.join('?' for _ in keys)
        library = {r[0] for r in self.conn.execute(
            f'SELECT DISTINCT identity_key FROM tracks WHERE identity_key IN ({qs})', keys)}
        # Preserve the full-input aggregation's empty and duplicate-entry semantics.
        content = {r['identity_key']: (r['genre'], r['year']) for r in self.conn.execute(
            'SELECT identity_key,MIN(genre) genre,MIN(mb_year) year FROM tracks '
            f"WHERE identity_key IN ({qs}) AND (genre<>'' OR mb_year<>'') GROUP BY identity_key", keys)}
        cols = prepared_inputs.AUDIO
        sel = ','.join(f'MIN({c}) AS {c}' for c in cols)
        where = ' OR '.join(f'{c} IS NOT NULL' for c in cols)
        audio = {} if mask_numeric else {r['identity_key']: {c: r[c] for c in cols if r[c] not in (None, '')}
                     for r in self.conn.execute(f'SELECT identity_key,{sel} FROM tracks '
                         f'WHERE identity_key IN ({qs}) AND ({where}) GROUP BY identity_key', keys)}
        previous = dict(self.conn.execute(
                'SELECT r.identity_key,p.digest FROM prepared_recording_inputs p '
                f'JOIN recordings r ON r.id=p.recording_id WHERE r.identity_key IN ({qs})', keys))
        # Admission must promote a raw retraction even when both cohorts are empty.
        previous.update(self.conn.execute('SELECT p.raw_key,p.digest FROM unresolved_preparation_inputs p '
                f'WHERE p.raw_key IN ({qs}) AND NOT EXISTS(SELECT 1 FROM recordings r '
                'WHERE r.identity_key=p.raw_key AND NOT EXISTS(SELECT 1 FROM recording_redirects d '
                'WHERE d.retired_id=r.id))', keys))
        return {'library': library, 'content': content, 'audio': audio, 'previous': previous}

    @synchronized
    def publish_capture(self, rows, now):
        with self.conn.atomic(join=True):
            changed = 0
            serial = self.counters()[0]
            for key, fields, digest, empty in rows:
                owner = self.conn.execute('SELECT r.id FROM recordings r WHERE r.identity_key=? '
                    'AND NOT EXISTS(SELECT 1 FROM recording_redirects d WHERE d.retired_id=r.id)', (key,)).fetchone()
                previous = (self.conn.execute('SELECT digest FROM prepared_recording_inputs WHERE recording_id=?',
                                             (owner[0],)).fetchone() if owner else
                            self.conn.execute('SELECT digest FROM unresolved_preparation_inputs WHERE raw_key=?',
                                              (key,)).fetchone())
                if previous and previous[0] == digest:
                    continue
                changed += 1
                prepared_inputs.upsert_values(self.conn, key, fields, digest, now, serial + changed, empty=empty)
            self.conn.execute('UPDATE rec_preparation SET content_changes=content_changes+? WHERE id=1', (changed,))
        return changed

    @synchronized
    def acknowledge(self, pending):
        with self.conn.atomic(join=True):
            self.conn.executemany('DELETE FROM rec_content_pending WHERE identity_key=? AND revision=?', pending)

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
        from yt_playlist.core import embedding_files
        table = embedding_files.relation(self.conn, 'recording')
        return self.conn.execute(
            f'SELECT EXISTS(SELECT 1 FROM {table} v '
            'LEFT JOIN prepared_recording_inputs p ON p.recording_id=v.recording_id '
            "WHERE v.space IN ('content_library','content_discovery') AND p.recording_id IS NULL)").fetchone()[0]

    @synchronized
    def missing_unqueued(self, limit=32, *, after=None):
        """Find lost bootstrap work, including old vectors needing a retraction.

        Each indexed anti-join returns at most one batch. Already queued work
        keeps its revision and position; no accepted input is discarded.
        """
        cursor = 'AND r.identity_key>? ' if after is not None else ''
        args = (after, limit) if after is not None else (limit,)
        return [row[0] for row in self.conn.execute(
            'SELECT r.identity_key FROM recordings r '
            'LEFT JOIN prepared_recording_inputs p ON p.recording_id=r.id '
            'LEFT JOIN rec_content_pending q ON q.identity_key=r.identity_key '
            'WHERE p.recording_id IS NULL AND q.identity_key IS NULL AND NOT EXISTS('
            'SELECT 1 FROM recording_redirects d WHERE d.retired_id=r.id) '
            + cursor + 'ORDER BY r.identity_key LIMIT ?', args)]

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
            from yt_playlist.core import embedding_files
            table = embedding_files.relation(self.conn, 'recording')
            return self.conn.execute('SELECT COALESCE(SUM(e.dimensions*4+4*length(r.identity_key)+512),0) '
                f'FROM {table} e JOIN recordings r ON r.id=e.recording_id '
                "WHERE e.space IN ('content_library','content_discovery')").fetchone()[0]

    @synchronized
    def serial(self):
        return self.conn.execute('SELECT MAX(COALESCE((SELECT MAX(input_revision) FROM prepared_recording_inputs),0),'
            'COALESCE((SELECT MAX(input_revision) FROM unresolved_preparation_inputs),0))').fetchone()[0]
