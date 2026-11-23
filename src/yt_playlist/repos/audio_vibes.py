"""Durable research samples and internal model-development annotations.

Feedback rows, including legacy labels, are for development collection/inspection
and model training/evaluation only. Recommendation ranking and artist/genre
prediction must not read them as track overrides, preferences or propagation
evidence. Audio measurements and trained-model outputs are separate inputs.
"""
from yt_playlist.core import audio_annotations, sample_identity
from yt_playlist.repos.base import Repo, synchronized


class AudioVibesRepo(Repo):
    @synchronized
    def base_metadata_inputs(self, video_id):
        """Exact catalog inputs for the model, independent of development labels."""
        from yt_playlist.core.audio_vibe_metadata import read_facts
        return read_facts(self.conn, video_id)

    @synchronized
    def playback(self, run_id):
        row = self.conn.execute("SELECT video_id,title,artist FROM audio_annotations WHERE run_key=? AND kind='playback'",
                                (run_id,)).fetchone()
        if row:
            return dict(row)
        return None

    @synchronized
    def save_playback(self, run_id, video_id, title, artist, now):
        with self.conn.atomic(join=True):
            self.conn.execute('UPDATE audio_annotations SET video_id=video_id WHERE 0')
            previous = self.playback(run_id)
            if previous and previous['video_id'] != video_id:
                raise ValueError('The playback run belongs to a different song')
            if previous is None:
                self.conn.execute('INSERT INTO audio_annotations(run_key,kind,video_id,title,artist,created_at) '
                                  "VALUES (?,'playback',?,?,?,?)", (run_id, video_id, title, artist, now))

    @synchronized
    def sample_receipts(self, run_id):
        """Acknowledge published identities without loading their evidence payloads."""
        return [{key: row[key] for key in ('id', 'point')} for row in self.sample_facts(run_id)]

    @synchronized
    def sample_facts(self, run_id):
        return [dict(row) for row in self.conn.execute(
            "SELECT * FROM sample_attributes WHERE run_id=? AND file_status='ready' ORDER BY captured_at,id", (run_id,))]

    @synchronized
    def sample_conflicts(self, run_id, sample_id):
        return [dict(row) for row in self.conn.execute('SELECT * FROM sample_attributes WHERE run_id=? OR id=? ORDER BY id',
                                                       (run_id, sample_id))]

    @synchronized
    def reserve_sample(self, sample, reference, now, *, expected):
        with self.conn.atomic():
            self.conn.execute('UPDATE sample_attributes SET filename=filename WHERE 0')
            if self.sample_conflicts(sample['run_id'], sample['id']) != expected:
                raise ValueError('This sampling point has already been saved')
            self.save_playback(sample['run_id'], sample['video_id'], sample['title'], sample['artist'], now)
            # New captured evidence supplies its actual playback identity.
            # Historical missing runs remain unresolved during migration.
            rid = sample_identity.recording(self.conn, sample, now)
            self.conn.execute('INSERT INTO sample_attributes('
                'id,recording_id,video_id,run_id,point,filename,captured_at,content_digest,file_status) '
                "VALUES (?,?,?,?,?,?,?,?,'pending')",
                (sample['id'], rid, sample['video_id'], sample['run_id'], sample['point'],
                 reference['filename'], now, reference['content_digest']))

    @synchronized
    def complete_sample(self, sample_id, reference):
        with self.conn.atomic():
            retained = self.conn.execute('SELECT * FROM sample_attributes WHERE id=?', (sample_id,)).fetchone()
            row = dict(retained) if retained else None
            if (row is None or row['filename'] != reference['filename']
                    or row['content_digest'] != reference.get('content_digest')):
                raise ValueError('The reserved sample changed before completion')
            if row['file_status'] == 'pending':
                self.conn.execute("UPDATE sample_attributes SET file_status='ready' WHERE id=?", (sample_id,))

    @synchronized
    def annotation(self, run_id, *, kind, taxonomy_version):
        return audio_annotations.latest(self.conn, run_id, kind=kind, taxonomy_version=taxonomy_version)

    @synchronized
    def latest_feedback_run(self, video_id, *, exclude_run_id, taxonomy_version):
        row = self.conn.execute('SELECT a.run_key FROM audio_annotations a JOIN ('
            "SELECT run_key AS run_id FROM audio_annotations WHERE kind='playback' AND video_id=? UNION "
            'SELECT run_id FROM sample_attributes WHERE video_id=?) runs '
            'ON runs.run_id=a.run_key '
            "WHERE a.kind='session' AND a.taxonomy_version=? AND a.run_key!=? AND " + audio_annotations.HEAD +
            ' ORDER BY a.created_at DESC,a.run_key DESC LIMIT 1',
            (video_id, video_id, taxonomy_version, exclude_run_id)).fetchone()
        return row[0] if row else None

    @synchronized
    def annotation_heads(self, run_id):
        rows = self.conn.execute('SELECT a.* FROM audio_annotations a WHERE a.run_key=? AND ' +
            audio_annotations.HEAD + ' ORDER BY a.kind,a.created_at,a.taxonomy_version', (run_id,))
        return [audio_annotations.decode(rows, row) for row in rows]

    @synchronized
    def save_annotation(self, run_id, sample_ids, vibes, taxonomy_version, prediction, now):
        with self.conn.atomic():
            return audio_annotations.save(self.conn, run_id, sample_ids, vibes, taxonomy_version, prediction, now)
