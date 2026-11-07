"""Revisioned human annotations. Callers own transactions and the Store lock."""
from pathlib import Path

from yt_playlist.core import audio_annotation_files as files
from yt_playlist.core.audio_annotation_contract import FILE_FORMAT, evaluation_values, validate


def latest(conn, run_id, *, kind, taxonomy_version):
    fields = ('id', 'run_id', 'kind', 'taxonomy_version', 'revision', 'state', 'created_at',
              'legacy_run_key', 'resolution_status', 'unresolved_sample_count', 'model_output_filename')
    row = conn.execute('SELECT ' + ','.join(fields) + ' FROM audio_annotations '
        'WHERE COALESCE(run_id,legacy_run_key)=? AND kind=? AND taxonomy_version=? AND sealed=1 '
        'ORDER BY revision DESC LIMIT 1', (run_id, kind, taxonomy_version)).fetchone()
    return dict(zip(fields, row)) if row else None


def read(conn, annotation):
    annotation_id = annotation['id']
    run = annotation['run_id'] or annotation['legacy_run_key']
    document = files.read(annotation['model_output_filename'], expected={
        'run_key': run, 'kind': annotation['kind'], 'taxonomy_version': annotation['taxonomy_version'],
        'revision': annotation['revision']})
    samples = [row[0] for row in conn.execute('SELECT sample_id FROM audio_annotation_samples '
                                             'WHERE annotation_id=? ORDER BY position', (annotation_id,))]
    labels = [row[0] for row in conn.execute('SELECT vibe FROM audio_annotation_vibes '
                                            'WHERE annotation_id=? ORDER BY position', (annotation_id,))]
    return {**annotation, 'sample_ids': samples, 'vibes': labels,
            'prediction': document['prediction'], 'updated_at': annotation['created_at'],
            'legacy_sample_ids': (document.get('legacy') or {}).get('sample_ids'),
            'unresolved_samples': document.get('unresolved_samples', [])}


def evaluation(conn, annotation_id):
    """Read the frozen, typed hypothesis; never rerun the current model."""
    row = conn.execute('SELECT taxonomy_version,prediction_version,model_present,model_labels_present,model_sha256,'
                        'model_feature_version,model_training_songs FROM audio_annotation_evaluations '
                        'WHERE annotation_id=?', (annotation_id,)).fetchone()
    if row is None:
        return None
    version, prediction_version, present, labels_present, digest, feature, training = row
    model = ({key: value for key, value in (('sha256', digest), ('feature_version', feature),
                                            ('training_songs', training)) if value is not None} if present else None)
    if present and labels_present:
        model['trained_labels'] = [row[0] for row in conn.execute('SELECT vibe FROM audio_annotation_model_labels '
            'WHERE annotation_id=? ORDER BY position', (annotation_id,))]
    return {'taxonomy_version': version, 'version': prediction_version, 'model': model,
            'ranking': [{'vibe': vibe, 'score': score} for vibe, score in conn.execute(
                'SELECT vibe,score FROM audio_annotation_scores WHERE annotation_id=? ORDER BY position',
                (annotation_id,))],
            'vibes': [row[0] for row in conn.execute('SELECT vibe FROM audio_annotation_predictions '
                'WHERE annotation_id=? ORDER BY position', (annotation_id,))]}


def _save_evaluation(conn, annotation_id, prediction):
    if not isinstance(prediction, dict):
        return
    version = prediction.get('taxonomy_version')
    vocabulary = {row[0] for row in conn.execute('SELECT vibe FROM audio_taxonomy_labels '
                                                 'WHERE taxonomy_version=?', (version,))}
    write_evaluation(conn, annotation_id, evaluation_values(prediction, vocabulary))


def write_evaluation(conn, annotation_id, values):
    if values is None:
        return
    version = values['header'][0]
    conn.execute('INSERT INTO audio_annotation_evaluations VALUES (?,?,?,?,?,?,?,?)',
                 (annotation_id, *values['header']))
    conn.executemany('INSERT INTO audio_annotation_scores VALUES (?,?,?,?,?)',
                     ((annotation_id, version, *row) for row in values['scores']))
    conn.executemany('INSERT INTO audio_annotation_predictions VALUES (?,?,?)',
                     ((annotation_id, label, position) for position, label in enumerate(values['predicted'])))
    conn.executemany('INSERT INTO audio_annotation_model_labels VALUES (?,?,?,?)',
                     ((annotation_id, version, label, position) for position, label in enumerate(values['trained'])))


def save(conn, run_id, sample_ids, labels, taxonomy_version, prediction, now, *, kind='session',
         state=None, memory_key=None):
    """Publish a new immutable revision, including an explicitly empty judgment."""
    samples, labels, state = validate(run_id, sample_ids, labels, now, kind=kind, state=state)
    if not conn.in_transaction:
        conn.execute('BEGIN IMMEDIATE')
    # An existing transaction may be a deferred reader. Reserve the writer
    # before publishing so concurrent orphan collection cannot remove the file
    # in the gap before its reference is inserted.
    conn.execute('UPDATE audio_annotations SET sealed=sealed WHERE 0')
    if not conn.execute('SELECT 1 FROM audio_vibe_runs WHERE run_id=?', (run_id,)).fetchone():
        raise ValueError('A new annotation requires an existing playback run')
    vocabulary = {row[0] for row in conn.execute('SELECT vibe FROM audio_taxonomy_labels '
                                                 'WHERE taxonomy_version=?', (taxonomy_version,))}
    if not conn.execute('SELECT 1 FROM audio_taxonomies WHERE taxonomy_version=?', (taxonomy_version,)).fetchone() or not set(labels) <= vocabulary:
        raise ValueError('Labels do not belong to the annotation taxonomy')
    for sample_id in samples:
        owner = conn.execute('SELECT run_id FROM sample_attributes WHERE id=?', (sample_id,)).fetchone()
        if owner is None or owner[0] != run_id:
            raise ValueError('Annotation sample is missing or belongs to another run')
    previous = latest(conn, run_id, kind=kind, taxonomy_version=taxonomy_version)
    revision = previous['revision'] + 1 if previous else 1
    filename = files.publish(files.directory(conn, memory_key), {
        'format': FILE_FORMAT, 'run_key': run_id, 'kind': kind, 'taxonomy_version': taxonomy_version,
        'revision': revision, 'prediction': prediction, 'unresolved_samples': []})
    annotation_id = conn.execute('INSERT INTO audio_annotations('
        'run_id,kind,taxonomy_version,revision,state,created_at,supersedes_id,resolution_status,'
        'model_output_filename,model_output_digest,file_format) VALUES (?,?,?,?,?,?,?,\'resolved\',?,?,?)',
        (run_id, kind, taxonomy_version, revision, state, now, previous['id'] if previous else None,
         filename, Path(filename).stem, FILE_FORMAT)).lastrowid
    conn.executemany('INSERT INTO audio_annotation_samples VALUES (?,?,?)',
                     ((annotation_id, sample_id, position) for position, sample_id in enumerate(samples)))
    conn.executemany('INSERT INTO audio_annotation_vibes VALUES (?,?,?,?)',
                     ((annotation_id, taxonomy_version, label, position) for position, label in enumerate(labels)))
    _save_evaluation(conn, annotation_id, prediction)
    conn.execute('UPDATE audio_annotations SET sealed=1 WHERE id=?', (annotation_id,))
    return annotation_id


def resolve_run(conn, run_id):
    """Attach preserved history only when that exact run is explicitly admitted."""
    if not conn.execute('SELECT 1 FROM audio_vibe_runs WHERE run_id=?', (run_id,)).fetchone():
        raise ValueError('Cannot resolve annotations without their actual playback run')
    conn.execute("UPDATE audio_annotations SET run_id=?,legacy_run_key=NULL,resolution_status='resolved' "
                 'WHERE run_id IS NULL AND legacy_run_key=?', (run_id, run_id))


def training_rows(conn, taxonomy_version):
    """Latest judgment per run, with typed labels and evaluation facts."""
    rows = []
    for annotation_id, run, created, video in conn.execute(
            'SELECT a.id,COALESCE(a.run_id,a.legacy_run_key),a.created_at,COALESCE(r.video_id,'
            '(SELECT video_id FROM sample_attributes WHERE run_id=COALESCE(a.run_id,a.legacy_run_key) '
            'ORDER BY captured_at DESC,id DESC LIMIT 1)) FROM audio_annotations a '
            'LEFT JOIN audio_vibe_runs r ON r.run_id=a.run_id '
            "WHERE a.kind='session' AND a.taxonomy_version=? AND a.sealed=1 "
            'AND NOT EXISTS(SELECT 1 FROM audio_annotations successor WHERE successor.supersedes_id=a.id AND successor.sealed=1) '
            'ORDER BY a.created_at,COALESCE(a.run_id,a.legacy_run_key)', (taxonomy_version,)):
        labels = [row[0] for row in conn.execute('SELECT vibe FROM audio_annotation_vibes '
                                                'WHERE annotation_id=? ORDER BY position', (annotation_id,))]
        rows.append({'id': annotation_id, 'run_id': run, 'updated_at': created, 'video_id': video,
                     'labels': labels, 'evaluation': evaluation(conn, annotation_id)})
    earlier = conn.execute("SELECT COUNT(*) FROM audio_annotations a WHERE sealed=1 "
        "AND (kind<>'session' OR taxonomy_version<>?) AND NOT EXISTS("
        'SELECT 1 FROM audio_annotations successor WHERE successor.supersedes_id=a.id AND successor.sealed=1)', (taxonomy_version,)).fetchone()[0]
    return rows, earlier


def export_snapshot(conn, root):
    """Freeze all judgment revisions and their files for offline development."""
    manifest = files.export(conn, root)
    tables = {}
    for table in ('audio_taxonomies', 'audio_taxonomy_labels', 'audio_annotations',
                  'audio_annotation_samples', 'audio_annotation_vibes', 'audio_annotation_evaluations',
                  'audio_annotation_scores', 'audio_annotation_predictions', 'audio_annotation_model_labels'):
        cursor = conn.execute(f'SELECT * FROM {table} ORDER BY 1,2')
        fields = [column[0] for column in cursor.description]
        tables[table] = [dict(zip(fields, row)) for row in cursor]
    return {'format': 'audio-annotation-relations-1', 'tables': tables, 'files_manifest': manifest}
