"""Normalized vibe history, its serving cache and bounded dependency work."""
from contextlib import ExitStack
import json

from yt_playlist.core import audio_vibe_metadata, sample_files
from yt_playlist.core.audio_vibe_contract import PREDICTION_VERSION
from yt_playlist.core.activity import WorkBudgetExceeded, check_stopping
from yt_playlist.core.db_revisions import column_group_reads, keyed_reads
from yt_playlist.repos.base import Repo, synchronized


class VibePredictionsRepo(Repo):
    @synchronized
    def pending(self):
        return (self.conn.execute("SELECT group_key,revision,cursor FROM rec_vibe_pending WHERE group_key='*'").fetchone()
                or self.conn.execute('SELECT group_key,revision,cursor FROM rec_vibe_pending ORDER BY position LIMIT 1').fetchone())

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

    def _current(self, rid):
        rows = self.conn.execute('SELECT v.*,m.input_digest,m.model_version FROM recording_vibes v '
            'LEFT JOIN measurements m ON m.id=v.measurement_id WHERE v.recording_id=? ORDER BY v.vibe', (rid,)).fetchall()
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
            for table in ('track_artists', 'entry_field_choices', 'unresolved_items'):
                guards.enter_context(keyed_reads(self._store, table, entries))
            for key in keys:
                check_stopping()
                rid = ids.get(key)
                old = self._current(rid) if rid else None
                artists = [r[0] for r in self.conn.execute('SELECT DISTINCT a.artist_id FROM catalog_entries e '
                    'JOIN track_artists a ON a.entry_id=e.id WHERE e.recording_id=? ORDER BY a.artist_id LIMIT 17', (rid,))]
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
                result[key] = {'old': old, 'artist': artist, 'samples': samples, 'metadata': metadata}
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
            for identity, mid, vibe, score, support in rows:
                check_stopping()
                identity = identities.setdefault(identity, identity)
                inputs.setdefault(vibe, {})[identity] = mid
                values = stats.setdefault(vibe, [0, 0., 0., 0.])
                for i, amount in enumerate((1, support, support * score, support * score * score)):
                    values[i] += amount
            result[key] = {'stats': stats, 'inputs': inputs}
        return result

    @synchronized
    def publish(self, tracks, resolved, touched, now):
        """History, prediction inputs and current scores commit together."""
        if not self.conn.in_transaction:
            raise RuntimeError('Vibe predictions require guarded publication')
        ids = {key: row[0] for key in tracks for row in self.conn.execute(
            'SELECT id FROM recordings WHERE identity_key=?', (key,))}
        mids = {}
        for key, track in tracks.items():
            if not track or not track['evidence']:
                continue
            evidence, rid = track['evidence'], ids[key]
            for vibe, fit in evidence['fits'].items():
                args = (rid, vibe, track['version'], evidence['input_digest'])
                self.conn.execute('INSERT INTO measurements(recording_id,vibe,score,support,source,model_version,input_digest,measured_at) '
                    'VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(recording_id,vibe,model_version,input_digest) DO NOTHING',
                    (rid, vibe, fit['score'], fit['support'], evidence['source'], track['version'], evidence['input_digest'], now))
                mid = self.conn.execute('SELECT id FROM measurements WHERE recording_id=? AND vibe=? '
                                        'AND model_version=? AND input_digest=?', args).fetchone()[0]
                mids[key, vibe] = mid
                self.conn.executemany('INSERT OR IGNORE INTO measurement_samples VALUES (?,?)',
                                      [(mid, sid) for sid in evidence.get('sample_ids', [])])
        changed = False
        for key, track in tracks.items():
            rid = ids.get(key)
            if rid is None:
                continue
            if track is None:
                changed |= self.conn.execute('DELETE FROM recording_vibes WHERE recording_id=?', (rid,)).rowcount > 0
                continue
            if track['genre']:
                from yt_playlist.util import genre_catalog
                self.conn.execute("INSERT OR IGNORE INTO genre_catalog(key,name,source) VALUES (?,?,'custom')",
                                   (track['genre'], genre_catalog.canonical_name(track['genre'])))
            previous = {row['vibe']: row for row in self.conn.execute('SELECT * FROM recording_vibes WHERE recording_id=?', (rid,))}
            for vibe in previous.keys() - resolved.get(key, {}).keys():
                self.conn.execute('DELETE FROM recording_vibes WHERE recording_id=? AND vibe=?', (rid, vibe))
                changed = True
            for vibe, fit in resolved.get(key, {}).items():
                source, mid, pid = fit['source'], mids.get((key, vibe)), None
                if source in ('artist_genre', 'artist', 'genre'):
                    inputs = {mids.get((identity, vibe), mid) for identity, mid in fit['inputs'].items()}
                    if not inputs or None in inputs:
                        raise ValueError('A prediction requires original measurement inputs')
                    old = previous.get(vibe)
                    if (old and old['prediction_id'] is not None and old['source'] == source
                            and old['score'] == fit['score'] and old['support'] == fit['support']
                            and old['inputs_version'] == track['version'] and old['artist_key'] == track['artist']
                            and old['subgenre_key'] == track['genre']):
                        saved = {row[0] for row in self.conn.execute('SELECT measurement_id FROM prediction_inputs WHERE prediction_id=?',
                                                                   (old['prediction_id'],))}
                        if saved == inputs:
                            pid = old['prediction_id']
                    if pid is None:
                        pid = self.conn.execute('INSERT INTO predictions(recording_id,vibe,score,support,subgenre_key,kind,policy_version,predicted_at) '
                            'VALUES (?,?,?,?,?,?,?,?)', (rid, vibe, fit['score'], fit['support'],
                            track['genre'] if source != 'artist' else None, source, track['version'], now)).lastrowid
                        self.conn.executemany('INSERT INTO prediction_artists VALUES (?,?)',
                            [(pid, aid) for aid in json.loads(track['artist'] or '[]')] if source != 'genre' else [])
                        self.conn.executemany('INSERT INTO prediction_inputs VALUES (?,?)', [(pid, mid) for mid in sorted(inputs)])
                    mid = None
                values = (rid, vibe, fit['score'], fit['support'], source, mid, pid, track['version'], track['artist'], track['genre'],
                          fit.get('recordings'), fit.get('variance'))
                if vibe not in previous or tuple(previous[vibe]) != values:
                    self.conn.execute('INSERT INTO recording_vibes VALUES (?,?,?,?,?,?,?,?,?,?,?,?) '
                        'ON CONFLICT(recording_id,vibe) DO UPDATE SET score=excluded.score,support=excluded.support,'
                        'source=excluded.source,measurement_id=excluded.measurement_id,prediction_id=excluded.prediction_id,'
                        'inputs_version=excluded.inputs_version,artist_key=excluded.artist_key,subgenre_key=excluded.subgenre_key,'
                        'recordings=excluded.recordings,variance=excluded.variance', values)
                    changed = True
        self._queue(touched)
        if changed:
            self._store.rec_refresh.publish_notification()

    @synchronized
    def overlay(self, profiles):
        """Serve relational results without another persistent score copy in JSON."""
        if not profiles:
            return profiles
        keys = list(profiles)
        with keyed_reads(self._store, 'recordings', keys):
            ids = {r[0]: r[1] for r in self.conn.execute('SELECT id,identity_key FROM recordings '
                'WHERE identity_key IN (' + ','.join('?' for _ in keys) + ')', keys)}
        with keyed_reads(self._store, 'recording_vibes', ids):
            rows = self.conn.execute('SELECT recording_id,vibe,score,support,source,inputs_version,recordings,variance '
                'FROM recording_vibes WHERE recording_id IN (' + ','.join('?' for _ in ids) + ')', list(ids))
            for row in rows:
                profile = profiles[ids[row['recording_id']]]
                vibes = profile.setdefault('vibes', {'version': PREDICTION_VERSION,
                    'inputs_version': row['inputs_version'], 'fits': {}})
                fit = {name: row[name] for name in ('score', 'support', 'source')}
                if row['recordings'] is not None:
                    fit.update({name: row[name] for name in ('recordings', 'variance')})
                vibes['fits'][row['vibe']] = fit
        return profiles

    @synchronized
    def ranked(self, vibe, limit=100):
        return [dict(row) for row in self.conn.execute('SELECT recording_id,score,support,source FROM recording_vibes '
            'WHERE vibe=? ORDER BY score DESC,recording_id LIMIT ?', (vibe, limit))]
