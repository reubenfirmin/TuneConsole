"""Durable research samples and internal model-development annotations.

Feedback rows, including legacy labels, are for development collection/inspection
and model training/evaluation only. Recommendation ranking and artist/genre
prediction must not read them as track overrides, preferences or propagation
evidence. Audio measurements and trained-model outputs are separate inputs.
"""
from yt_playlist.core import audio_annotations, sample_identity
from yt_playlist.core.audio_annotation_contract import FILE_FORMAT
from yt_playlist.repos.base import Repo, synchronized


def install_sample_files(conn, context=None):
    """Add capture status without rewriting installed sample rows or files."""
    columns = {row[1] for row in conn.execute('PRAGMA main.table_info(sample_attributes)')}
    for name, declaration in (('content_digest', 'TEXT'), ('file_status', "TEXT NOT NULL DEFAULT 'ready'")):
        if name not in columns:
            conn.execute(f'ALTER TABLE sample_attributes ADD COLUMN {name} {declaration}')


def validate_sample_files(conn):
    from yt_playlist.core.schema_contract import MigrationError
    columns = {row[1]: (row[2], row[3], row[4])
               for row in conn.execute('PRAGMA main.table_info(sample_attributes)')}
    if (columns.get('content_digest') != ('TEXT', 0, None)
            or columns.get('file_status') != ('TEXT', 1, "'ready'")):
        raise MigrationError('Incompatible sample file status')


class AudioVibesRepo(Repo):
    @synchronized
    def base_metadata_inputs(self, video_id):
        """Exact catalog inputs for the model, independent of development labels."""
        from yt_playlist.core.audio_vibe_metadata import read_facts
        return read_facts(self.conn, video_id)

    @synchronized
    def playback(self, run_id):
        row = self.conn.execute('SELECT video_id,title,artist FROM audio_vibe_runs WHERE run_id=?',
                                (run_id,)).fetchone()
        if row:
            return dict(row)
        return None

    @synchronized
    def save_playback(self, run_id, video_id, title, artist, now):
        with self.conn.atomic(join=True):
            self.conn.execute('UPDATE audio_vibe_runs SET video_id=video_id WHERE 0')
            previous = self.playback(run_id)
            if previous and previous['video_id'] != video_id:
                raise ValueError('The playback run belongs to a different song')
            self.conn.execute('INSERT OR IGNORE INTO audio_vibe_runs VALUES (?,?,?,?,?)',
                              (run_id, video_id, title, artist, now))

    @synchronized
    def sample_receipts(self, run_id):
        """Acknowledge published identities without loading their evidence payloads."""
        return [{key: row[key] for key in ('id', 'point')} for row in self.sample_facts(run_id)]

    @synchronized
    def sample_facts(self, run_id):
        # At most three sampling points belong to a run. SELECT * also reads
        # retained layouts before their bounded status-column adoption.
        rows = [dict(row) for row in self.conn.execute(
            'SELECT * FROM sample_attributes WHERE run_id=? ORDER BY captured_at,id', (run_id,))]
        return [row for row in rows if row.get('file_status', 'ready') == 'ready']

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
                    or row.get('content_digest') != reference.get('content_digest')):
                raise ValueError('The reserved sample changed before completion')
            if row.get('file_status') == 'pending':
                self.conn.execute("UPDATE sample_attributes SET file_status='ready' WHERE id=?", (sample_id,))

    @synchronized
    def annotation(self, run_id, *, kind, taxonomy_version):
        row = audio_annotations.latest(self.conn, run_id, kind=kind, taxonomy_version=taxonomy_version)
        return self._annotation_facts(row) if row else None

    def _annotation_facts(self, row):
        return {**row, 'sample_ids': [item[0] for item in self.conn.execute(
            'SELECT sample_id FROM audio_annotation_samples WHERE annotation_id=? ORDER BY position', (row['id'],))],
            'vibes': [item[0] for item in self.conn.execute(
            'SELECT vibe FROM audio_annotation_vibes WHERE annotation_id=? ORDER BY position', (row['id'],))]}

    @synchronized
    def latest_feedback_run(self, video_id, *, exclude_run_id, taxonomy_version):
        row = self.conn.execute('SELECT COALESCE(a.run_id,a.legacy_run_key) run_id FROM audio_annotations a JOIN ('
            'SELECT run_id FROM audio_vibe_runs WHERE video_id=? UNION '
            'SELECT run_id FROM sample_attributes WHERE video_id=?) runs '
            'ON runs.run_id=COALESCE(a.run_id,a.legacy_run_key) '
            "WHERE a.kind='session' AND a.sealed=1 AND a.taxonomy_version=? "
            'AND COALESCE(a.run_id,a.legacy_run_key)!=? AND NOT EXISTS('
            'SELECT 1 FROM audio_annotations successor WHERE successor.supersedes_id=a.id AND successor.sealed=1) '
            'ORDER BY a.created_at DESC,COALESCE(a.run_id,a.legacy_run_key) DESC LIMIT 1',
            (video_id, video_id, taxonomy_version, exclude_run_id)).fetchone()
        return row['run_id'] if row else None

    @synchronized
    def annotation_heads(self, run_id):
        rows = self.conn.execute('SELECT a.* FROM audio_annotations a '
            'WHERE COALESCE(a.run_id,a.legacy_run_key)=? AND a.sealed=1 '
            'AND NOT EXISTS(SELECT 1 FROM audio_annotations successor '
            'WHERE successor.supersedes_id=a.id AND successor.sealed=1) '
            'ORDER BY a.kind,a.created_at,a.taxonomy_version', (run_id,)).fetchall()
        return [self._annotation_facts(dict(row)) for row in rows]

    @synchronized
    def taxonomy(self, version):
        known = self.conn.execute('SELECT 1 FROM audio_taxonomies WHERE taxonomy_version=?', (version,)).fetchone()
        return bool(known), {row[0] for row in self.conn.execute(
            'SELECT vibe FROM audio_taxonomy_labels WHERE taxonomy_version=?', (version,))}

    @synchronized
    def sample_owners(self, samples):
        owners = {}
        for sample in samples:
            row = self.conn.execute('SELECT * FROM sample_attributes WHERE id=?', (sample,)).fetchone()
            if row:
                row = dict(row)
                if row.get('file_status', 'ready') == 'ready':
                    owners[sample] = row['run_id']
        return owners

    @synchronized
    def reserve_annotation_file(self, run_id, taxonomy_version, revision, integrity):
        from yt_playlist.repos import annotation_file_writes
        with self.conn.atomic():
            return annotation_file_writes.reserve(self.conn, run_id, taxonomy_version, revision, integrity)

    @synchronized
    def publish_annotation(self, run_id, sample_ids, vibes, taxonomy_version, now, *, state, revision,
                           file_id, reference, evaluation, expected):
        from yt_playlist.repos import annotation_file_writes
        with self.conn.atomic():
            self.conn.execute('UPDATE audio_annotations SET sealed=sealed WHERE 0')
            audio_annotations.resolve_run(self.conn, run_id)
            previous = audio_annotations.latest(self.conn, run_id, kind='session',
                                                taxonomy_version=taxonomy_version)
            if (previous['id'] if previous else None) != expected:
                raise ValueError('The annotation changed; reload its current judgment')
            annotation_id = self.conn.execute('INSERT INTO audio_annotations('
                'run_id,kind,taxonomy_version,revision,state,created_at,supersedes_id,resolution_status,'
                'model_output_filename,model_output_digest,file_format) VALUES (?,\'session\',?,?,?,?,?,\'resolved\',?,?,?)',
                (run_id, taxonomy_version, revision, state, now, expected, *reference[:2], FILE_FORMAT)).lastrowid
            self.conn.executemany('INSERT INTO audio_annotation_samples VALUES (?,?,?)',
                ((annotation_id, sample, position) for position, sample in enumerate(sample_ids)))
            self.conn.executemany('INSERT INTO audio_annotation_vibes VALUES (?,?,?,?)',
                ((annotation_id, taxonomy_version, vibe, position) for position, vibe in enumerate(vibes)))
            audio_annotations.write_evaluation(self.conn, annotation_id, evaluation)
            self.conn.execute('UPDATE audio_annotations SET sealed=1 WHERE id=?', (annotation_id,))
            annotation_file_writes.complete(self.conn, file_id, run_id, taxonomy_version, revision,
                                           reference, annotation_id)
            return annotation_id

    @synchronized
    def resolve_annotations(self, run_id):
        """Repair legacy history after explicitly admitting its actual playback."""
        with self.conn.atomic(join=True):
            audio_annotations.resolve_run(self.conn, run_id)
