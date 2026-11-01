"""Human judgments retain revisions, sample provenance and frozen hypotheses."""
import json
import math
from pathlib import Path

from yt_playlist.core import audio_annotation_files
from .runner import MigrationError


# Frozen vocabulary, independent of later application label names. Older
# undocumented vocabularies retain their observed labels as legacy evidence;
# they are never silently mapped onto a current listening-session target.
VOCABULARIES = {
    'audio-feedback-presets-1': ('gentle', 'boost', 'focus', 'bright', 'unwind', 'move'),
    'audio-session-targets-1': ('relax', 'concentrate', 'cruise', 'release', 'energize', 'uplift'),
}


def statements():
    yield ("CREATE TABLE audio_taxonomies (taxonomy_version TEXT PRIMARY KEY NOT NULL,"
           "status TEXT NOT NULL CHECK(status IN ('supported','legacy_observed')))")
    yield ("CREATE TABLE audio_taxonomy_labels (taxonomy_version TEXT NOT NULL "
           "REFERENCES audio_taxonomies(taxonomy_version),vibe TEXT NOT NULL CHECK(length(vibe)>0),"
           "PRIMARY KEY(taxonomy_version,vibe)) WITHOUT ROWID")
    yield ("CREATE TABLE audio_annotations (id INTEGER PRIMARY KEY AUTOINCREMENT,"
           "run_id TEXT REFERENCES audio_vibe_runs(run_id),kind TEXT NOT NULL "
           "CHECK(kind IN ('feedback','development','session')),taxonomy_version TEXT NOT NULL "
           "REFERENCES audio_taxonomies(taxonomy_version),revision INTEGER NOT NULL "
           "CHECK(typeof(revision)='integer' AND revision>0),state TEXT NOT NULL "
           "CHECK(state IN ('labeled','empty','withdrawn')),created_at REAL NOT NULL,"
           "supersedes_id INTEGER UNIQUE REFERENCES audio_annotations(id),legacy_run_key TEXT,"
           "sealed INTEGER NOT NULL DEFAULT 0 CHECK(sealed IN (0,1)),"
           "resolution_status TEXT NOT NULL CHECK(resolution_status IN ('resolved','legacy_missing_run')),"
           "unresolved_sample_count INTEGER NOT NULL DEFAULT 0 CHECK(typeof(unresolved_sample_count)='integer' "
           "AND unresolved_sample_count>=0),model_output_filename TEXT NOT NULL,"
           "model_output_digest TEXT NOT NULL CHECK(length(model_output_digest)=64 "
           "AND model_output_digest NOT GLOB '*[^0-9a-f]*'),"
           "file_format TEXT NOT NULL CHECK(file_format='audio-annotation-1'),"
           "CHECK(substr(model_output_filename,-69)=model_output_digest||'.json'),"
           "UNIQUE(id,taxonomy_version),CHECK((run_id IS NOT NULL AND legacy_run_key IS NULL "
           "AND resolution_status='resolved') OR (run_id IS NULL AND legacy_run_key IS NOT NULL "
           "AND resolution_status='legacy_missing_run')))")
    yield ('CREATE UNIQUE INDEX ix_annotation_revision ON audio_annotations('
           'COALESCE(run_id,legacy_run_key),kind,taxonomy_version,revision)')
    yield ('CREATE INDEX ix_annotation_latest ON audio_annotations('
           'COALESCE(run_id,legacy_run_key),kind,taxonomy_version,revision DESC)')
    yield ("CREATE TABLE audio_annotation_samples (annotation_id INTEGER NOT NULL "
           "REFERENCES audio_annotations(id),sample_id TEXT NOT NULL REFERENCES sample_attributes(id),"
           "position INTEGER NOT NULL CHECK(typeof(position)='integer' AND position>=0),"
           "PRIMARY KEY(annotation_id,sample_id),UNIQUE(annotation_id,position)) WITHOUT ROWID")
    yield 'CREATE INDEX ix_annotation_sample ON audio_annotation_samples(sample_id,annotation_id)'
    yield ("CREATE TABLE audio_annotation_vibes (annotation_id INTEGER NOT NULL,"
           "taxonomy_version TEXT NOT NULL,vibe TEXT NOT NULL,position INTEGER NOT NULL "
           "CHECK(typeof(position)='integer' AND position>=0),PRIMARY KEY(annotation_id,vibe),"
           "UNIQUE(annotation_id,position),FOREIGN KEY(annotation_id,taxonomy_version) "
           "REFERENCES audio_annotations(id,taxonomy_version),FOREIGN KEY(taxonomy_version,vibe) "
           "REFERENCES audio_taxonomy_labels(taxonomy_version,vibe)) WITHOUT ROWID")
    yield ('CREATE INDEX ix_annotation_vibe ON audio_annotation_vibes('
           'taxonomy_version,vibe,annotation_id)')
    # Evaluation consumes these facts. The captured explanation/arc remains in
    # its immutable file and never becomes a product prediction or measurement.
    yield ("CREATE TABLE audio_annotation_evaluations (annotation_id INTEGER PRIMARY KEY "
           "REFERENCES audio_annotations(id),taxonomy_version TEXT NOT NULL "
           "REFERENCES audio_taxonomies(taxonomy_version),prediction_version TEXT,"
           "model_present INTEGER NOT NULL CHECK(model_present IN (0,1)),"
           "model_labels_present INTEGER NOT NULL CHECK(model_labels_present IN (0,1)),model_sha256 TEXT,"
           "model_feature_version TEXT,model_training_songs INTEGER CHECK(model_training_songs IS NULL "
           "OR (typeof(model_training_songs)='integer' AND model_training_songs>=0)),"
           "UNIQUE(annotation_id,taxonomy_version))")
    yield ("CREATE TABLE audio_annotation_scores (annotation_id INTEGER NOT NULL,"
           "taxonomy_version TEXT NOT NULL,vibe TEXT NOT NULL,position INTEGER NOT NULL "
           "CHECK(typeof(position)='integer' AND position>=0),score REAL NOT NULL CHECK(score BETWEEN 0 AND 1),"
           "PRIMARY KEY(annotation_id,vibe),UNIQUE(annotation_id,position),"
           "FOREIGN KEY(annotation_id,taxonomy_version) REFERENCES "
           "audio_annotation_evaluations(annotation_id,taxonomy_version),"
           "FOREIGN KEY(taxonomy_version,vibe) REFERENCES audio_taxonomy_labels(taxonomy_version,vibe)) WITHOUT ROWID")
    yield ("CREATE TABLE audio_annotation_predictions (annotation_id INTEGER NOT NULL,"
           "vibe TEXT NOT NULL,position INTEGER NOT NULL CHECK(typeof(position)='integer' AND position>=0),"
           "PRIMARY KEY(annotation_id,vibe),UNIQUE(annotation_id,position),"
           "FOREIGN KEY(annotation_id,vibe) REFERENCES audio_annotation_scores(annotation_id,vibe)) WITHOUT ROWID")
    yield ("CREATE TABLE audio_annotation_model_labels (annotation_id INTEGER NOT NULL,"
           "taxonomy_version TEXT NOT NULL,vibe TEXT NOT NULL,position INTEGER NOT NULL "
           "CHECK(typeof(position)='integer' AND position>=0),PRIMARY KEY(annotation_id,vibe),"
           "UNIQUE(annotation_id,position),FOREIGN KEY(annotation_id,taxonomy_version) "
           "REFERENCES audio_annotation_evaluations(annotation_id,taxonomy_version),"
           "FOREIGN KEY(taxonomy_version,vibe) REFERENCES audio_taxonomy_labels(taxonomy_version,vibe)) WITHOUT ROWID")
    yield ("CREATE TRIGGER audio_annotation_revision BEFORE INSERT ON audio_annotations "
           "WHEN NEW.sealed<>0 OR (NEW.supersedes_id IS NULL AND NEW.revision<>1) OR (NEW.supersedes_id IS NOT NULL "
           "AND NOT EXISTS(SELECT 1 FROM audio_annotations a WHERE a.id=NEW.supersedes_id "
           "AND COALESCE(a.run_id,a.legacy_run_key)=COALESCE(NEW.run_id,NEW.legacy_run_key) "
           "AND a.kind=NEW.kind AND a.taxonomy_version=NEW.taxonomy_version AND a.sealed=1 "
           "AND NEW.revision=a.revision+1)) "
           "BEGIN SELECT RAISE(ABORT,'Annotation revision does not follow its own judgment'); END")
    yield ("CREATE TRIGGER audio_annotation_immutable BEFORE UPDATE ON audio_annotations "
           "WHEN NEW.id IS NOT OLD.id OR NEW.kind IS NOT OLD.kind "
           "OR NEW.taxonomy_version IS NOT OLD.taxonomy_version OR NEW.revision IS NOT OLD.revision "
           "OR NEW.state IS NOT OLD.state OR NEW.created_at IS NOT OLD.created_at "
           "OR NEW.supersedes_id IS NOT OLD.supersedes_id OR NEW.unresolved_sample_count IS NOT OLD.unresolved_sample_count "
           "OR (NEW.sealed IS NOT OLD.sealed AND NOT (OLD.sealed=0 AND NEW.sealed=1)) "
           "OR NEW.model_output_digest IS NOT OLD.model_output_digest OR NEW.file_format IS NOT OLD.file_format "
           "OR ((NEW.run_id IS NOT OLD.run_id OR NEW.legacy_run_key IS NOT OLD.legacy_run_key "
           "OR NEW.resolution_status IS NOT OLD.resolution_status) AND NOT (OLD.run_id IS NULL "
           "AND NEW.run_id=OLD.legacy_run_key AND NEW.legacy_run_key IS NULL AND NEW.resolution_status='resolved')) "
           "BEGIN SELECT RAISE(ABORT,'Supersede an annotation instead of rewriting its judgment'); END")
    yield ("CREATE TRIGGER audio_annotation_seal BEFORE UPDATE OF sealed ON audio_annotations "
           "WHEN OLD.sealed=0 AND NEW.sealed=1 AND ((NEW.state='labeled' AND NOT EXISTS("
           "SELECT 1 FROM audio_annotation_vibes WHERE annotation_id=NEW.id)) OR "
           "(NEW.state<>'labeled' AND EXISTS(SELECT 1 FROM audio_annotation_vibes WHERE annotation_id=NEW.id))) "
           "BEGIN SELECT RAISE(ABORT,'Annotation labels do not match its final judgment state'); END")
    yield ("CREATE TRIGGER audio_annotation_retained BEFORE DELETE ON audio_annotations "
           "BEGIN SELECT RAISE(ABORT,'Annotation history must be retained'); END")
    for operation in ('INSERT', 'UPDATE'):
        yield (f'CREATE TRIGGER audio_annotation_samples_{operation.lower()} BEFORE {operation} '
               'ON audio_annotation_samples WHEN NOT EXISTS(SELECT 1 FROM audio_annotations a '
               'JOIN sample_attributes s ON s.run_id=COALESCE(a.run_id,a.legacy_run_key) '
               'WHERE a.id=NEW.annotation_id AND s.id=NEW.sample_id) '
               "BEGIN SELECT RAISE(ABORT,'Annotation sample belongs to another run'); END")
        yield (f'CREATE TRIGGER audio_annotation_vibes_{operation.lower()} BEFORE {operation} '
               "ON audio_annotation_vibes WHEN NOT EXISTS(SELECT 1 FROM audio_annotations a "
               "WHERE a.id=NEW.annotation_id AND a.state='labeled' AND (a.kind<>'feedback' "
               "OR NOT EXISTS(SELECT 1 FROM audio_annotation_vibes v WHERE v.annotation_id=a.id "
               "AND v.vibe<>NEW.vibe))) BEGIN SELECT RAISE(ABORT,'Invalid annotation label state'); END")
    yield ("CREATE TRIGGER annotation_sample_run BEFORE UPDATE OF run_id,video_id ON sample_attributes "
           "WHEN (NEW.run_id IS NOT OLD.run_id OR NEW.video_id IS NOT OLD.video_id) "
           "AND EXISTS(SELECT 1 FROM audio_annotation_samples "
           "WHERE sample_id=OLD.id) BEGIN SELECT RAISE(ABORT,'Retained annotation protects its sample run'); END")
    yield ("CREATE TRIGGER annotation_run_identity BEFORE UPDATE OF run_id,video_id ON audio_vibe_runs "
           "WHEN (NEW.run_id IS NOT OLD.run_id OR NEW.video_id IS NOT OLD.video_id) "
           "AND EXISTS(SELECT 1 FROM audio_annotations WHERE run_id=OLD.run_id) "
           "BEGIN SELECT RAISE(ABORT,'Retained annotation protects its playback run'); END")
    for table in ('audio_annotation_samples', 'audio_annotation_vibes', 'audio_annotation_evaluations',
                  'audio_annotation_scores', 'audio_annotation_predictions', 'audio_annotation_model_labels'):
        for operation in ('INSERT', 'UPDATE', 'DELETE'):
            sides = ('OLD', 'NEW') if operation == 'UPDATE' else (('OLD',) if operation == 'DELETE' else ('NEW',))
            condition = ' OR '.join('EXISTS(SELECT 1 FROM audio_annotations WHERE id='
                                    f'{side}.annotation_id AND sealed=1)' for side in sides)
            yield (f'CREATE TRIGGER {table}_sealed_{operation.lower()} BEFORE {operation} ON {table} '
                   f'WHEN {condition} BEGIN SELECT RAISE(ABORT,\'Supersede a sealed annotation\'); END')


def seed(conn):
    for version, labels in VOCABULARIES.items():
        conn.execute("INSERT INTO audio_taxonomies VALUES (?,'supported')", (version,))
        conn.executemany('INSERT INTO audio_taxonomy_labels VALUES (?,?)', ((version, label) for label in labels))


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


def apply(conn, context):
    rows = []
    for table, kind in (('audio_vibe_feedback', 'feedback'), ('audio_vibe_labels', 'development'),
                        ('audio_session_labels', 'session')):
        fields = [row[1] for row in conn.execute(f'PRAGMA table_info({table})')]
        for values in conn.execute(f'SELECT * FROM {table} ORDER BY updated_at,run_id'):
            row = dict(zip(fields, values))
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
            rows.append((table, kind, version, row, samples, labels, prediction))
    for sql in statements():
        conn.execute(sql)
    seed(conn)
    root = audio_annotation_files.directory(conn)
    for table, kind, version, row, samples, labels, prediction in rows:
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
    for table in ('audio_vibe_feedback', 'audio_vibe_labels', 'audio_session_labels'):
        conn.execute(f'DROP TABLE {table}')


def validate(conn):
    from .current import _tokens
    for sql in statements():
        words = sql.split()
        name = words[3] if words[1] == 'UNIQUE' else words[2]
        row = conn.execute('SELECT sql FROM sqlite_master WHERE name=?', (name,)).fetchone()
        if row is None or _tokens(row[0]) != _tokens(sql):
            raise MigrationError(f'Incompatible audio annotation structure: {name}')
    if conn.execute("SELECT 1 FROM sqlite_master WHERE name IN "
                    "('audio_vibe_feedback','audio_vibe_labels','audio_session_labels')").fetchone():
        raise MigrationError('Superseded audio annotation stores remain')
