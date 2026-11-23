"""Schema changes for shared Vibe model generations and conditioning guards."""
from ..schema_contract import tokens
from ..vibe_model_files import REVISION


def _album_tables(conn):
    names = {row[0] for row in conn.execute("SELECT name FROM sqlite_schema WHERE type='table' AND name IN "
        "('album_tracks','album_track_occurrences','album_item_observations','album_occurrence_sources')")}
    current = {'album_track_occurrences','album_item_observations','album_occurrence_sources'}
    if current <= names:
        return sorted(current)
    if names == {'album_tracks'}:
        return ['album_tracks']
    raise ValueError('Unsupported album conditioning owners')


def _schema(conn):
    yield 'prediction_model_generations', (
        'CREATE TABLE prediction_model_generations ('
        'prediction_id INTEGER PRIMARY KEY REFERENCES predictions(id) ON DELETE CASCADE,'
        "generation BLOB NOT NULL CHECK(typeof(generation)='blob' AND length(generation)=32)) WITHOUT ROWID")
    yield 'prediction_model_generation', (
        'CREATE INDEX prediction_model_generation ON prediction_model_generations(generation,prediction_id)')
    yield 'prediction_model_immutable', (
        'CREATE TRIGGER prediction_model_immutable BEFORE UPDATE ON prediction_model_generations '
        "BEGIN SELECT RAISE(ABORT,'Prediction model identity is immutable'); END")
    yield 'prediction_model_delete', (
        'CREATE TRIGGER prediction_model_delete BEFORE DELETE ON prediction_model_generations '
        'WHEN EXISTS(SELECT 1 FROM predictions WHERE id=OLD.prediction_id) '
        "BEGIN SELECT RAISE(ABORT,'Prediction model identity requires its calculation'); END")
    yield from _conditioning_guards(conn)


def _conditioning_guards(conn):
    bump = ("INSERT INTO settings(key,value) VALUES ('" + REVISION + "','1') "
            "ON CONFLICT(key) DO UPDATE SET value=CAST(CAST(value AS INTEGER)+1 AS TEXT);")
    for operation, condition in (
            ('insert', 'NEW.measurement_id IS NOT NULL'),
            ('delete', 'OLD.measurement_id IS NOT NULL'),
            ('update', '(NEW.measurement_id IS NOT NULL OR OLD.measurement_id IS NOT NULL) AND '
             '(NEW.measurement_id IS NOT OLD.measurement_id OR NEW.artist_key IS NOT OLD.artist_key '
             'OR NEW.subgenre_key IS NOT OLD.subgenre_key)')):
        name = 'recording_vibes_joint_' + operation
        yield name, (f'CREATE TRIGGER {name} AFTER {operation.upper()} ON recording_vibes '
                     f'WHEN {condition} BEGIN {bump} END')
    owners = _album_tables(conn)
    for table in owners:
        for operation in ('insert', 'update', 'delete'):
            name = table + '_joint_' + operation
            yield name, f'CREATE TRIGGER {name} AFTER {operation.upper()} ON {table} BEGIN {bump} END'
    if 'album_track_occurrences' in owners:
        yield 'albums_joint_current', ('CREATE TRIGGER albums_joint_current AFTER UPDATE OF current_tracklist_id '
            f'ON albums WHEN NEW.current_tracklist_id IS NOT OLD.current_tracklist_id BEGIN {bump} END')
    yield 'catalog_entries_joint_recording', ('CREATE TRIGGER catalog_entries_joint_recording AFTER UPDATE OF recording_id '
        f'ON catalog_entries WHEN NEW.recording_id IS NOT OLD.recording_id BEGIN {bump} END')


def install(conn, context=None):
    for _, sql in _schema(conn):
        conn.execute(sql)


def install_conditioning_guards(conn):
    """Follow album-owner replacements without replacing model identities."""
    for name, sql in _conditioning_guards(conn):
        conn.execute('DROP TRIGGER IF EXISTS ' + name)
        conn.execute(sql)


def _metadata_guards():
    """Invalidate masked conditioning without depending on derived predictions.

    The accepted raw inputs and source profile/link owners precede vibe scoring.
    Listening, annotations and publication of a derived target cannot feed back
    into this revision. These guards do not convert or scan the library.
    """
    bump = ("INSERT INTO settings(key,value) VALUES ('" + REVISION + "','1') "
            "ON CONFLICT(key) DO UPDATE SET value=CAST(CAST(value AS INTEGER)+1 AS TEXT);")
    for table in ('rec_musical_source_profiles', 'rec_musical_links', 'prepared_recording_inputs'):
        for operation in ('insert', 'update', 'delete'):
            condition = ''
            if table == 'prepared_recording_inputs':
                reference = 'OLD' if operation == 'delete' else 'NEW'
                condition = ('EXISTS(SELECT 1 FROM recording_vibes WHERE recording_id='
                             + reference + '.recording_id AND measurement_id IS NOT NULL)')
                if operation == 'update':
                    condition += ' AND NEW.digest IS NOT OLD.digest'
            elif table == 'rec_musical_source_profiles' and operation == 'update':
                condition = 'NEW.digest IS NOT OLD.digest OR NEW.version IS NOT OLD.version'
            name = table + '_joint_metadata_' + operation
            yield name, (f'CREATE TRIGGER {name} AFTER {operation.upper()} ON {table} '
                         + ('WHEN ' + condition + ' ' if condition else '') + 'BEGIN ' + bump + ' END')


def install_metadata_guards(conn, context=None):
    for _, sql in _metadata_guards():
        conn.execute(sql)


def validate_metadata_guards(conn):
    for name, sql in _metadata_guards():
        row = conn.execute('SELECT sql FROM sqlite_schema WHERE name=?', (name,)).fetchone()
        if row is None or tokens(row[0]) != tokens(sql):
            raise ValueError('Incompatible shared vibe metadata guard: ' + name)


def validate(conn):
    for name, sql in _schema(conn):
        saved = conn.execute('SELECT sql FROM sqlite_schema WHERE name=?', (name,)).fetchone()
        if saved is None or tokens(saved[0]) != tokens(sql):
            raise ValueError('Incompatible shared vibe model reference: ' + name)
