"""SQL facts, immutable calculation writes and bounded dependency queues."""
from contextlib import ExitStack
import json
import logging

from yt_playlist.core import (
    acousticbrainz_projection, audio_vibe_metadata, content_work, metadata_native, vibe_model_queries, vibe_provenance,
    vibe_versions,
)
from yt_playlist.core.acousticbrainz_vibe_schema import LINKED_CLASSIFIERS
from yt_playlist.core.activity import check_stopping
from yt_playlist.core.audio_vibe_contract import score_version_filter, served_score
from yt_playlist.core.db_revisions import column_group_reads, keyed_reads
from yt_playlist.repos.base import Repo, lookup_read, snapshot_read, synchronized
from yt_playlist.core.recommendation_invalidation import queue_refresh
from yt_playlist.repos import acousticbrainz_vibes, provenance_file_writes

log = logging.getLogger(__name__)
ORIGINALS = '*originals'
REFRESH = '*'
FOLLOWING = '*next'
ACOUSTICBRAINZ = '*acousticbrainz'
JOINT_ARTIST = '@joint-artist:'
CAPTURE_RUN_PAGE = 16


class VibePredictionsRepo(Repo):
    @synchronized
    def publication_facts(self, keys, requests, originals, artists):
        if not getattr(self._store, '_read_only', False):
            raise RuntimeError('Vibe preparation requires a guarded private snapshot')
        with keyed_reads(self._store, 'recordings', keys):
            qs = ','.join('?' for _ in keys)
            ids = dict(self.conn.execute(f'SELECT identity_key,id FROM recordings WHERE identity_key IN ({qs})', keys))
        previous, estimates = {}, {}
        with keyed_reads(self._store, 'vibe_versions', []):
            marks = ','.join('?' for _ in ids)
            for row in self.conn.execute(
                    f'SELECT * FROM vibe_recording_fits WHERE recording_id IN ({marks})', list(ids.values())):
                previous.setdefault(row['recording_id'], {})[row['vibe']] = dict(row)
        with keyed_reads(self._store, 'rec_musical_links', ids.values()), \
                keyed_reads(self._store, 'recordings', ids):
            classifier_inputs = {rid: (source, mbid, values) for rid, source, mbid, values in self.conn.execute(
                f'SELECT * FROM ({LINKED_CLASSIFIERS}) WHERE recording_id IN ({marks})', list(ids.values()))}
            for rid, value in self.conn.execute(
                    f'SELECT id,acousticbrainz_estimates FROM recordings WHERE id IN ({marks}) '
                    f'AND {acousticbrainz_projection.PRESENT}', list(ids.values())):
                estimates[rid] = {vibe: tuple(fit) for vibe, fit in json.loads(value).items()}
        requests = [(ids[key], version, digest) for key, version, digest in requests if key in ids]
        measurements = {}
        if requests:
            wanted = ','.join('(?,?,?)' for _ in requests)
            with keyed_reads(self._store, 'vibe_versions', []):
                measurements = {(row[0], row[1]): tuple(row[2:]) for row in self.conn.execute(
                    'WITH wanted(recording_id,version,digest) AS (VALUES ' + wanted + ') '
                    'SELECT m.recording_id,m.vibe,m.id,m.score,m.support,m.source FROM wanted w '
                    'JOIN vibe_versions version ON version.value=w.version '
                    'JOIN measurements m ON m.recording_id=w.recording_id AND '
                    'm.model_version_id=version.id AND m.input_digest=w.digest',
                    [value for request in requests for value in request])}
        provenance = {}
        for pid in {row['prediction_id'] for fits in previous.values() for row in fits.values()
                    if row['prediction_id'] is not None}:
            provenance[pid] = vibe_model_queries.receipt_marker(self.conn, pid)
        present_artists = set()
        artists = list(artists)
        for start in range(0, len(artists), 128):
            batch = artists[start:start + 128]
            present_artists.update(row[0] for row in self.conn.execute(
                'SELECT id FROM artists WHERE id IN (' + ','.join('?' for _ in batch) + ')', batch))
        return {'ids': ids, 'next_measurement': self.conn.execute(
                    'SELECT coalesce(max(id),0)+1 FROM measurements').fetchone()[0],
                'next_prediction': vibe_provenance.next_id(self.conn), 'previous': previous,
                'measurements': measurements, 'originals': self.measurement_subjects(originals),
                'artists': present_artists, 'provenance': provenance, 'classifier_inputs': classifier_inputs,
                'estimates': estimates}

    @synchronized
    def measurement_subjects(self, identities):
        identities, result = list(identities), {}
        for start in range(0, len(identities), 128):
            check_stopping()
            batch = identities[start:start + 128]
            result.update((row[0], tuple(row[1:])) for row in self.conn.execute(
                'SELECT id,vibe,recording_id FROM measurements WHERE id IN ('
                + ','.join('?' for _ in batch) + ')', batch))
        return result

    @synchronized
    def assert_idle(self):
        if self.conn.in_transaction:
            raise RuntimeError('Vibe files must be published outside a transaction')

    @synchronized
    def reserve_provenance(self, records):
        with self.conn.atomic():
            return provenance_file_writes.reserve(self.conn, records)

    @synchronized
    def expire_provenance(self, now):
        with self.conn.atomic():
            return vibe_provenance.maintain(self.conn, now)

    @synchronized
    def provenance_owners(self, identity, group):
        # The caller holds the file-reader fence. Opening another application
        # reader here would wait for that same fence; use the current SQL owner.
        if identity is not None:
            ready = self.conn.execute("SELECT 1 FROM predictions WHERE provenance_file_id=? AND "
                                      "provenance_state='ready' LIMIT 1", (identity,)).fetchone() is not None
            return ready, None
        rows = self.conn.execute('SELECT id,provenance_state FROM predictions '
            'WHERE id>=? AND id<? AND provenance_file_id IS NULL AND provenance_state IS NOT NULL',
            (group * 128, (group + 1) * 128)).fetchall()
        return {str(pid) for pid, state in rows if state == 'ready'}, {str(pid) for pid, state in rows}

    @synchronized
    def retire_history(self, now, after, limit=128):
        from yt_playlist.core.vibe_history import RETAIN_SECONDS
        if type(limit) is not int or not 1 <= limit <= 256:
            raise ValueError('Invalid vibe history collection budget')
        removed = 0
        with self.conn.atomic():
            rows = self.conn.execute('SELECT id,recording_id,vibe,predicted_at FROM predictions '
                'WHERE id>? ORDER BY id LIMIT ?', (after, limit)).fetchall()
            for pid, recording, vibe, at in rows:
                if at is None or at >= now - RETAIN_SECONDS:
                    continue
                if self.conn.execute('SELECT 1 FROM recording_vibes WHERE recording_id=? AND vibe=? '
                                     'AND prediction_id=?', (recording, vibe, pid)).fetchone():
                    continue
                removed += self.conn.execute('DELETE FROM predictions WHERE id=?', (pid,)).rowcount
        return removed, rows[-1][0] if len(rows) == limit else 0

    @synchronized
    def publish_prepared(self, plan, *, cohorts=()):
        with self.conn.atomic():
            provenance_file_writes.complete(self.conn, plan.file_records)
            vibe_model_queries.insert_measurements(self.conn, plan.measurements)
            self.conn.executemany('INSERT OR IGNORE INTO measurement_samples VALUES (?,?)', plan.samples)
            vibe_model_queries.retain_measurements(self.conn, plan.roots)
            generations = dict(plan.generations)
            markers = {pid: (fingerprint, size, identity) for pid, fingerprint, size, identity in plan.markers}
            versions = {version: vibe_versions.intern(self.conn, version)
                        for version in dict.fromkeys(row[7] for row in plan.predictions)}
            self.conn.executemany('INSERT INTO predictions '
                '(id,recording_id,vibe,score,support,subgenre_key,kind,policy_version_id,predicted_at,'
                'model_generation,provenance_fingerprint,provenance_bytes,provenance_file_id,provenance_state) '
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,'ready')",
                ((*row[:7], versions[row[7]], row[8], generations.get(row[0]), *markers[row[0]])
                 for row in plan.predictions))
            self.conn.executemany('DELETE FROM recording_vibes WHERE recording_id=? AND vibe=?', plan.deleted)
            self.conn.executemany('INSERT INTO vibe_recording_fits '
                '(recording_id,vibe,score,support,source,measurement_id,prediction_id,inputs_version,'
                'artist_key,subgenre_key,recordings,variance) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)', plan.publications)
            acousticbrainz_vibes.publish(self.conn, plan)
            self._queue(cohorts)

    @synchronized
    def acousticbrainz_matches(self, keys):
        with keyed_reads(self._store, 'recordings', keys):
            return acousticbrainz_vibes.matches(self.conn, keys)

    @synchronized
    def acousticbrainz_training_rows(self, version):
        return acousticbrainz_vibes.training_rows(self.conn, version)

    @lookup_read
    def acousticbrainz_model(self):
        return self.conn.execute('SELECT acousticbrainz_fit FROM rec_vibe_pending WHERE position=0').fetchone()[0]

    @synchronized
    def install_acousticbrainz_model(self, previous, state, *, refresh):
        with self.conn.atomic():
            updated = self.conn.execute('UPDATE rec_vibe_pending SET acousticbrainz_fit=? '
                                        'WHERE position=0 AND acousticbrainz_fit IS ?', (state, previous)).rowcount
            if not updated:
                return False
            if refresh:
                self._queue((ACOUSTICBRAINZ,))
            return True

    @synchronized
    def acousticbrainz_pending(self):
        return self.conn.execute('SELECT revision,cursor FROM rec_vibe_pending WHERE group_key=?',
                                 (ACOUSTICBRAINZ,)).fetchone()

    @synchronized
    def acousticbrainz_estimate_page(self, cursor, limit):
        return acousticbrainz_vibes.estimate_page(self.conn, cursor, limit)

    @synchronized
    def publish_acousticbrainz_page(self, updates, cursor):
        with self.conn.atomic(join=True):
            if updates:
                for score, version, recording, vibe in updates:
                    acousticbrainz_projection.put(self.conn, recording, vibe, score, version, existing=True)
                queue_refresh(self.conn)
            if cursor is None:
                self.conn.execute('DELETE FROM rec_vibe_pending WHERE group_key=?', (ACOUSTICBRAINZ,))
            else:
                self.conn.execute('UPDATE rec_vibe_pending SET cursor=? WHERE group_key=?', (cursor, ACOUSTICBRAINZ))

    @synchronized
    def pending(self, *, include_acousticbrainz=True):
        select = 'SELECT group_key,revision,cursor FROM rec_vibe_pending '
        return (self.conn.execute(select + 'WHERE group_key=?', (ORIGINALS,)).fetchone()
                or self.conn.execute(select + 'NOT INDEXED WHERE group_key GLOB ? ORDER BY position LIMIT 1',
                                     (JOINT_ARTIST + '*',)).fetchone()
                or self.conn.execute(select + 'WHERE group_key=?', (REFRESH,)).fetchone()
                or self.conn.execute(select + 'WHERE position>0 AND group_key<>? ORDER BY position LIMIT 1',
                                     ('' if include_acousticbrainz else ACOUSTICBRAINZ,)).fetchone())

    @synchronized
    def has_joint_scores(self, generations):
        if not generations:
            return False
        if len(generations) > 8:
            raise ValueError('Joint score acknowledgement exceeds the channel bound')
        marks = ','.join('?' for _ in generations)
        return self.conn.execute('SELECT 1 FROM predictions p '
            'JOIN recording_vibes v ON v.recording_id=p.recording_id AND v.vibe=p.vibe AND v.prediction_id=p.id '
            f'WHERE p.model_generation IN ({marks}) LIMIT 1',
            [bytes.fromhex(value) for value in generations]).fetchone() is not None

    @synchronized
    def fanout(self, limit=32, *, measurement_pattern='*'):
        worked, remaining = False, limit
        identities = {}
        with self.conn.atomic():
            for _ in range(limit):
                if not remaining:
                    break
                check_stopping()
                pending = self.pending(include_acousticbrainz=False)
                if not pending:
                    break
                worked = True
                key, revision, cursor = pending
                joint = key.startswith(JOINT_ARTIST)
                if joint:
                    rows = self._joint_artist_page(key, cursor, remaining, measurement_pattern)
                elif key == REFRESH:
                    rows = self.conn.execute('SELECT identity_key FROM recordings WHERE identity_key>? '
                        'ORDER BY identity_key LIMIT ?', (cursor, remaining)).fetchall()
                elif key == ORIGINALS:
                    rows = self.conn.execute('SELECT r.identity_key FROM recordings r WHERE r.identity_key>? AND ('
                        'EXISTS (SELECT 1 FROM recording_vibes v WHERE v.recording_id=r.id AND v.measurement_id IS NOT NULL) '
                        'OR EXISTS (SELECT 1 FROM sample_attributes s WHERE s.recording_id=r.id AND s.file_status=\'ready\')) '
                        'ORDER BY r.identity_key LIMIT ?', (cursor, remaining)).fetchall()
                else:
                    where, args = self._cohort(*json.loads(key))
                    # Own measurements do not consume cohort estimates. A
                    # partial recording still appears through any fallback
                    # channel; model-version refreshes above visit everyone.
                    rows = self.conn.execute('SELECT DISTINCT r.identity_key FROM recording_vibes v '
                        f'JOIN recordings r ON r.id=v.recording_id WHERE {where} AND v.measurement_id IS NULL '
                        'AND r.identity_key>? '
                        'ORDER BY r.identity_key LIMIT ?', (*args, cursor, remaining)).fetchall()
                identities.update(dict.fromkeys(row[-1] for row in rows if row[-1] is not None))
                self.conn.execute('DELETE FROM rec_vibe_pending WHERE group_key=? AND revision=? AND cursor=?',
                                  (key, revision, cursor))
                if len(rows) == remaining:
                    self.conn.execute('INSERT INTO rec_vibe_pending(group_key,revision,cursor) VALUES (?,?,?)',
                                      (key, revision, rows[-1][0]))
                elif key == REFRESH:
                    # Changes behind the completed cursor get one follow-up pass.
                    self.conn.execute('UPDATE rec_vibe_pending SET group_key=? WHERE group_key=?', (REFRESH, FOLLOWING))
                elif joint and revision > 1:
                    self.conn.execute('INSERT INTO rec_vibe_pending(group_key) VALUES (?)', (key,))
                log.debug('Vibe dispatch phase=%s count=%d done=%s', key, len(rows), len(rows) < remaining)
                remaining -= len(rows)
            content_work.enqueue(self.conn, identities)
        return worked

    def _joint_artist_page(self, key, cursor, limit, pattern):
        vibe, artist = key[len(JOINT_ARTIST):].split(':')
        # Page the artist index before checking targets. A full page can contain
        # only sampled recordings or duplicate uploads and still advance.
        return self.conn.execute(f'''WITH page AS MATERIALIZED (
                SELECT DISTINCT c.entry_id,e.recording_id FROM {self.entry_artists_table} c
                JOIN track_sources e ON e.id=c.entry_id
                WHERE c.artist_id=? AND c.entry_id>? ORDER BY c.entry_id LIMIT ?)
            SELECT page.entry_id, CASE WHEN NOT EXISTS (
                SELECT 1 FROM vibe_recording_fits v WHERE v.recording_id=r.id AND v.vibe=?
                AND v.source='audio_measurement' AND v.inputs_version GLOB ?
            ) AND NOT EXISTS (
                SELECT 1 FROM track_sources e JOIN {self.entry_artists_table} c ON c.entry_id=e.id
                WHERE e.recording_id=r.id AND c.artist_id=? AND c.entry_id<page.entry_id
            ) THEN r.identity_key END
            FROM page JOIN recordings r ON r.id=page.recording_id ORDER BY page.entry_id''',
            (int(artist), int(cursor) if cursor else -1, limit, vibe, pattern, int(artist))).fetchall()

    @staticmethod
    def _cohort(kind, artist, genre):
        return {'artist_genre': ('v.artist_key=? AND v.subgenre_key=?', (artist, genre)),
                'artist': ('v.artist_key=?', (artist,)), 'genre': ('v.subgenre_key=?', (genre,))}[kind]

    @synchronized
    def queue(self, keys):
        keys = tuple(keys)
        with self.conn.atomic():
            self._queue(key for key in keys if key != REFRESH)
            if REFRESH in keys:
                self._request_refresh('manual')

    def _queue(self, keys):
        self.conn.executemany('INSERT INTO rec_vibe_pending(group_key) VALUES (?) '
            "ON CONFLICT(group_key) DO UPDATE SET revision=revision+1,cursor=''", [(key,) for key in keys])

    def _request_refresh(self, reason):
        active = self.conn.execute('SELECT cursor FROM rec_vibe_pending WHERE group_key=?', (REFRESH,)).fetchone()
        phase = FOLLOWING if active and active[0] else REFRESH
        queued = bool(self.conn.execute('INSERT OR IGNORE INTO rec_vibe_pending(group_key) VALUES (?)', (phase,)).rowcount)
        log.debug('Vibe refresh requested reason=%s phase=%s queued=%s', reason, phase, queued)

    @lookup_read
    def rebuild_policy(self):
        return self.conn.execute('SELECT rebuild_policy FROM rec_vibe_pending WHERE position=0').fetchone()[0]

    @synchronized
    def ensure_version(self, version):
        with self.conn.atomic():
            current = self.conn.execute('UPDATE rec_vibe_pending SET rebuild_policy=? '
                                        'WHERE position=0 AND rebuild_policy IS NOT ?', (version, version)).rowcount
            if not current:
                return False
            self._queue([ORIGINALS])
            self._request_refresh('policy-rebuild')
            return True

    @synchronized
    def current_facts(self, rid):
        with keyed_reads(self._store, 'vibe_versions', []):
            rows = [dict(row) for row in self.conn.execute(
                'SELECT v.*,m.input_digest FROM vibe_recording_fits v LEFT JOIN measurements m ON m.id=v.measurement_id '
                'WHERE v.recording_id=? ORDER BY v.vibe', (rid,))]
        ids = [sid for sid, in self.conn.execute('SELECT s.sample_id FROM recording_vibes v '
            'JOIN measurement_samples s ON s.measurement_id=v.measurement_id '
            'WHERE v.recording_id=? ORDER BY v.vibe,s.sample_id', (rid,))]
        return rows, ids

    @synchronized
    def input_facts(self, keys, *, run_offset=0):
        result = {}
        with ExitStack() as guards:
            guards.enter_context(keyed_reads(self._store, 'recordings', keys))
            ids = {key: row[0] for key in keys for row in self.conn.execute(
                'SELECT id FROM recordings WHERE identity_key=?', (key,))}
            for table in ('sample_attributes', 'track_sources', 'rec_musical_links'):
                guards.enter_context(keyed_reads(self._store, table, ids.values()))
            entries = [r[0] for rid in ids.values() for r in self.conn.execute(
                'SELECT id FROM track_sources WHERE recording_id=?', (rid,))]
            from yt_playlist.core import metadata_runtime
            runtime = metadata_runtime.active(self.conn)
            choices = (('track_sources',) if runtime else
                       ('metadata_observations',))
            for table in (self.entry_artists_table, *choices, 'unresolved_items'):
                guards.enter_context(keyed_reads(self._store, table, entries))
            sources = {r[0] for rid in ids.values() for r in self.conn.execute(
                'SELECT source_key FROM rec_musical_links WHERE recording_id=?', (rid,))}
            guards.enter_context(keyed_reads(self._store, 'rec_musical_sources', sources))
            if not runtime:
                with keyed_reads(self._store, 'native_metadata_observations', []):
                    references = [r for source in sources for r in self.conn.execute(
                        'SELECT o.id,o.delivery_id ' + metadata_native.SELECTED_OBSERVATIONS +
                        'WHERE s.source_key=?', (source,))]
                guards.enter_context(keyed_reads(self._store, 'native_metadata_observations', [r[0] for r in references]))
                guards.enter_context(keyed_reads(self._store, 'metadata_deliveries', [r[1] for r in references]))
            for key in keys:
                check_stopping()
                rid = ids.get(key)
                artists = [r[0] for r in self.conn.execute('SELECT DISTINCT a.artist_id FROM track_sources e '
                    f'JOIN {self.entry_artists_table} a ON a.entry_id=e.id WHERE e.recording_id=? ORDER BY a.artist_id LIMIT 17', (rid,))]
                rows = self.conn.execute(
                    'WITH runs AS (SELECT run_id,video_id,MAX(captured_at) AS captured,COUNT(*) AS ready_count '
                    "FROM sample_attributes WHERE recording_id=? AND file_status='ready' GROUP BY run_id,video_id "
                    'ORDER BY ready_count DESC,captured DESC,video_id DESC,run_id DESC LIMIT ? OFFSET ?) '
                    'SELECT s.id,s.run_id,s.point,s.video_id,s.filename,s.content_digest,r.captured '
                    'FROM runs r JOIN sample_attributes s ON s.run_id=r.run_id AND s.video_id=r.video_id '
                    "WHERE s.recording_id=? AND s.file_status='ready' "
                    'ORDER BY r.ready_count DESC,r.captured DESC,r.video_id DESC,r.run_id DESC,s.point',
                    (rid, CAPTURE_RUN_PAGE + 1, run_offset, rid))
                runs = {}
                next_run_offset = None
                for row in rows:
                    sample = dict(row)
                    order = (sample.pop('captured'), sample['video_id'], sample['run_id'])
                    if len(runs) == CAPTURE_RUN_PAGE and order not in runs:
                        next_run_offset = run_offset + CAPTURE_RUN_PAGE
                        break
                    runs.setdefault(order, []).append(sample)
                sources_by_video = {video: audio_vibe_metadata.read_facts(self.conn, video)
                                    for video in sorted({order[1] for order in runs})}
                result[key] = {'artists': artists, 'runs': runs, 'sources': sources_by_video, 'recording': rid,
                               'next_run_offset': next_run_offset}
        return result

    @synchronized
    def cohort_rows(self, key, after, limit, *, pattern):
        where, args = self._cohort(*json.loads(key))
        with keyed_reads(self._store, 'measurements', []), column_group_reads(self._store, 'recordings', 'identity'):
            return [tuple(row) for row in self.conn.execute(
                'SELECT v.recording_id,r.identity_key,m.id,m.vibe,m.score,m.support FROM vibe_recording_fits v '
                'JOIN recordings r ON r.id=v.recording_id JOIN measurements m ON m.id=v.measurement_id '
                'JOIN vibe_versions version ON version.id=m.model_version_id '
                f"WHERE {where} AND m.source='audio_measurement' AND v.source='audio_measurement' "
                'AND m.support>0 AND v.inputs_version GLOB ? AND version.value GLOB ? '
                'AND (v.recording_id,v.vibe)>(?,?) ORDER BY v.recording_id,v.vibe LIMIT ?',
                (*args, pattern, pattern, *after, limit))]

    @synchronized
    def joint_training_rows(self, vibe, limit, *, pattern):
        return vibe_model_queries.training_rows(self.conn, vibe, limit, pattern=pattern)

    @synchronized
    def album_facts(self, recordings):
        return vibe_model_queries.album_facts(self.conn, recordings)

    @synchronized
    def joint_profile_keys(self, recordings):
        if not 0 < len(recordings) <= 32:
            raise ValueError('Joint masked profile capture requires a bounded batch')
        keys = dict(self.conn.execute('SELECT id,identity_key FROM recordings WHERE id IN ('
            + ','.join('?' for _ in recordings) + ')', recordings))
        if set(keys) != set(recordings):
            raise ValueError('Joint masked profile requires each original recording')
        return keys

    @synchronized
    def joint_album_facts(self, keys):
        with keyed_reads(self._store, 'recordings', keys):
            ids = {key: row[0] for key in keys for row in self.conn.execute(
                'SELECT id FROM recordings WHERE identity_key=?', (key,))}
        with keyed_reads(self._store, 'track_sources', ids.values()):
            albums = vibe_model_queries.album_facts(self.conn, ids.values())
        return ids, albums

    @lookup_read
    def joint_state(self, vibe):
        with keyed_reads(self._store, 'vibe_definitions', (vibe,)):
            return vibe_model_queries.joint_state(self.conn, vibe)

    @lookup_read
    def joint_revision(self):
        return str(self.conn.execute('SELECT evidence_revision FROM rec_vibe_pending WHERE position=0').fetchone()[0])

    @lookup_read
    def joint_fit_states(self):
        return {vibe: json.loads(outcome) for vibe, outcome in self.conn.execute(
            'SELECT vibe,joint_outcome FROM vibe_definitions WHERE joint_outcome IS NOT NULL')}

    @synchronized
    def save_joint_outcome(self, vibe, outcome, *, refresh=False):
        with self.conn.atomic(join=True):
            if refresh:
                self._request_refresh('manual')
            self.conn.execute('UPDATE vibe_definitions SET joint_outcome=? WHERE vibe=?', (json.dumps(outcome), vibe))

    @synchronized
    def clear_joint_outcomes(self, vibes):
        with self.conn.atomic(join=True):
            self.conn.executemany('UPDATE vibe_definitions SET joint_outcome=NULL WHERE vibe=?', ((v,) for v in vibes))

    @synchronized
    def install_joint_model(self, vibe, generation, outcome, measurements, refresh, *, artists=()):
        with self.conn.atomic():
            vibe_model_queries.retain_measurements(self.conn, measurements)
            if artists:
                # A newer fit needs one more pass for recipients behind an active cursor.
                self.conn.execute('INSERT INTO rec_vibe_pending(group_key) '
                    'SELECT ? || value FROM json_each(?) WHERE true '
                    "ON CONFLICT(group_key) DO UPDATE SET revision=2 WHERE cursor<>'' AND revision=1",
                    (JOINT_ARTIST + vibe + ':', json.dumps(artists)))
            if refresh:
                self._request_refresh('model:' + vibe)
            self.conn.execute('UPDATE vibe_definitions SET joint_head=?,joint_outcome=? WHERE vibe=?',
                              (generation, json.dumps(outcome), vibe))

    @synchronized
    def retained_joint_fits(self, keys, *, patterns):
        result = {}
        with keyed_reads(self._store, 'recordings', keys):
            ids = {key: row[0] for key in keys for row in self.conn.execute(
                'SELECT id FROM recordings WHERE identity_key=?', (key,))}
        with keyed_reads(self._store, 'recording_vibes', ids.values()), keyed_reads(self._store, 'vibe_versions', []):
            for key, rid in ids.items():
                result[key] = [dict(row) for row in self.conn.execute('SELECT v.* FROM vibe_recording_fits v '
                    'JOIN predictions p ON p.id=v.prediction_id WHERE v.recording_id=? AND p.model_generation IS NOT NULL AND '
                    + score_version_filter('v.'), (rid, *patterns))]
        return result

    @synchronized
    def prediction_conditioning(self, prediction_id):
        return vibe_provenance.conditioning(self.conn, prediction_id, file_reads=lambda file_id:
            keyed_reads(self._store, 'collection_file_writes', (file_id,)))

    @synchronized
    def overlay_rows(self, keys, *, patterns):
        with column_group_reads(self._store, 'recordings', 'identity'):
            ids = {r[0]: r[1] for r in self.conn.execute('SELECT id,identity_key FROM recordings '
                'WHERE identity_key IN (' + ','.join('?' for _ in keys) + ')', keys)}
        # The served fits view checks the selected MusicBrainz ID for provider
        # estimates. Other recording metadata cannot change these results.
        with column_group_reads(self._store, 'recordings', 'vibe_evidence'), \
                keyed_reads(self._store, 'recording_vibes', ids), keyed_reads(self._store, 'vibe_versions', []):
            return [(ids[row['recording_id']], dict(row)) for row in self.conn.execute(
                'SELECT recording_id,vibe,score,support,source,inputs_version,recordings,variance,prediction_id '
                'FROM served_vibe_fits WHERE recording_id IN (' + ','.join('?' for _ in ids) + ') AND '
                + score_version_filter(), (*ids, *patterns))]

    @snapshot_read
    def fit_scores(self, keys, preset, *, patterns):
        keys = tuple(dict.fromkeys(keys))
        if not keys:
            return {}
        marks = ','.join('?' for _ in keys)
        with keyed_reads(self._store, 'recordings', keys), keyed_reads(self._store, 'vibe_versions', []):
            return dict(self.conn.execute(f'''
                WITH requested AS MATERIALIZED (
                    SELECT id,identity_key FROM recordings
                    WHERE identity_key IN ({marks}) AND musical_profile_version IS NOT NULL
                )
                SELECT r.identity_key, {served_score('fit')}
                FROM served_vibe_fits fit JOIN requested r ON r.id=fit.recording_id
                WHERE fit.vibe=? AND {score_version_filter('fit.')}
                GROUP BY r.identity_key
            ''', (*keys, preset, *patterns)))

    @synchronized
    def ranked(self, vibe, limit, *, patterns):
        return [dict(row) for row in self.conn.execute(
            'SELECT recording_id,score,support,source FROM served_vibe_fits WHERE vibe=? AND '
            + score_version_filter() + ' ORDER BY score DESC,recording_id LIMIT ?', (vibe, *patterns, limit))]
