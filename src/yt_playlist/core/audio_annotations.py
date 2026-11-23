"""Read and append human judgments and export their archived model output."""
import json

from .audio_annotation_contract import TAXONOMIES, evaluation_values, validate


SAMPLE_COLUMNS = ('sample_1_id', 'sample_2_id', 'sample_3_id')
HEAD = ("a.kind<>'playback' AND "
        'NOT EXISTS (SELECT 1 FROM audio_annotations successor WHERE successor.supersedes_id=a.id)')


def decode(cursor, row):
    annotation = dict(zip((field[0] for field in cursor.description), row))
    for field in ('video_id', 'title', 'artist'):
        annotation.pop(field)
    annotation['sample_ids'] = [sample for column in SAMPLE_COLUMNS
                                if (sample := annotation.pop(column)) is not None]
    annotation['vibes'] = json.loads(annotation.pop('labels_json'))
    annotation['archive'] = json.loads(annotation.pop('archive_json'))
    return annotation


def latest(conn, run_id, *, kind, taxonomy_version):
    cursor = conn.execute(
        "SELECT * FROM audio_annotations WHERE run_key=? AND kind=? AND taxonomy_version=? AND kind<>'playback' "
        'ORDER BY revision DESC LIMIT 1', (run_id, kind, taxonomy_version))
    row = cursor.fetchone()
    return decode(cursor, row) if row else None


def read(conn, annotation_id):
    cursor = conn.execute("SELECT * FROM audio_annotations WHERE id=? AND kind<>'playback'", (annotation_id,))
    row = cursor.fetchone()
    return decode(cursor, row) if row else None


def evaluation(prediction):
    """Interpret an archived hypothesis using the vocabulary it was recorded with."""
    if not isinstance(prediction, dict):
        return None
    taxonomy = prediction.get('taxonomy_version')
    if not isinstance(taxonomy, str):
        return None
    values = evaluation_values(prediction, TAXONOMIES.get(taxonomy, ()))
    if values is None:
        return None
    version, model_version, present, labels_present, digest, feature, training = values['header']
    model = ({key: value for key, value in (('sha256', digest), ('feature_version', feature),
                                            ('training_songs', training)) if value is not None}
             if present else None)
    if present and labels_present:
        model['trained_labels'] = values['trained']
    return {'taxonomy_version': version, 'version': model_version, 'model': model,
            'ranking': [{'vibe': vibe, 'score': score} for vibe, _, score in values['scores']],
            'vibes': values['predicted']}


def save(conn, run_id, sample_ids, labels, taxonomy_version, prediction, now, *, kind='session', state=None):
    """Append a judgment in the caller's write transaction."""
    samples, labels, state = validate(run_id, sample_ids, labels, now, kind=kind, state=state)
    if not conn.execute("SELECT 1 FROM audio_annotations WHERE run_key=? AND kind='playback'", (run_id,)).fetchone():
        raise ValueError('A new annotation requires an existing playback run')
    if taxonomy_version not in TAXONOMIES or not set(labels) <= set(TAXONOMIES[taxonomy_version]):
        raise ValueError('Labels do not belong to the annotation taxonomy')
    for sample in samples:
        if not conn.execute("SELECT 1 FROM sample_attributes WHERE id=? AND run_id=? AND file_status='ready'",
                            (sample, run_id)).fetchone():
            raise ValueError('Annotation sample is missing or belongs to another run')
    previous = latest(conn, run_id, kind=kind, taxonomy_version=taxonomy_version)
    if previous and not labels and previous['state'] in ('labeled', 'withdrawn'):
        state = 'withdrawn'
    archive = {'prediction': prediction, 'unresolved_samples': []}
    if (previous and previous['state'] == state and previous['sample_ids'] == samples
            and previous['vibes'] == labels and previous['archive'] == archive):
        return previous['id']
    return conn.execute(
        'INSERT INTO audio_annotations(run_key,kind,taxonomy_version,revision,state,created_at,'
        'supersedes_id,sample_1_id,sample_2_id,sample_3_id,labels_json,archive_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
        (run_id, kind, taxonomy_version, previous['revision'] + 1 if previous else 1, state, now,
         previous['id'] if previous else None, *samples, *([None] * (3 - len(samples))),
         json.dumps(labels), json.dumps(archive, allow_nan=False))).lastrowid


def training_rows(conn, taxonomy_version):
    """Latest session judgment per run, with the recorded model evaluation."""
    cursor = conn.execute(
        'SELECT a.id,a.run_key,a.created_at,a.labels_json,a.archive_json,COALESCE(r.video_id,'
        '(SELECT video_id FROM sample_attributes WHERE run_id=a.run_key ORDER BY captured_at DESC,id DESC LIMIT 1)) '
        "FROM audio_annotations a LEFT JOIN audio_annotations r ON r.run_key=a.run_key AND r.kind='playback' "
        "WHERE a.kind='session' AND a.taxonomy_version=? AND " + HEAD +
        ' ORDER BY a.created_at,a.run_key', (taxonomy_version,))
    rows = [{'id': identity, 'run_id': run, 'updated_at': created, 'video_id': video,
             'labels': json.loads(labels), 'evaluation': evaluation(json.loads(archive).get('prediction'))}
            for identity, run, created, labels, archive, video in cursor]
    earlier = conn.execute("SELECT COUNT(*) FROM audio_annotations a WHERE "
                           "(kind<>'session' OR taxonomy_version<>?) AND " + HEAD,
                           (taxonomy_version,)).fetchone()[0]
    return rows, earlier


def export_snapshot(conn):
    """Export all revisions and their model output directly from the archive."""
    rows = conn.execute("SELECT * FROM audio_annotations WHERE kind<>'playback' ORDER BY id")
    return {'format': 'audio-annotations-2', 'annotations': [decode(rows, row) for row in rows]}
