"""Bounded SQL facts and immutable identities for shared vibe models."""
from yt_playlist.core import vibe_versions
from yt_playlist.core.activity import check_stopping
from yt_playlist.core.vibe_model_files import HEAD_PREFIX, REVISION


def album_tables(conn):
    names = {row[0] for row in conn.execute("SELECT name FROM sqlite_schema WHERE type='table' AND name IN "
        "('album_tracks','album_track_occurrences','album_item_observations','album_occurrence_sources')")}
    current = {'album_track_occurrences','album_item_observations','album_occurrence_sources'}
    if current <= names:
        return sorted(current)
    if names == {'album_tracks'}:
        return ['album_tracks']
    raise ValueError('Unsupported album conditioning owners')


def schema(conn):
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
    yield from conditioning_guards(conn)


def conditioning_guards(conn):
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
    owners = album_tables(conn)
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
    for _, sql in schema(conn):
        conn.execute(sql)


def install_conditioning_guards(conn):
    """Follow album-owner replacements without replacing model identities."""
    for name, sql in conditioning_guards(conn):
        conn.execute('DROP TRIGGER IF EXISTS ' + name)
        conn.execute(sql)


def metadata_guards(conn):
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
    for _name, sql in metadata_guards(conn):
        conn.execute(sql)


def validate_metadata_guards(conn):
    from yt_playlist.core.schema_contract import tokens

    for name, sql in metadata_guards(conn):
        row = conn.execute('SELECT sql FROM sqlite_schema WHERE name=?', (name,)).fetchone()
        if row is None or tokens(row[0]) != tokens(sql):
            raise ValueError('Incompatible shared vibe metadata guard: ' + name)


def validate(conn):
    from yt_playlist.core.schema_contract import tokens
    for name, sql in schema(conn):
        saved = conn.execute('SELECT sql FROM sqlite_schema WHERE name=?', (name,)).fetchone()
        if saved is None or tokens(saved[0]) != tokens(sql):
            raise ValueError('Incompatible shared vibe model reference: ' + name)


def available(conn):
    return bool(conn.execute("SELECT 1 FROM sqlite_schema WHERE name='prediction_model_generations'").fetchone())


def album_facts(conn, recordings):
    """Album identity and distinct-owner count in bounded indexed batches."""
    identities, result = list(recordings), {}
    legacy = 'album_tracks' in album_tables(conn)
    for start in range(0, len(identities), 128):
        check_stopping()
        batch = identities[start:start+128]
        marks = ','.join('?' for _ in batch)
        if legacy:
            sql = ('SELECT recording_id,album_id FROM album_tracks WHERE recording_id IN (' + marks + ') '
                'UNION ALL SELECT e.recording_id,t.album_id FROM album_tracks t JOIN catalog_entries e ON e.id=t.entry_id '
                'WHERE e.recording_id IN (' + marks + ')')
            args = batch * 2
        else:
            sql = ('SELECT o.recording_id,t.album_id FROM album_track_occurrences o '
                'JOIN album_tracklists t ON t.id=o.tracklist_id JOIN albums a ON a.current_tracklist_id=t.id '
                'WHERE o.recording_id IN (' + marks + ') '
                'UNION ALL SELECT recording_id,album_id FROM album_item_observations WHERE recording_id IN (' + marks + ') '
                'UNION ALL SELECT e.recording_id,i.album_id FROM catalog_entries e '
                'JOIN album_item_observations i ON i.entry_id=e.id WHERE e.recording_id IN (' + marks + ')')
            args = batch * 3
        result.update((row[0], (row[1], row[2])) for row in conn.execute(
            'SELECT recording_id,min(album_id),count(DISTINCT album_id) FROM (' + sql + ') GROUP BY recording_id', args))
    return result


def training_rows(conn, vibe, limit):
    """Return at most limit plus one original rows so callers detect overflow."""
    return [dict(row) for row in conn.execute('SELECT v.recording_id,v.measurement_id,v.artist_key,v.subgenre_key,'
        'm.score,m.support,m.source,m.input_digest,' + vibe_versions.expression(conn, 'measurements', 'm') +
        ' AS exact_version FROM recording_vibes v JOIN measurements m ON m.id=v.measurement_id '
        'WHERE v.vibe=? AND m.support>0 ORDER BY v.recording_id LIMIT ?', (vibe, limit + 1))]


def linked(conn, prediction):
    if not available(conn):
        return None
    row = conn.execute('SELECT generation FROM prediction_model_generations WHERE prediction_id=?',
                       (prediction,)).fetchone()
    return row[0].hex() if row else None


def subject(conn, prediction):
    return conn.execute('SELECT vibe,recording_id FROM predictions WHERE id=?', (prediction,)).fetchone()


def receipt_marker(conn, prediction):
    row = conn.execute('SELECT input_fingerprint,state FROM prediction_provenance WHERE prediction_id=?',
                       (prediction,)).fetchone()
    return (row[0], row[1], linked(conn, prediction)) if row else None


def root_measurements(conn, identities):
    conn.executemany('INSERT OR IGNORE INTO prediction_measurement_roots VALUES (?)', [(mid,) for mid in identities])


def referenced_generations(conn):
    if available(conn):
        seen = set()
        # Expired history no longer requires its model. The SQL identity remains
        # until bounded prediction-history retirement removes that calculation.
        for generation, in conn.execute("SELECT DISTINCT g.generation FROM prediction_model_generations g "
                "JOIN prediction_provenance p ON p.prediction_id=g.prediction_id WHERE p.state='ready'"):
            seen.add(generation.hex())
            yield generation.hex()
        for generation, in conn.execute("SELECT value FROM settings WHERE key GLOB ?", (HEAD_PREFIX + '*',)):
            if generation and generation not in seen:
                seen.add(generation)
                yield generation


def referenced(conn, generation):
    return (conn.execute('SELECT 1 FROM settings WHERE key GLOB ? AND value=? LIMIT 1',
                         (HEAD_PREFIX + '*', generation)).fetchone()
            or conn.execute("SELECT 1 FROM prediction_model_generations g JOIN prediction_provenance p "
                            "ON p.prediction_id=g.prediction_id WHERE g.generation=? AND p.state='ready' LIMIT 1",
                            (bytes.fromhex(generation),)).fetchone())


def measurement_rows(conn, identities):
    if not 0 < len(identities) <= 128:
        raise ValueError('Shared model measurements require a bounded batch')
    return [dict(row) for row in conn.execute(
        'SELECT m.id,m.recording_id,m.vibe,m.score,m.support,m.source,m.input_digest,'
        + vibe_versions.expression(conn, 'measurements', 'm') + ' AS exact_version FROM measurements m '
        'WHERE m.id IN (' + ','.join('?' for _ in identities) + ')', identities)]


class VibeModelQueries:
    """Connection-only queries shared by runtime repositories and offline rebuilds."""

    def __init__(self, conn):
        self.conn = conn

    def assert_idle(self):
        if self.conn.in_transaction:
            raise RuntimeError('Shared model collection requires committed SQL')

    def measurements(self, identities):
        return measurement_rows(self.conn, identities)

    def receipt_facts(self, prediction, artists):
        return {'prediction': tuple(row) if (row := subject(self.conn, prediction)) else None,
                'generation': linked(self.conn, prediction), 'marker': receipt_marker(self.conn, prediction),
                'artists': {artist for artist in artists if type(artist) is int and self.conn.execute(
                    'SELECT 1 FROM artists WHERE id=?', (artist,)).fetchone()}}

    def install_receipts(self, generations, roots, markers):
        # Explicit rebuild callers already own their SQL batch. Runtime model
        # publication uses the prediction repository's ordered publication.
        if not self.conn.in_transaction:
            raise RuntimeError('Receipt rebuild requires its SQL batch')
        root_measurements(self.conn, roots)
        self.conn.executemany('INSERT OR IGNORE INTO prediction_model_generations VALUES (?,?)', generations)
        self.conn.executemany("INSERT INTO prediction_provenance VALUES (?,?,?,'ready') "
                             'ON CONFLICT(prediction_id) DO NOTHING', markers)

    def referenced(self, generation):
        return bool(referenced(self.conn, generation))
