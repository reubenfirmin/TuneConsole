"""Durable research samples and internal model-development annotations.

Feedback rows, including legacy labels, are for development collection/inspection
and model training/evaluation only. Recommendation ranking and artist/genre
prediction must not read them as track overrides, preferences or propagation
evidence. Audio measurements and trained-model outputs are separate inputs.
"""
import json
import hashlib
from pathlib import Path

from yt_playlist.core.audio_vibe_contract import TAXONOMY_VERSION
from yt_playlist.core import sample_files
from yt_playlist.repos.base import Repo, synchronized


class AudioVibesRepo(Repo):
    @synchronized
    def base_metadata(self, video_id):
        """Exact catalog inputs for the model, independent of development labels."""
        from yt_playlist.core.audio_vibe_metadata import read
        return read(self.conn, video_id)

    @synchronized
    def playback(self, run_id):
        row = self.conn.execute('SELECT video_id,title,artist FROM audio_vibe_runs WHERE run_id=?',
                                (run_id,)).fetchone()
        if row:
            return dict(row)
        samples = self.samples(run_id)
        return {key: samples[0][key] for key in ('video_id', 'title', 'artist')} if samples else None

    @synchronized
    def save_playback(self, run_id, video_id, title, artist, now):
        previous = self.playback(run_id)
        if previous and previous['video_id'] != video_id:
            raise ValueError('The playback run belongs to a different song')
        with self.conn:
            self.conn.execute('INSERT OR IGNORE INTO audio_vibe_runs VALUES (?,?,?,?,?)',
                              (run_id, video_id, title, artist, now))

    @synchronized
    def samples(self, run_id):
        result = []
        for row in self.conn.execute('SELECT id,run_id,point,video_id,filename FROM sample_attributes '
                                      'WHERE run_id=? ORDER BY captured_at,id', (run_id,)):
            try:
                result.append(sample_files.read(row['filename'], expected={name: row[name]
                              for name in ('id', 'run_id', 'point', 'video_id')}))
            except (OSError, ValueError):
                continue  # Incomplete evidence remains usable by collection/inspection.
        return result

    @synchronized
    def save_sample(self, sample, now):
        playback = self.playback(sample['run_id'])
        if playback and playback['video_id'] != sample['video_id']:
            raise ValueError('The playback run belongs to a different song')
        existing = self.conn.execute('SELECT * FROM sample_attributes WHERE run_id=? OR id=?',
                                      (sample['run_id'], sample['id'])).fetchall()
        digest = hashlib.sha256(sample_files.canonical(sample)).hexdigest()
        for previous in existing:
            if previous['run_id'] == sample['run_id'] and previous['video_id'] != sample['video_id']:
                raise ValueError('The playback run belongs to a different song')
            if previous['point'] == sample['point'] or previous['id'] == sample['id']:
                if (any(previous[name] != sample[name] for name in ('id', 'run_id', 'point', 'video_id'))
                        or Path(previous['filename']).stem != digest):
                    raise ValueError('This sampling point has already been saved')
                # Also repairs a missing file on an identical collection retry.
                missing = not Path(previous['filename']).is_file()
                sample_files.publish(Path(previous['filename']).parent.parent, sample)
                if missing:
                    with self.conn:
                        self.conn.execute('INSERT INTO rec_content_pending(identity_key) '
                            'SELECT identity_key FROM recordings WHERE id=? '
                            'ON CONFLICT(identity_key) DO UPDATE SET revision=revision+1',
                            (previous['recording_id'],))
                return
        filename = sample_files.publish(sample_files.directory(self.conn, self._store._evidence_key), sample)
        with self.conn:
            rid = sample_files.recording(self.conn, sample, now)
            self.conn.execute('INSERT INTO sample_attributes VALUES (?,?,?,?,?,?,?)',
                (sample['id'], rid, sample['video_id'], sample['run_id'], sample['point'], filename, now))

    @synchronized
    def feedback(self, run_id):
        """Read a development annotation for the labeling UI or model evaluation."""
        row = self.conn.execute('SELECT * FROM audio_session_labels WHERE run_id=? AND taxonomy_version=?',
                                (run_id, TAXONOMY_VERSION)).fetchone()
        return ({'taxonomy_version': row['taxonomy_version'], 'vibes': json.loads(row['vibes']),
                 'sample_ids': json.loads(row['sample_ids']), 'prediction': json.loads(row['prediction']),
                 'updated_at': row['updated_at']} if row else None)

    @synchronized
    def latest_feedback(self, video_id, *, exclude_run_id):
        """Restore this exact video's previous answer in the development form."""
        row = self.conn.execute('SELECT labels.run_id FROM audio_session_labels labels JOIN ('
            'SELECT run_id FROM audio_vibe_runs WHERE video_id=? UNION '
            'SELECT run_id FROM sample_attributes WHERE video_id=?) runs USING(run_id) '
            'WHERE labels.taxonomy_version=? AND labels.run_id!=? '
            'ORDER BY labels.updated_at DESC,labels.run_id DESC LIMIT 1',
            (video_id, video_id, TAXONOMY_VERSION, exclude_run_id)).fetchone()
        return {'run_id': row['run_id'], **self.feedback(row['run_id'])} if row else None

    @synchronized
    def legacy_feedback(self, run_id):
        row = self.conn.execute('SELECT * FROM audio_vibe_feedback WHERE run_id=?', (run_id,)).fetchone()
        return ({'vibe': row['vibe'], 'sample_ids': json.loads(row['sample_ids']),
                 'prediction': json.loads(row['prediction']), 'updated_at': row['updated_at']} if row else None)

    @synchronized
    def earlier_feedback(self, run_id):
        """Expose historical judgments without translating their target meanings."""
        rows = self.conn.execute('SELECT * FROM audio_vibe_labels WHERE run_id=?', (run_id,)).fetchall()
        rows += self.conn.execute('SELECT * FROM audio_session_labels WHERE run_id=? AND taxonomy_version!=?',
                                  (run_id, TAXONOMY_VERSION)).fetchall()
        result = [{'taxonomy_version': row['taxonomy_version'], 'vibes': json.loads(row['vibes']),
                   'sample_ids': json.loads(row['sample_ids']), 'prediction': json.loads(row['prediction']),
                   'updated_at': row['updated_at']} for row in rows]
        legacy = self.legacy_feedback(run_id)
        if legacy:
            result.append(legacy)
        return result

    @synchronized
    def save_feedback(self, run_id, sample_ids, vibes, taxonomy_version, prediction, now):
        """Persist a development annotation; do not publish product ranking evidence."""
        with self.conn:
            self.conn.execute('INSERT INTO audio_session_labels VALUES (?,?,?,?,?,?) '
                'ON CONFLICT(run_id,taxonomy_version) DO UPDATE SET sample_ids=excluded.sample_ids,'
                'prediction=excluded.prediction,vibes=excluded.vibes,'
                'updated_at=excluded.updated_at',
                (run_id, taxonomy_version, json.dumps(sample_ids), json.dumps(prediction), json.dumps(vibes), now))
