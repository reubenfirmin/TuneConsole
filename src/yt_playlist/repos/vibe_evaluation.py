"""Bounded SQL facts for isolated, original-evidence evaluation."""
import sqlite3

from yt_playlist.core import audio_annotations, catalog_keys
from yt_playlist.repos.base import Repo, synchronized
from yt_playlist.repos.vibe_models import album_facts, training_rows


class VibeEvaluationRepo(Repo):
    @synchronized
    def annotation_rows(self, limit):
        return [dict(row) for row in self.conn.execute(
            'SELECT id,run_id,legacy_run_key,kind,taxonomy_version,revision,state,'
            'created_at,supersedes_id,resolution_status,unresolved_sample_count FROM audio_annotations '
            'WHERE sealed=1 ORDER BY created_at,id LIMIT ?', (limit + 1,))]

    @synchronized
    def annotation_values(self, annotation_id, run):
        return {
            'videos': [row[0] for row in self.conn.execute(
                'SELECT video_id FROM audio_vibe_runs WHERE run_id=? '
                'UNION SELECT video_id FROM sample_attributes WHERE run_id=?', (run, run)) if row[0]],
            'labels': [row[0] for row in self.conn.execute('SELECT vibe FROM audio_annotation_vibes '
                'WHERE annotation_id=? ORDER BY position', (annotation_id,))],
            'sample_ids': [row[0] for row in self.conn.execute('SELECT sample_id FROM audio_annotation_samples '
                'WHERE annotation_id=? ORDER BY position', (annotation_id,))],
            'evaluation': audio_annotations.evaluation(self.conn, annotation_id),
        }

    @synchronized
    def video_recordings(self, videos):
        result = {}
        for video in videos:
            row = self.conn.execute('SELECT recording_id FROM youtube_items WHERE video_id=?', (video,)).fetchone()
            if row is not None and row[0] is not None:
                result[video] = catalog_keys.resolve_recording(self.conn, row[0])
        return result

    @synchronized
    def conditioning_rows(self, recordings, *, artist_limit):
        result = {}
        for recording in recordings:
            artists = [row[0] for row in self.conn.execute(
                'SELECT DISTINCT a.artist_id FROM catalog_entries e '
                f'JOIN {self.entry_artists_table} a ON a.entry_id=e.id WHERE e.recording_id=? '
                'ORDER BY a.artist_id LIMIT ?', (recording, artist_limit))]
            genre, = self.conn.execute('SELECT genre_key FROM recordings WHERE id=?', (recording,)).fetchone()
            result[recording] = {'artists': artists, 'genre': genre}
        return result

    @synchronized
    def album_facts(self, recordings):
        return album_facts(self.conn, recordings)

    @synchronized
    def recording_keys(self, recordings):
        return dict(self.conn.execute('SELECT identity_key,id FROM recordings WHERE id IN ('
            + ','.join('?' for _ in recordings) + ')', recordings))

    @synchronized
    def training_rows(self, vibe, limit):
        return training_rows(self.conn, vibe, limit)

    @synchronized
    def storage_statistics(self):
        try:
            sizes = [dict(row) for row in self.conn.execute('SELECT name,sum(pgsize) AS bytes '
                'FROM dbstat WHERE name IN (\'measurements\',\'recording_vibes\',\'predictions\',\'vibe_versions\','
                "'prediction_model_generations','prediction_model_generation') GROUP BY name")]
        except sqlite3.OperationalError:
            sizes = None
        return {'sql_owner_bytes': sizes,
                'page_size': self.conn.execute('PRAGMA page_size').fetchone()[0],
                'page_count': self.conn.execute('PRAGMA page_count').fetchone()[0],
                'sqlite_version': sqlite3.sqlite_version}
