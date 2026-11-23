"""Put each annotation and its archived model output in the same row."""
import hashlib
import json
import logging
from pathlib import Path

from .. import fao
from ..schema_contract import MigrationError, tokens
from ..schema_inspection import definitions
from .account_storage import _preserve_sequence, _sequence
from .runner import PreparationChanged


NAME = 'compact_audio_annotation_storage'
TABLE = 'audio_annotations'
RETIRED = (
    'annotation_file_writes', 'audio_annotation_samples', 'audio_annotation_vibes', 'audio_annotation_predictions',
    'audio_annotation_scores', 'audio_annotation_model_labels', 'audio_annotation_evaluations',
    'audio_taxonomy_labels', 'audio_taxonomies',
)
REMOVED_GUARDS = ('annotation_sample_run', 'annotation_run_identity', 'audio_annotation_revision',
                  'audio_annotation_seal', 'ix_annotation_latest', 'ix_annotation_revision')
SQL = {
    TABLE: '''CREATE TABLE audio_annotations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        run_key TEXT NOT NULL, kind TEXT NOT NULL CHECK(kind IN ('feedback','development','session')),
        taxonomy_version TEXT NOT NULL, revision INTEGER NOT NULL CHECK(revision>0),
        state TEXT NOT NULL CHECK(state IN ('labeled','empty','withdrawn')), created_at REAL NOT NULL,
        supersedes_id INTEGER UNIQUE REFERENCES audio_annotations(id),
        sample_1_id TEXT REFERENCES sample_attributes(id),
        sample_2_id TEXT REFERENCES sample_attributes(id),
        sample_3_id TEXT REFERENCES sample_attributes(id),
        labels_json TEXT NOT NULL CHECK(json_valid(labels_json) AND json_type(labels_json)='array'),
        archive_json TEXT NOT NULL CHECK(json_valid(archive_json) AND json_type(archive_json)='object'),
        unresolved_sample_count INTEGER NOT NULL DEFAULT 0 CHECK(unresolved_sample_count>=0),
        UNIQUE(run_key,kind,taxonomy_version,revision),
        CHECK((state='labeled')=(json_array_length(labels_json)>0)),
        CHECK(kind<>'feedback' OR json_array_length(labels_json)<=1))''',
    'audio_annotation_immutable': '''CREATE TRIGGER audio_annotation_immutable BEFORE UPDATE ON audio_annotations
        BEGIN SELECT RAISE(ABORT,'Supersede an annotation instead of rewriting its judgment'); END''',
    'audio_annotation_retained': '''CREATE TRIGGER audio_annotation_retained BEFORE DELETE ON audio_annotations
        BEGIN SELECT RAISE(ABORT,'Annotation history must be retained'); END''',
}
_FILE_LIMIT = 4 * 1024 * 1024
_DOCUMENT_REFERENCES = (
    'SELECT id,COALESCE(run_id,legacy_run_key),kind,taxonomy_version,revision,'
    'model_output_filename,model_output_digest FROM audio_annotations WHERE sealed=1 ORDER BY id')


def _document(filename, digest, *, run_key, kind, taxonomy_version, revision):
    raw = fao.read(Path(filename), max_bytes=_FILE_LIMIT).bytes
    if hashlib.sha256(raw).hexdigest() != digest:
        raise ValueError('Annotation digest mismatch')
    document = json.loads(raw)
    expected = dict(format='audio-annotation-1', run_key=run_key, kind=kind,
                    taxonomy_version=taxonomy_version, revision=revision)
    if not isinstance(document, dict) or any(document.get(key) != value for key, value in expected.items()):
        raise ValueError('Annotation identity mismatch')
    return {key: value for key, value in document.items() if key not in expected}


def prepare(conn, version):
    # Release the read statement before opening the files. Playback can keep
    # saving to the predecessor while the migration reads archived predictions.
    with conn:
        conn.execute('BEGIN')
        if conn.execute('SELECT 1 FROM schema_changes WHERE version=?', (version,)).fetchone():
            return
        references = [tuple(row) for row in conn.execute(_DOCUMENT_REFERENCES).fetchall()]
    archives = {}
    for identity, run, kind, taxonomy, revision, filename, digest in references:
        try:
            document = _document(filename, digest, run_key=run, kind=kind, taxonomy_version=taxonomy, revision=revision)
            archives[identity] = json.dumps(document, allow_nan=False)
        except (OSError, ValueError, fao.FileChanged):
            continue
    return references, archives


def _retained_evaluation(conn, identity):
    row = conn.execute(
        'SELECT taxonomy_version,prediction_version,model_present,model_labels_present,model_sha256,'
        'model_feature_version,model_training_songs FROM audio_annotation_evaluations WHERE annotation_id=?',
        (identity,)).fetchone()
    if row is None:
        return None
    taxonomy, version, present, has_labels, digest, feature, songs = row
    model = ({key: value for key, value in [('sha256', digest), ('feature_version', feature), ('training_songs', songs)]
              if value is not None} if present else None)
    if present and has_labels:
        model['trained_labels'] = [row[0] for row in conn.execute(
            'SELECT vibe FROM audio_annotation_model_labels WHERE annotation_id=? ORDER BY position', (identity,))]
    return {'taxonomy_version': taxonomy, 'version': version, 'model': model,
            'ranking': [{'vibe': vibe, 'score': score} for vibe, score in conn.execute(
                'SELECT vibe,score FROM audio_annotation_scores WHERE annotation_id=? ORDER BY position', (identity,))],
            'vibes': [row[0] for row in conn.execute(
                'SELECT vibe FROM audio_annotation_predictions WHERE annotation_id=? ORDER BY position', (identity,))]}


def install(conn, context):
    references, archives = context.prepared
    if [tuple(row) for row in conn.execute(_DOCUMENT_REFERENCES)] != references:
        raise PreparationChanged('The annotation archive changed before installation')
    previous_sequence = _sequence(conn, TABLE)
    for name in ('annotation_sample_run', 'annotation_run_identity'):
        conn.execute('DROP TRIGGER ' + name)
    conn.execute('ALTER TABLE audio_annotations RENAME TO previous_audio_annotations')
    conn.execute(SQL[TABLE])
    unavailable = 0
    for identity, run, kind, taxonomy, revision, state, created, supersedes, unresolved in conn.execute(
            'SELECT id,COALESCE(run_id,legacy_run_key),kind,taxonomy_version,revision,state,created_at,'
            'supersedes_id,unresolved_sample_count '
            'FROM previous_audio_annotations WHERE sealed=1 ORDER BY id'):
        samples = [row[0] for row in conn.execute(
            'SELECT sample_id FROM audio_annotation_samples WHERE annotation_id=? ORDER BY position', (identity,))]
        if len(samples) > 3:
            raise MigrationError(f'Annotation {identity} has more than three captured samples')
        labels = [row[0] for row in conn.execute(
            'SELECT vibe FROM audio_annotation_vibes WHERE annotation_id=? ORDER BY position', (identity,))]
        archive = archives.get(identity)
        if archive is None:
            unavailable += 1
            archive = json.dumps({'prediction': _retained_evaluation(conn, identity), 'unresolved_samples': []})
        conn.execute('INSERT INTO audio_annotations(id,run_key,kind,taxonomy_version,revision,state,created_at,'
                     'supersedes_id,sample_1_id,sample_2_id,sample_3_id,labels_json,archive_json,unresolved_sample_count) '
                     'VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                     (identity, run, kind, taxonomy, revision, state, created, supersedes,
                      *samples, *([None] * (3 - len(samples))), json.dumps(labels), archive, unresolved))
    for name in RETIRED:
        conn.execute('DROP TABLE ' + name)
    conn.execute('DROP TABLE previous_audio_annotations')
    _preserve_sequence(conn, TABLE, previous_sequence)
    for name, sql in SQL.items():
        if name != TABLE:
            conn.execute(sql)
    if unavailable:
        logging.getLogger(__name__).warning(
            'Some archived annotation documents were unavailable (%d). Imported the retained SQL evidence.',
            unavailable)


def validate(conn):
    from . import audio_playback_storage

    actual = definitions(conn, (*SQL, *RETIRED, *REMOVED_GUARDS, audio_playback_storage.INDEX))
    if any(name in actual for name in (*RETIRED, *REMOVED_GUARDS)):
        raise MigrationError('Retired audio annotation storage remains')
    expected = (dict(SQL, audio_annotations=audio_playback_storage.TABLE_SQL)
                if audio_playback_storage.INDEX in actual else SQL)
    if any(tokens(actual.get(name)) != tokens(sql) for name, sql in expected.items()):
        raise MigrationError('Incompatible audio annotation archive')
