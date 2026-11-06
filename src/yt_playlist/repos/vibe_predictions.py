"""Normalized vibe history, its serving cache and bounded dependency work."""
from contextlib import ExitStack
from dataclasses import dataclass
import json

from yt_playlist.core import audio_vibe_metadata, genre_identity, metadata_evidence, metadata_native, sample_files, vibe_model_files, vibe_provenance, vibe_versions
from yt_playlist.core.audio_vibe_contract import INTENSITY_CHANNELS, PREDICTION_VERSION
from yt_playlist.core.activity import WorkBudgetExceeded, check_stopping
from yt_playlist.core.db_revisions import column_group_reads, keyed_reads
from yt_playlist.repos.base import Repo, snapshot_read, synchronized
from yt_playlist.repos import vibe_models


@dataclass
class PreparedVibes:
    measurements: list
    samples: list
    predictions: list
    publications: list
    deleted: list
    cohorts: set
    staged: list
    markers: list
    roots: list
    generations: list

    def close(self):
        for publication in self.staged:
            publication.close()


class VibePredictionsRepo(Repo):
    @synchronized
    def prepare_publication(self, tracks, resolved, touched, now):
        """Plan original IDs, reuse and exact file contents on the private reader."""
        if not getattr(self._store,'_read_only',False):
            raise RuntimeError('Vibe preparation requires a guarded private snapshot')
        with keyed_reads(self._store, 'recordings', tracks):
            qs = ','.join('?' for _ in tracks)
            ids = dict(self.conn.execute(
                f'SELECT identity_key,id FROM recordings WHERE identity_key IN ({qs})', list(tracks)))
        next_mid = self.conn.execute('SELECT coalesce(max(id),0)+1 FROM measurements').fetchone()[0]
        next_pid = vibe_provenance.next_id(self.conn)
        measured_version = vibe_versions.column(self.conn, 'measurements')
        with keyed_reads(self._store, 'vibe_versions', []):
            stamps = {version: vibe_versions.lookup(self.conn, version)
                      for version in {track['version'] for track in tracks.values() if track}}
            previous_projection = vibe_versions.projection(self.conn, 'recording_vibes', 'v')
            previous_rows = {}
            rqs = ','.join('?' for _ in ids)
            for row in self.conn.execute('SELECT '+previous_projection+
                    f' FROM recording_vibes v WHERE recording_id IN ({rqs})', list(ids.values())):
                previous_rows.setdefault(row['recording_id'], {})[row['vibe']] = row
        measurements, samples, mids, originals = [], [], {}, {}
        requests = [(ids[key], stamps[track['version']], track['evidence']['input_digest'])
                    for key, track in tracks.items() if key in ids and track and track['evidence']]
        current_measurements = {}
        if requests:
            values = ','.join('(?,?,?)' for _ in requests)
            current_measurements = {(row[0], row[1]): tuple(row[2:]) for row in self.conn.execute(
                'WITH wanted(recording_id,version,digest) AS (VALUES '+values+') '
                'SELECT m.recording_id,m.vibe,m.id,m.score,m.support,m.source FROM wanted w '
                'JOIN measurements m ON m.recording_id=w.recording_id AND '
                f'm.{measured_version}=w.version AND m.input_digest=w.digest',
                [value for request in requests for value in request])}
        for key,track in tracks.items():
            if key not in ids or not track or not track['evidence']:
                continue
            evidence, rid = track['evidence'],ids[key]
            for vibe,fit in evidence['fits'].items():
                row = current_measurements.get((rid, vibe))
                if row:
                    mid = row[0]
                    if tuple(row[1:]) != (fit['score'],fit['support'],evidence['source']):
                        raise ValueError('Original measurement identity changed its score')
                else:
                    mid,next_mid = next_mid,next_mid+1
                    measurements.append((mid,rid,vibe,fit['score'],fit['support'],evidence['source'],
                                         track['version'],evidence['input_digest'],now))
                originals[mid] = (vibe,rid)
                mids[key,vibe] = mid
                samples.extend((mid,sid) for sid in evidence.get('sample_ids',[]))
        # Resolve the existing original identities once for the entire batch.
        # The exact same per-prediction distinct-recording checks below remain;
        # shared cohort contributors no longer require thousands of point reads.
        referenced = {mids.get((identity, vibe), original)
                      for key, fits in resolved.items() if key in ids
                      for vibe, fit in fits.items()
                      if not fit.get('retain') and not fit.get('generation')
                      and fit['source'] in ('artist_genre', 'artist', 'genre')
                      for identity, original in fit['inputs'].items()}
        referenced = [mid for mid in referenced if mid is not None and mid not in originals]
        for start in range(0, len(referenced), 128):
            check_stopping()
            batch = referenced[start:start + 128]
            qs = ','.join('?' for _ in batch)
            originals.update((row[0], tuple(row[1:])) for row in self.conn.execute(
                f'SELECT id,vibe,recording_id FROM measurements WHERE id IN ({qs})', batch))
        predictions, provenance, publications, deleted, generations = [], [], [], [], []
        cohorts = set(touched)
        validated_genres, validated_artists = set(), set()
        for key,track in tracks.items():
            rid = ids.get(key)
            if rid is None:
                continue
            previous = previous_rows.get(rid, {})
            for vibe in previous.keys()-resolved.get(key,{}).keys():
                deleted.append((rid,vibe))
            if track is None:
                continue
            if track['genre'] and track['genre'] not in validated_genres:
                genre,reason = genre_identity.resolve(self.conn,track['genre'])
                if reason is not None or genre != track['genre']:
                    raise ValueError('Prediction conditioning requires a canonical specific genre key')
                validated_genres.add(genre)
            for vibe,fit in resolved.get(key,{}).items():
                if fit.get('retain'):
                    continue
                source,mid,pid = fit['source'],mids.get((key,vibe)),None
                if source in ('artist_genre','artist','genre'):
                    model = fit.get('generation')
                    if model:
                        old = previous.get(vibe)
                        artists = json.loads(track['artist'] or '[]')
                        for artist in artists:
                            if artist not in validated_artists and not self.conn.execute(
                                    'SELECT 1 FROM artists WHERE id=?', (artist,)).fetchone():
                                raise ValueError('Unknown conditioning artist')
                            validated_artists.add(artist)
                        conditioning = fit.get('conditioning')
                        receipt = vibe_model_files.receipt(model, artists, fit['mean_variance'], fit['new_track_variance'],
                                                           conditioning=conditioning)
                        if conditioning is not None and conditioning['genre'] != track['genre']:
                            raise ValueError('Joint target genre differs from publication conditioning')
                        stamp = track['version'] + '/' + fit['model_version']
                        if (old and old['prediction_id'] is not None and old['source'] == source
                                and old['score'] == fit['score'] and old['support'] == fit['support']
                                and old['artist_key'] == track['artist'] and old['subgenre_key'] == track['genre']
                                and old['inputs_version'] == stamp
                                and vibe_provenance.same_receipt(self.conn, old['prediction_id'], receipt)):
                            pid = old['prediction_id']
                        else:
                            # Model files are already durable and pinned by this
                            # reader. The generation manifest was validated at fit
                            # publication; no per-target contributor graph is built.
                            pid, next_pid = next_pid, next_pid + 1
                            predictions.append((pid,rid,vibe,fit['score'],fit['support'],
                                track['genre'] if source != 'artist' else None,source,stamp,now))
                            provenance.append((pid,receipt,artists))
                            generations.append((pid,bytes.fromhex(model)))
                        mid = None
                        values = (rid,vibe,fit['score'],fit['support'],source,None,pid,stamp,track['artist'],
                                  track['genre'],fit.get('recordings'),None)
                        if vibe not in previous or tuple(previous[vibe]) != values:
                            publications.append(values)
                        continue
                    inputs = {mids.get((identity,vibe),original) for identity,original in fit['inputs'].items()}
                    if not inputs or None in inputs:
                        raise ValueError('A prediction requires original measurement inputs')
                    old = previous.get(vibe)
                    if (old and old['prediction_id'] is not None and old['source']==source
                            and old['score']==fit['score'] and old['support']==fit['support']
                            and old['inputs_version']==track['version'] and old['artist_key']==track['artist']
                            and old['subgenre_key']==track['genre']
                            and vibe_provenance.same_inputs(self.conn,old['prediction_id'],inputs)):
                        pid = old['prediction_id']
                    if pid is None:
                        subjects = set()
                        for original in inputs:
                            subject = originals.get(original)
                            if not subject or subject[0]!=vibe or subject[1]==rid or subject[1] in subjects:
                                raise ValueError('Prediction inputs require distinct original recordings of the same vibe')
                            subjects.add(subject[1])
                        artists = json.loads(track['artist'] or '[]') if source!='genre' else []
                        if source!='genre' and not artists:
                            raise ValueError('Complete calculation requires original artist conditioning')
                        for artist in artists:
                            if artist not in validated_artists and not self.conn.execute(
                                    'SELECT 1 FROM artists WHERE id=?',(artist,)).fetchone():
                                raise ValueError('Unknown conditioning artist')
                            validated_artists.add(artist)
                        pid,next_pid = next_pid,next_pid+1
                        predictions.append((pid,rid,vibe,fit['score'],fit['support'],
                            track['genre'] if source!='artist' else None,source,track['version'],now))
                        provenance.append((pid,inputs,artists))
                    mid = None
                values = (rid,vibe,fit['score'],fit['support'],source,mid,pid,track['version'],track['artist'],
                          track['genre'],fit.get('recordings'),fit.get('variance'))
                if vibe not in previous or tuple(previous[vibe])!=values:
                    publications.append(values)
        staged,markers,roots = vibe_provenance.stage_many(self.conn,provenance)
        return PreparedVibes(measurements,samples,predictions,publications,deleted,cohorts,staged,markers,roots,generations)

    @synchronized
    def publish_prepared(self, plan):
        """Only prepared SQL rows and verified atomic file renames under the writer."""
        if not self.conn.in_transaction:
            raise RuntimeError('Vibe predictions require guarded publication')
        measured_version = vibe_versions.column(self.conn, 'measurements')
        predicted_version = vibe_versions.column(self.conn, 'predictions')
        current_version = vibe_versions.column(self.conn, 'recording_vibes')
        versions = {row[index] for rows,index in ((plan.measurements,6),(plan.predictions,7),(plan.publications,7))
                    for row in rows}
        versions = {version:vibe_versions.stored(self.conn, version) for version in versions}
        self.conn.executemany(f'INSERT INTO measurements(id,recording_id,vibe,score,support,source,{measured_version},input_digest,measured_at) '
                              'VALUES (?,?,?,?,?,?,?,?,?)', [(*row[:6],versions[row[6]],*row[7:]) for row in plan.measurements])
        self.conn.executemany('INSERT OR IGNORE INTO measurement_samples VALUES (?,?)',plan.samples)
        self.conn.executemany(f'INSERT INTO predictions(id,recording_id,vibe,score,support,subgenre_key,kind,{predicted_version},predicted_at) '
                              'VALUES (?,?,?,?,?,?,?,?,?)', [(*row[:7],versions[row[7]],row[8]) for row in plan.predictions])
        for publication in plan.staged:
            publication.publish()
        self.conn.executemany('INSERT INTO prediction_model_generations VALUES (?,?)',plan.generations)
        self.conn.executemany('INSERT OR IGNORE INTO prediction_measurement_roots VALUES (?)',[(mid,) for mid in plan.roots])
        self.conn.executemany("INSERT INTO prediction_provenance VALUES (?,?,?,'ready')",plan.markers)
        self.conn.executemany('DELETE FROM recording_vibes WHERE recording_id=? AND vibe=?',plan.deleted)
        self.conn.executemany('INSERT INTO recording_vibes VALUES (?,?,?,?,?,?,?,?,?,?,?,?) '
            'ON CONFLICT(recording_id,vibe) DO UPDATE SET score=excluded.score,support=excluded.support,'
            'source=excluded.source,measurement_id=excluded.measurement_id,prediction_id=excluded.prediction_id,'
            f'{current_version}=excluded.{current_version},artist_key=excluded.artist_key,subgenre_key=excluded.subgenre_key,'
            'recordings=excluded.recordings,variance=excluded.variance',
            [(*row[:7],versions[row[7]],*row[8:]) for row in plan.publications])
        repairs = {key.removeprefix('!joint:') for key in plan.cohorts if key.startswith('!joint:')}
        if repairs:
            self._store.settings.publish_values({vibe_model_files.STATE_PREFIX + vibe: '' for vibe in repairs})
        self._queue(sorted(key for key in plan.cohorts if not key.startswith('!joint:')))
        if plan.deleted or plan.publications:
            self._store.rec_refresh.publish_notification()

    @synchronized
    def pending(self):
        return (self.conn.execute("SELECT group_key,revision,cursor FROM rec_vibe_pending WHERE group_key='*'").fetchone()
                or self.conn.execute('SELECT group_key,revision,cursor FROM rec_vibe_pending ORDER BY position LIMIT 1').fetchone())

    @synchronized
    def has_joint_scores(self, generations):
        """Indexed acknowledgement that a current model cycle reached scoring."""
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
        with self.conn:
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
                    kind, artist, genre = json.loads(key)
                    where, args = self._cohort(kind, artist, genre)
                    rows = self.conn.execute('SELECT DISTINCT r.identity_key FROM recording_vibes v '
                        f'JOIN recordings r ON r.id=v.recording_id WHERE {where} AND r.identity_key>? '
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
    def ensure_version(self, version):
        if self._store.settings.get_setting('rec_vibe_version') == version:
            return False
        with self.conn:
            self._queue(['*'])
            self._store.settings.publish_values({'rec_vibe_version': version})
        return True

    def _queue(self, keys):
        self.conn.executemany('INSERT INTO rec_vibe_pending(group_key) VALUES (?) '
            "ON CONFLICT(group_key) DO UPDATE SET revision=revision+1,cursor=''", [(key,) for key in keys])

    def _current(self, rid, *, projection=None):
        with keyed_reads(self._store, 'vibe_versions', []):
            projection = projection or vibe_versions.projection(self.conn, 'recording_vibes', 'v')
            rows = self.conn.execute('SELECT '+projection+
                ',m.input_digest FROM recording_vibes v LEFT JOIN measurements m ON m.id=v.measurement_id '
                'WHERE v.recording_id=? ORDER BY v.vibe', (rid,)).fetchall()
        if not rows:
            return None
        own = [row for row in rows if row['measurement_id'] is not None]
        evidence = None
        if own:
            ids = sorted({sid for row in own for sid, in self.conn.execute(
                'SELECT sample_id FROM measurement_samples WHERE measurement_id=?', (row['measurement_id'],))})
            evidence = {'source': own[0]['source'], 'input_digest': own[0]['input_digest'], 'sample_ids': ids,
                        'fits': {row['vibe']: {'score': row['score'], 'support': row['support']} for row in own}}
        return {'artist': rows[0]['artist_key'], 'genre': rows[0]['subgenre_key'], 'evidence': evidence,
                'version': rows[0]['inputs_version']}

    @synchronized
    def inputs(self, keys, max_bytes=8 * 1024 * 1024):
        result, used = {}, 0
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
                        'JOIN native_metadata_observations o ON o.id=s.observation_id WHERE s.source_key=?',
                        (source,))]
                guards.enter_context(keyed_reads(self._store, 'native_metadata_observations',
                                                [r[0] for r in references]))
                guards.enter_context(keyed_reads(self._store, 'metadata_deliveries', [r[1] for r in references]))
            with keyed_reads(self._store, 'vibe_versions', []):
                projection = vibe_versions.projection(self.conn, 'recording_vibes', 'v')
            for key in keys:
                check_stopping()
                rid = ids.get(key)
                old = self._current(rid, projection=projection) if rid else None
                artists = [r[0] for r in self.conn.execute('SELECT DISTINCT a.artist_id FROM catalog_entries e '
                    f'JOIN {self.entry_artists_table} a ON a.entry_id=e.id WHERE e.recording_id=? ORDER BY a.artist_id LIMIT 17', (rid,))]
                artist = json.dumps(artists, separators=(',', ':')) if 0 < len(artists) <= 16 else None
                runs = self.conn.execute('SELECT run_id,video_id,MAX(captured_at) AS captured FROM sample_attributes '
                    'WHERE recording_id=? GROUP BY run_id,video_id HAVING COUNT(*)=3 '
                    'ORDER BY captured DESC,video_id DESC,run_id DESC LIMIT 16', (rid,)).fetchall()
                samples, metadata = [], {}
                for run in runs:
                    captured = []
                    for row in self.conn.execute('SELECT id,run_id,point,video_id,filename FROM sample_attributes WHERE run_id=? '
                                                 'AND recording_id=? ORDER BY point', (run[0], rid)):
                        try:
                            sample = sample_files.read(row['filename'], expected={name: row[name]
                                for name in ('id', 'run_id', 'point', 'video_id')})
                        except (OSError, ValueError):
                            break  # Try another complete playback or provider evidence.
                        used += len(sample_files.canonical(sample))
                        if used > max_bytes:
                            raise WorkBudgetExceeded('Vibe measurements exceed input budget')
                        captured.append(sample)
                    if len(captured) == 3:
                        samples, metadata = captured, audio_vibe_metadata.read(self.conn, run[1])
                        break
                result[key] = {'old': old, 'artist': artist, 'samples': samples, 'metadata': metadata, 'recording': rid}
        return result

    @synchronized
    def groups(self, keys):
        """Current original measurements, one per recording/vibe; no projected scores."""
        result, identities = {}, {}
        for key in keys:
            check_stopping()
            where, args = self._cohort(*json.loads(key))
            # Referenced calculations cannot change or disappear: immutable
            # rows plus the cache FK protect this join. Cache/identity revisions
            # guard membership; unrelated appended calculation history is safe.
            with keyed_reads(self._store, 'measurements', []), column_group_reads(self._store, 'recordings', 'identity'):
                rows = self.conn.execute('SELECT r.identity_key,m.id,m.vibe,m.score,m.support FROM recording_vibes v '
                    'JOIN recordings r ON r.id=v.recording_id JOIN measurements m ON m.id=v.measurement_id '
                    f'WHERE {where} AND m.support>0 ORDER BY v.recording_id,v.vibe', args)
            stats, inputs = {}, {}
            while batch := rows.fetchmany(128):
                for identity, mid, vibe, score, support in batch:
                    check_stopping()
                    identity = identities.setdefault(identity, identity)
                    inputs.setdefault(vibe, {})[identity] = mid
                    values = stats.setdefault(vibe, [0, 0., 0., 0.])
                    for i, amount in enumerate((1, support, support * score, support * score * score)):
                        values[i] += amount
            result[key] = {'stats': stats, 'inputs': inputs}
        return result

    @synchronized
    def joint_inputs(self, vibe, limit):
        """Current originals only, once per canonical recording, exact versions.

        Album linkage is used only when one canonical browse identity is
        unambiguous. An upload, replay or extra provider never becomes a row.
        The guarded snapshot fences measurement and conditioning changes.
        """
        return vibe_models.training_manifest(self.conn, vibe, limit)

    @synchronized
    def joint_profile_rows(self, recordings):
        """A bounded batch of raw profile inputs, never prepared target scores."""
        if not 0 < len(recordings) <= 32:
            raise ValueError('Joint masked profile capture requires a bounded batch')
        keys = dict(self.conn.execute('SELECT id,identity_key FROM recordings WHERE id IN ('
                    + ','.join('?' for _ in recordings) + ')', recordings))
        if set(keys) != set(recordings):
            raise ValueError('Joint masked profile requires each original recording')
        return keys, self._store.prepared_content.capture([(key, 0) for key in keys.values()], mask_numeric=True)

    @synchronized
    def joint_albums(self, keys):
        ids = {key:row[0] for key in keys for row in self.conn.execute(
            'SELECT id FROM recordings WHERE identity_key=?', (key,))}
        albums = vibe_models.conditioning_albums(self.conn, ids.values())
        return {key:albums.get(rid) for key,rid in ids.items()}

    @synchronized
    def stage_joint_model(self, value):
        return vibe_models.stage(self.conn, value)

    @synchronized
    def joint_model(self, generation):
        return vibe_models.read(self.conn, generation)

    @synchronized
    def publish_joint_model(self, vibe, generation, outcome, measurements, staged, refresh):
        if not self.conn.in_transaction:
            raise RuntimeError('Joint model publication requires a guarded writer')
        if staged:
            staged.publish()
        vibe_models.root_measurements(self.conn, measurements)
        self._store.settings.publish_values({vibe_model_files.HEAD_PREFIX + vibe: generation,
                                            vibe_model_files.STATE_PREFIX + vibe: json.dumps(outcome)})
        if refresh:
            self._queue(['*'])

    @synchronized
    def defer_joint_model(self, vibe, outcome, refresh):
        """Keep the last head and atomically schedule healthy partial results."""
        with self.conn:
            if not self.conn.in_transaction:
                self.conn.execute('BEGIN IMMEDIATE')
            self._store.settings.publish_values({vibe_model_files.STATE_PREFIX + vibe: json.dumps(outcome)})
            if refresh:
                self._queue(['*'])

    @synchronized
    def retained_joint_fits(self, keys):
        result = {}
        with keyed_reads(self._store, 'recordings', keys):
            ids = {key:row[0] for key in keys for row in self.conn.execute(
                'SELECT id FROM recordings WHERE identity_key=?', (key,))}
        with keyed_reads(self._store, 'recording_vibes', ids.values()):
            for key, rid in ids.items():
                result[key] = {row['vibe']: {**{name:row[name] for name in ('score','support','source','recordings','variance')},
                                            'retain': True}
                    for row in self.conn.execute('SELECT v.* FROM recording_vibes v '
                        'JOIN prediction_model_generations g ON g.prediction_id=v.prediction_id WHERE recording_id=?', (rid,))}
        return result

    @synchronized
    def overlay(self, profiles):
        """Serve relational results without another persistent score copy in JSON."""
        if not profiles:
            return profiles
        keys = list(profiles)
        with keyed_reads(self._store, 'recordings', keys):
            ids = {r[0]: r[1] for r in self.conn.execute('SELECT id,identity_key FROM recordings '
                'WHERE identity_key IN (' + ','.join('?' for _ in keys) + ')', keys)}
        with keyed_reads(self._store, 'recording_vibes', ids), keyed_reads(self._store, 'vibe_versions', []):
            rows = self.conn.execute('SELECT recording_id,vibe,score,support,source,'+
                vibe_versions.expression(self.conn, 'recording_vibes', 'v')+' AS inputs_version,recordings,variance '
                'FROM recording_vibes v WHERE recording_id IN (' + ','.join('?' for _ in ids) + ')', list(ids))
            for row in rows:
                profile = profiles[ids[row['recording_id']]]
                vibes = profile.setdefault('vibes', {'version': PREDICTION_VERSION,
                    'inputs_version': row['inputs_version'], 'fits': {}})
                fit = {name: row[name] for name in ('score', 'support', 'source')}
                if row['recordings'] is not None:
                    fit.update({name: row[name] for name in ('recordings', 'variance')})
                if row['vibe'] in INTENSITY_CHANNELS:
                    vibes.setdefault('intensity_fits', {})[INTENSITY_CHANNELS[row['vibe']]] = fit
                else:
                    vibes['fits'][row['vibe']] = fit
        return profiles

    @snapshot_read
    def fit_scores(self, keys, preset):
        """Read the relational scores overlaid on prepared recording profiles.

        Ranking needs one score; exact profile bodies are read for the selected
        tracks and for candidates without a relational specialist result.
        """
        keys = list(dict.fromkeys(keys))
        result = {}
        for offset in range(0, len(keys), 512):
            page = keys[offset:offset + 512]
            marks = ','.join('?' for _ in page)
            with keyed_reads(self._store, 'recordings', page), \
                    keyed_reads(self._store, 'rec_musical_profiles', page):
                result.update(dict(self.conn.execute('SELECT r.identity_key,v.score FROM recordings r '
                    'JOIN rec_musical_profiles p ON p.identity_key=r.identity_key '
                    'JOIN recording_vibes v ON v.recording_id=r.id '
                    f'WHERE r.identity_key IN ({marks}) AND v.vibe=?', (*page, preset))))
        return result

    @synchronized
    def ranked(self, vibe, limit=100):
        return [dict(row) for row in self.conn.execute('SELECT recording_id,score,support,source FROM recording_vibes '
            'WHERE vibe=? ORDER BY score DESC,recording_id LIMIT ?', (vibe, limit))]
