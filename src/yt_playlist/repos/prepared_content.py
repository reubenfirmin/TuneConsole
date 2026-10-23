"""Guarded, restartable content inputs; no provider calls or fitting on the writer."""
from contextlib import ExitStack
import hashlib
import json

from yt_playlist.core.db_revisions import keyed_reads
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
        return {name: self.conn.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]
                for name, table in (('pending', 'rec_content_pending'), ('prepared', 'rec_prepared_content'),
                                    ('library_vectors', 'rec_content_vectors'),
                                    ('discovered_vectors', 'rec_discovered_content_vectors'))}

    @synchronized
    def changed_since(self, revision):
        return self.conn.execute('SELECT COUNT(*) FROM rec_prepared_content WHERE revision>?',
                                 (revision,)).fetchone()[0]

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
            for table in ('unresolved_items', 'entry_field_choices', 'library_memberships'):
                guards.enter_context(keyed_reads(self._store, table, entry_ids))
            guards.enter_context(keyed_reads(self._store, 'youtube_items',
                                             [r['video_id'] for r in entries if r['video_id']]))
            for table in ('discovery_memberships', 'discovery_sources'):
                guards.enter_context(keyed_reads(self._store, table, recordings))
            guards.enter_context(keyed_reads(self._store, 'rec_prepared_content', keys))
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
                f'SELECT identity_key,digest FROM rec_prepared_content WHERE identity_key IN ({qs})', keys))
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
                self.conn.execute('INSERT INTO rec_prepared_content VALUES (?,?,?,?,?) '
                    'ON CONFLICT(identity_key) DO UPDATE SET payload=excluded.payload,'
                    'digest=excluded.digest,captured_at=excluded.captured_at,revision=excluded.revision',
                    (key, payload, digest, now, serial + changed))
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
    def corpus(self, limit):
        """Bound detached memory before decoding payloads; None requests a larger budget."""
        rows = self.conn.execute('SELECT identity_key,payload,digest FROM rec_prepared_content '
                                 'ORDER BY identity_key LIMIT ?', (limit + 1,)).fetchall()
        if len(rows) > limit:
            return None
        return [(r['identity_key'], json.loads(r['payload']), r['digest']) for r in rows]

    @synchronized
    def missing_previous_inputs(self):
        """Bootstrap a previously fitted catalog without shrinking its usable cohort."""
        return self.conn.execute(
            'SELECT EXISTS(SELECT 1 FROM rec_content_vectors v LEFT JOIN rec_prepared_content p '
            'ON p.identity_key=v.identity_key WHERE p.identity_key IS NULL) OR '
            'EXISTS(SELECT 1 FROM rec_discovered_content_vectors v LEFT JOIN rec_prepared_content p '
            'ON p.identity_key=v.identity_key WHERE p.identity_key IS NULL)').fetchone()[0]

    @synchronized
    def serial(self):
        return self.conn.execute('SELECT COALESCE(MAX(revision),0) FROM rec_prepared_content').fetchone()[0]
