"""SQL facts, immutable calculation writes and bounded dependency queues."""
from contextlib import ExitStack
import json

from yt_playlist.core import audio_vibe_metadata, metadata_evidence, metadata_native, vibe_provenance, vibe_versions
from yt_playlist.core.activity import check_stopping
from yt_playlist.core.db_revisions import column_group_reads, keyed_reads
from yt_playlist.repos.base import Repo, snapshot_read, synchronized
from yt_playlist.repos import vibe_models


class VibePredictionsRepo(Repo):
    @synchronized
    def publication_facts(self, keys, versions, requests, originals, artists):
        if not getattr(self._store, '_read_only', False):
            raise RuntimeError('Vibe preparation requires a guarded private snapshot')
        with keyed_reads(self._store, 'recordings', keys):
            qs = ','.join('?' for _ in keys)
            ids = dict(self.conn.execute(f'SELECT identity_key,id FROM recordings WHERE identity_key IN ({qs})', keys))
        previous = {}
        with keyed_reads(self._store, 'vibe_versions', []):
            stamps = {version: vibe_versions.lookup(self.conn, version) for version in versions}
            projection = vibe_versions.projection(self.conn, 'recording_vibes', 'v')
            marks = ','.join('?' for _ in ids)
            for row in self.conn.execute('SELECT ' + projection +
                    f' FROM recording_vibes v WHERE recording_id IN ({marks})', list(ids.values())):
                previous.setdefault(row['recording_id'], {})[row['vibe']] = dict(row)
        measured_version = vibe_versions.column(self.conn, 'measurements')
        requests = [(ids[key], stamps[version], digest) for key, version, digest in requests if key in ids]
        measurements = {}
        if requests:
            wanted = ','.join('(?,?,?)' for _ in requests)
            measurements = {(row[0], row[1]): tuple(row[2:]) for row in self.conn.execute(
                'WITH wanted(recording_id,version,digest) AS (VALUES ' + wanted + ') '
                'SELECT m.recording_id,m.vibe,m.id,m.score,m.support,m.source FROM wanted w '
                'JOIN measurements m ON m.recording_id=w.recording_id AND '
                f'm.{measured_version}=w.version AND m.input_digest=w.digest',
                [value for request in requests for value in request])}
        provenance = {}
        for pid in {row['prediction_id'] for fits in previous.values() for row in fits.values()
                    if row['prediction_id'] is not None}:
            marker = self.conn.execute('SELECT input_fingerprint,state FROM prediction_provenance WHERE prediction_id=?',
                                       (pid,)).fetchone() if vibe_provenance.available(self.conn) else None
            legacy = ([] if marker else [row[0] for row in self.conn.execute(
                'SELECT measurement_id FROM prediction_inputs WHERE prediction_id=?', (pid,))])
            provenance[pid] = (*tuple(marker or (None, None)), vibe_models.linked(self.conn, pid), legacy)
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
                'artists': present_artists, 'provenance': provenance}

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
    def publish_prepared(self, plan, *, cohorts=()):
        with self.conn.atomic():
            measured = vibe_versions.column(self.conn, 'measurements')
            predicted = vibe_versions.column(self.conn, 'predictions')
            current = vibe_versions.column(self.conn, 'recording_vibes')
            versions = {row[index] for rows, index in ((plan.measurements, 6), (plan.predictions, 7), (plan.publications, 7))
                        for row in rows}
            versions = {version: vibe_versions.stored(self.conn, version) for version in versions}
            self.conn.executemany(f'INSERT INTO measurements(id,recording_id,vibe,score,support,source,{measured},input_digest,measured_at) '
                'VALUES (?,?,?,?,?,?,?,?,?)', [(*row[:6], versions[row[6]], *row[7:]) for row in plan.measurements])
            self.conn.executemany('INSERT OR IGNORE INTO measurement_samples VALUES (?,?)', plan.samples)
            self.conn.executemany(f'INSERT INTO predictions(id,recording_id,vibe,score,support,subgenre_key,kind,{predicted},predicted_at) '
                'VALUES (?,?,?,?,?,?,?,?,?)', [(*row[:7], versions[row[7]], row[8]) for row in plan.predictions])
            self.conn.executemany('INSERT INTO prediction_model_generations VALUES (?,?)', plan.generations)
            self.conn.executemany('INSERT OR IGNORE INTO prediction_measurement_roots VALUES (?)', [(mid,) for mid in plan.roots])
            self.conn.executemany("INSERT INTO prediction_provenance VALUES (?,?,?,'ready')", plan.markers)
            self.conn.executemany('DELETE FROM recording_vibes WHERE recording_id=? AND vibe=?', plan.deleted)
            self.conn.executemany('INSERT INTO recording_vibes VALUES (?,?,?,?,?,?,?,?,?,?,?,?) '
                'ON CONFLICT(recording_id,vibe) DO UPDATE SET score=excluded.score,support=excluded.support,'
                'source=excluded.source,measurement_id=excluded.measurement_id,prediction_id=excluded.prediction_id,'
                f'{current}=excluded.{current},artist_key=excluded.artist_key,subgenre_key=excluded.subgenre_key,'
                'recordings=excluded.recordings,variance=excluded.variance',
                [(*row[:7], versions[row[7]], *row[8:]) for row in plan.publications])
            self._queue(cohorts)

    @synchronized
    def pending(self):
        return (self.conn.execute("SELECT group_key,revision,cursor FROM rec_vibe_pending WHERE group_key='*'").fetchone()
                or self.conn.execute('SELECT group_key,revision,cursor FROM rec_vibe_pending ORDER BY position LIMIT 1').fetchone())

    @synchronized
    def has_joint_scores(self, generations):
        if not generations:
            return False
        if len(generations) > 8:
            raise ValueError('Joint score acknowledgement exceeds the channel bound')
        marks = ','.join('?' for _ in generations)
        return self.conn.execute('SELECT 1 FROM prediction_model_generations g '
            'JOIN predictions p ON p.id=g.prediction_id '
            'JOIN recording_vibes v ON v.recording_id=p.recording_id AND v.vibe=p.vibe AND v.prediction_id=p.id '
            f'WHERE g.generation IN ({marks}) LIMIT 1', [bytes.fromhex(value) for value in generations]).fetchone() is not None

    @synchronized
    def fanout(self, limit=32):
        worked, remaining = False, limit
        with self.conn.atomic():
            for _ in range(limit):
                check_stopping()
                pending = self.pending()
                if not pending or not remaining:
                    break
                worked = True
                key, revision, cursor = pending
                if key == '*':
                    rows = self.conn.execute('SELECT identity_key FROM recordings WHERE identity_key>? '
                        'ORDER BY identity_key LIMIT ?', (cursor, remaining)).fetchall()
                else:
                    where, args = self._cohort(*json.loads(key))
                    # Own measurements do not consume cohort estimates. A
                    # partial recording still appears through any fallback
                    # channel; model-version refreshes above visit everyone.
                    rows = self.conn.execute('SELECT DISTINCT r.identity_key FROM recording_vibes v '
                        f'JOIN recordings r ON r.id=v.recording_id WHERE {where} AND v.measurement_id IS NULL '
                        'AND r.identity_key>? '
                        'ORDER BY r.identity_key LIMIT ?', (*args, cursor, remaining)).fetchall()
                for identity, in rows:
                    self.conn.execute('INSERT INTO rec_content_pending(identity_key) VALUES (?) '
                        'ON CONFLICT(identity_key) DO UPDATE SET revision=revision+1', (identity,))
                self.conn.execute('DELETE FROM rec_vibe_pending WHERE group_key=? AND revision=? AND cursor=?',
                                  (key, revision, cursor))
                if len(rows) == remaining:
                    self.conn.execute('INSERT INTO rec_vibe_pending(group_key,revision,cursor) VALUES (?,?,?)',
                                      (key, revision, rows[-1][0]))
                remaining -= len(rows)
        return worked

    @staticmethod
    def _cohort(kind, artist, genre):
        return {'artist_genre': ('v.artist_key=? AND v.subgenre_key=?', (artist, genre)),
                'artist': ('v.artist_key=?', (artist,)), 'genre': ('v.subgenre_key=?', (genre,))}[kind]

    @synchronized
    def queue(self, keys):
        with self.conn.atomic():
            self._queue(keys)

    def _queue(self, keys):
        self.conn.executemany('INSERT INTO rec_vibe_pending(group_key) VALUES (?) '
            "ON CONFLICT(group_key) DO UPDATE SET revision=revision+1,cursor=''", [(key,) for key in keys])

    @synchronized
    def current_facts(self, rid, *, projection=None):
        with keyed_reads(self._store, 'vibe_versions', []):
            projection = projection or vibe_versions.projection(self.conn, 'recording_vibes', 'v')
            rows = [dict(row) for row in self.conn.execute('SELECT ' + projection +
                ',m.input_digest FROM recording_vibes v LEFT JOIN measurements m ON m.id=v.measurement_id '
                'WHERE v.recording_id=? ORDER BY v.vibe', (rid,))]
        ids = [sid for sid, in self.conn.execute('SELECT s.sample_id FROM recording_vibes v '
            'JOIN measurement_samples s ON s.measurement_id=v.measurement_id '
            'WHERE v.recording_id=? ORDER BY v.vibe,s.sample_id', (rid,))]
        return rows, ids

    @synchronized
    def input_facts(self, keys):
        result = {}
        with ExitStack() as guards:
            guards.enter_context(keyed_reads(self._store, 'recordings', keys))
            ids = {key: row[0] for key in keys for row in self.conn.execute(
                'SELECT id FROM recordings WHERE identity_key=?', (key,))}
            for table in ('recording_vibes', 'sample_attributes', 'catalog_entries', 'rec_musical_links'):
                guards.enter_context(keyed_reads(self._store, table, ids.values()))
            entries = [r[0] for rid in ids.values() for r in self.conn.execute(
                'SELECT id FROM catalog_entries WHERE recording_id=?', (rid,))]
            from yt_playlist.core import metadata_runtime
            choices = (('metadata_runtime_choices',) if metadata_runtime.active(self.conn) else
                       ('metadata_choice_heads', 'metadata_observations') if metadata_evidence.available(self.conn) else ('entry_field_choices',))
            for table in (self.entry_artists_table, *choices, 'unresolved_items'):
                guards.enter_context(keyed_reads(self._store, table, entries))
            sources = {r[0] for rid in ids.values() for r in self.conn.execute(
                'SELECT source_key FROM rec_musical_links WHERE recording_id=?', (rid,))}
            guards.enter_context(keyed_reads(self._store, 'rec_musical_sources', sources))
            if metadata_native.available(self.conn):
                guards.enter_context(keyed_reads(self._store, 'musical_source_observations', sources))
                with keyed_reads(self._store, 'native_metadata_observations', []):
                    references = [r for source in sources for r in self.conn.execute(
                        'SELECT o.id,o.delivery_id FROM musical_source_observations s '
                        'JOIN native_metadata_observations o ON o.id=s.observation_id WHERE s.source_key=?', (source,))]
                guards.enter_context(keyed_reads(self._store, 'native_metadata_observations', [r[0] for r in references]))
                guards.enter_context(keyed_reads(self._store, 'metadata_deliveries', [r[1] for r in references]))
            with keyed_reads(self._store, 'vibe_versions', []):
                projection = vibe_versions.projection(self.conn, 'recording_vibes', 'v')
            for key in keys:
                check_stopping()
                rid = ids.get(key)
                old = self.current_facts(rid, projection=projection) if rid else ([], [])
                artists = [r[0] for r in self.conn.execute('SELECT DISTINCT a.artist_id FROM catalog_entries e '
                    f'JOIN {self.entry_artists_table} a ON a.entry_id=e.id WHERE e.recording_id=? ORDER BY a.artist_id LIMIT 17', (rid,))]
                runs = self.conn.execute('SELECT run_id,video_id,MAX(captured_at) AS captured FROM sample_attributes '
                    'WHERE recording_id=? GROUP BY run_id,video_id HAVING COUNT(*)=3 '
                    'ORDER BY captured DESC,video_id DESC,run_id DESC LIMIT 16', (rid,)).fetchall()
                samples = []
                for run in runs:
                    rows = [dict(row) for row in self.conn.execute(
                        'SELECT id,run_id,point,video_id,filename FROM sample_attributes WHERE run_id=? '
                        'AND recording_id=? ORDER BY point', (run[0], rid))]
                    samples.append((rows, audio_vibe_metadata.read_facts(self.conn, run[1])))
                result[key] = {'old': old, 'artists': artists, 'runs': samples, 'recording': rid}
        return result

    @synchronized
    def cohort_rows(self, key, after=(0, ''), limit=128):
        where, args = self._cohort(*json.loads(key))
        with keyed_reads(self._store, 'measurements', []), column_group_reads(self._store, 'recordings', 'identity'):
            return [tuple(row) for row in self.conn.execute(
                'SELECT v.recording_id,r.identity_key,m.id,m.vibe,m.score,m.support FROM recording_vibes v '
                'JOIN recordings r ON r.id=v.recording_id JOIN measurements m ON m.id=v.measurement_id '
                f'WHERE {where} AND m.support>0 AND (v.recording_id,v.vibe)>(?,?) '
                'ORDER BY v.recording_id,v.vibe LIMIT ?', (*args, *after, limit))]

    @synchronized
    def joint_training_rows(self, vibe, limit):
        return vibe_models.training_rows(self.conn, vibe, limit)

    @synchronized
    def album_facts(self, recordings):
        return vibe_models.album_facts(self.conn, recordings)

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
        with keyed_reads(self._store, 'catalog_entries', ids.values()):
            albums = vibe_models.album_facts(self.conn, ids.values())
        return ids, albums

    @synchronized
    def root_joint_model(self, measurements, refresh):
        with self.conn.atomic():
            vibe_models.root_measurements(self.conn, measurements)
            if refresh:
                self._queue(['*'])

    @synchronized
    def retained_joint_fits(self, keys):
        result = {}
        with keyed_reads(self._store, 'recordings', keys):
            ids = {key: row[0] for key in keys for row in self.conn.execute(
                'SELECT id FROM recordings WHERE identity_key=?', (key,))}
        with keyed_reads(self._store, 'recording_vibes', ids.values()):
            for key, rid in ids.items():
                result[key] = [dict(row) for row in self.conn.execute('SELECT v.* FROM recording_vibes v '
                    'JOIN prediction_model_generations g ON g.prediction_id=v.prediction_id WHERE recording_id=?', (rid,))]
        return result

    @synchronized
    def overlay_rows(self, keys):
        with keyed_reads(self._store, 'recordings', keys):
            ids = {r[0]: r[1] for r in self.conn.execute('SELECT id,identity_key FROM recordings '
                'WHERE identity_key IN (' + ','.join('?' for _ in keys) + ')', keys)}
        with keyed_reads(self._store, 'recording_vibes', ids), keyed_reads(self._store, 'vibe_versions', []):
            return [(ids[row['recording_id']], dict(row)) for row in self.conn.execute(
                'SELECT recording_id,vibe,score,support,source,' +
                vibe_versions.expression(self.conn, 'recording_vibes', 'v') + ' AS inputs_version,recordings,variance '
                'FROM recording_vibes v WHERE recording_id IN (' + ','.join('?' for _ in ids) + ')', list(ids))]

    @snapshot_read
    def fit_scores(self, keys, preset):
        keys, result = list(dict.fromkeys(keys)), {}
        for offset in range(0, len(keys), 512):
            page = keys[offset:offset + 512]
            marks = ','.join('?' for _ in page)
            with keyed_reads(self._store, 'recordings', page), keyed_reads(self._store, 'rec_musical_profiles', page):
                result.update(dict(self.conn.execute('SELECT r.identity_key,v.score FROM recordings r '
                    'JOIN rec_musical_profiles p ON p.identity_key=r.identity_key '
                    'JOIN recording_vibes v ON v.recording_id=r.id '
                    f'WHERE r.identity_key IN ({marks}) AND v.vibe=?', (*page, preset))))
        return result

    @synchronized
    def ranked(self, vibe, limit=100):
        return [dict(row) for row in self.conn.execute('SELECT recording_id,score,support,source FROM recording_vibes '
            'WHERE vibe=? ORDER BY score DESC,recording_id LIMIT ?', (vibe, limit))]
