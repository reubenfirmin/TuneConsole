"""Preserve released judgments and their original sample/model provenance."""
import json
import math
from pathlib import Path

from .. import audio_annotation_files
from ..schema_contract import MigrationError


VOCABULARIES = {
    'audio-feedback-presets-1': ('gentle', 'boost', 'focus', 'bright', 'unwind', 'move'),
    'audio-session-targets-1': ('relax', 'concentrate', 'cruise', 'release', 'energize', 'uplift'),
}


def _list(encoded, *, source, run, field):
    try:
        values = json.loads(encoded)
        if not isinstance(values, list) or any(not isinstance(value, str) for value in values):
            raise ValueError('expected a string list')
        return values
    except (TypeError, ValueError) as exc:
        raise MigrationError(f'Invalid legacy annotation {source}/{run}/{field}; original rows retained') from exc


def _evaluation(conn, annotation_id, prediction):
    if not isinstance(prediction, dict):
        return
    version = prediction.get('taxonomy_version')
    if version != 'audio-session-targets-1':
        return
    try:
        ranking, predicted = prediction['ranking'], prediction['vibes']
        scores = {row['vibe']: row['score'] for row in ranking}
        if (len(ranking) != len(VOCABULARIES[version]) or set(scores) != set(VOCABULARIES[version])
                or not all(isinstance(score, (float, int)) and not isinstance(score, bool)
                           and math.isfinite(score) and 0 <= score <= 1 for score in scores.values())
                or not isinstance(predicted, list) or not predicted or len(set(predicted)) != len(predicted)
                or not set(predicted) <= set(scores)):
            return
        captured_model = prediction.get('model')
        model = {} if captured_model is None else captured_model
        if not isinstance(model, dict):
            return
        trained = model.get('trained_labels', [])
        if (not isinstance(trained, list) or len(set(trained)) != len(trained)
                or not set(trained) <= set(scores)):
            return
        training = model.get('training_songs')
        if training is not None and (type(training) is not int or training < 0):
            return
        text = [prediction.get('version'), model.get('sha256'), model.get('feature_version')]
        if any(value is not None and not isinstance(value, str) for value in text):
            return
    except (KeyError, TypeError):
        return
    conn.execute('INSERT INTO audio_annotation_evaluations VALUES (?,?,?,?,?,?,?,?)',
                 (annotation_id, version, text[0], int(prediction.get('model') is not None),
                  int('trained_labels' in model), *text[1:], training))
    conn.executemany('INSERT INTO audio_annotation_scores VALUES (?,?,?,?,?)',
                     ((annotation_id, version, row['vibe'], position, row['score'])
                      for position, row in enumerate(ranking)))
    conn.executemany('INSERT INTO audio_annotation_predictions VALUES (?,?,?)',
                     ((annotation_id, label, position) for position, label in enumerate(predicted)))
    conn.executemany('INSERT INTO audio_annotation_model_labels VALUES (?,?,?,?)',
                     ((annotation_id, version, label, position) for position, label in enumerate(trained)))


def prepare_row(table, kind, row):
    run = row['run_id']
    version = row.get('taxonomy_version', 'audio-feedback-presets-1')
    samples = _list(row['sample_ids'], source=table, run=run, field='sample_ids')
    labels = ([row['vibe']] if row['vibe'] else []) if kind == 'feedback' else _list(
        row['vibes'], source=table, run=run, field='vibes')
    try:
        prediction = json.loads(row['prediction'])
    except (TypeError, ValueError) as exc:
        raise MigrationError(f'Invalid prediction in {table}/{run}; original rows retained') from exc
    if (not run or not isinstance(version, str) or not version or any(not label for label in labels)
            or not isinstance(row['updated_at'], (float, int)) or not math.isfinite(row['updated_at'])):
        raise MigrationError(f'Invalid annotation identity in {table}/{run}; original rows retained')
    if version in VOCABULARIES and not set(labels) <= set(VOCABULARIES[version]):
        raise MigrationError(f'Unrecognized label in {table}/{run}/{version}; original rows retained')
    return table, kind, version, row, samples, labels, prediction


def copy_row(conn, prepared):
    table, kind, version, row, samples, labels, prediction = prepared
    root = audio_annotation_files.directory(conn)
    run = row['run_id']
    resolved = bool(conn.execute('SELECT 1 FROM audio_vibe_runs WHERE run_id=?', (run,)).fetchone())
    missing, members, seen = [], [], set()
    for position, sample_id in enumerate(samples):
        owner = conn.execute('SELECT run_id FROM sample_attributes WHERE id=?', (sample_id,)).fetchone()
        if owner is None or owner[0] != run:
            missing.append({'position': position, 'sample_id': sample_id,
                            'reason': 'missing_sample' if owner is None else 'wrong_run'})
        elif sample_id not in seen:
            members.append((sample_id, position))
            seen.add(sample_id)
    if version not in VOCABULARIES:
        conn.execute("INSERT OR IGNORE INTO audio_taxonomies VALUES (?,'legacy_observed')", (version,))
        conn.executemany('INSERT OR IGNORE INTO audio_taxonomy_labels VALUES (?,?)',
                         ((version, label) for label in labels))
    filename = audio_annotation_files.publish(root, {
        'format': 'audio-annotation-1', 'run_key': run, 'kind': kind, 'taxonomy_version': version,
        'revision': 1, 'prediction': prediction, 'legacy_source': table,
        'legacy': row, 'unresolved_samples': missing})
    state = 'labeled' if labels else ('withdrawn' if kind == 'feedback' else 'empty')
    annotation_id = conn.execute('INSERT INTO audio_annotations('
        'run_id,kind,taxonomy_version,revision,state,created_at,legacy_run_key,resolution_status,'
        'unresolved_sample_count,model_output_filename,model_output_digest,file_format) '
        'VALUES (?,?,?,1,?,?,?,?,?,?,?,?)',
        (run if resolved else None, kind, version, state, row['updated_at'], None if resolved else run,
         'resolved' if resolved else 'legacy_missing_run', len(missing), filename, Path(filename).stem,
         'audio-annotation-1')).lastrowid
    conn.executemany('INSERT INTO audio_annotation_samples VALUES (?,?,?)',
                     ((annotation_id, sample_id, position) for sample_id, position in members))
    conn.executemany('INSERT INTO audio_annotation_vibes VALUES (?,?,?,?)',
                     ((annotation_id, version, label, position)
                      for position, label in enumerate(dict.fromkeys(labels))))
    _evaluation(conn, annotation_id, prediction)
    conn.execute('UPDATE audio_annotations SET sealed=1 WHERE id=?', (annotation_id,))
