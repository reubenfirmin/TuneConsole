"""Seven recording/vibe relations, with attribute JSON outside SQLite."""
import hashlib
import json

from yt_playlist.core import sample_files


def statements():
    yield ("CREATE TABLE sample_attributes (id TEXT PRIMARY KEY NOT NULL,"
           "recording_id INTEGER NOT NULL REFERENCES recordings(id),video_id TEXT NOT NULL REFERENCES youtube_items(video_id),"
           "run_id TEXT NOT NULL,point TEXT NOT NULL CHECK(point IN ('thirty','middle','two_thirds')),"
           "filename TEXT NOT NULL,captured_at REAL NOT NULL,UNIQUE(run_id,point),"
           "FOREIGN KEY(video_id,recording_id) REFERENCES youtube_items(video_id,recording_id) DEFERRABLE INITIALLY DEFERRED)")
    yield 'CREATE INDEX idx_sample_recording ON sample_attributes(recording_id,captured_at DESC,id)'
    yield 'CREATE INDEX idx_sample_video ON sample_attributes(video_id,captured_at DESC,id)'
    yield ("CREATE TABLE measurements (id INTEGER PRIMARY KEY,recording_id INTEGER NOT NULL REFERENCES recordings(id),"
           "vibe TEXT NOT NULL,score REAL NOT NULL CHECK(score BETWEEN 0 AND 1),"
           "support REAL NOT NULL CHECK(support BETWEEN 0 AND 1),"
           "source TEXT NOT NULL CHECK(source IN ('audio_measurement','provider_measurement')),"
           "model_version TEXT NOT NULL,input_digest TEXT NOT NULL,measured_at REAL,"
           "UNIQUE(recording_id,vibe,model_version,input_digest),UNIQUE(id,recording_id,vibe))")
    yield ("CREATE TABLE measurement_samples (measurement_id INTEGER NOT NULL REFERENCES measurements(id),"
           "sample_id TEXT NOT NULL REFERENCES sample_attributes(id) ON DELETE CASCADE,"
           "PRIMARY KEY(measurement_id,sample_id)) WITHOUT ROWID")
    yield 'CREATE INDEX idx_measurement_sample ON measurement_samples(sample_id,measurement_id)'
    yield ("CREATE TABLE predictions (id INTEGER PRIMARY KEY,recording_id INTEGER NOT NULL REFERENCES recordings(id),"
           "vibe TEXT NOT NULL,score REAL NOT NULL CHECK(score BETWEEN 0 AND 1),"
           "support REAL NOT NULL CHECK(support BETWEEN 0 AND 1),subgenre_key TEXT REFERENCES genre_catalog(key),"
           "kind TEXT NOT NULL CHECK(kind IN ('artist_genre','artist','genre')),"
           "lineage_complete INTEGER NOT NULL DEFAULT 1 CHECK(lineage_complete IN (0,1)),"
           "policy_version TEXT NOT NULL,predicted_at REAL,UNIQUE(id,recording_id,vibe),"
           "CHECK(lineage_complete=0 OR (kind IN ('artist_genre','genre') AND subgenre_key IS NOT NULL) "
           "OR (kind='artist' AND subgenre_key IS NULL)))")
    yield 'CREATE INDEX idx_prediction_recording ON predictions(recording_id,vibe,id)'
    yield ("CREATE TABLE prediction_artists (prediction_id INTEGER NOT NULL REFERENCES predictions(id) ON DELETE CASCADE,"
           "artist_id INTEGER NOT NULL REFERENCES artists(id),PRIMARY KEY(prediction_id,artist_id)) WITHOUT ROWID")
    yield 'CREATE INDEX idx_prediction_artist ON prediction_artists(artist_id,prediction_id)'
    yield ("CREATE TABLE prediction_inputs (prediction_id INTEGER NOT NULL REFERENCES predictions(id) ON DELETE CASCADE,"
           "measurement_id INTEGER NOT NULL REFERENCES measurements(id),"
           "PRIMARY KEY(prediction_id,measurement_id)) WITHOUT ROWID")
    yield 'CREATE INDEX idx_prediction_measurement ON prediction_inputs(measurement_id,prediction_id)'
    yield ("CREATE TABLE recording_vibes (recording_id INTEGER NOT NULL REFERENCES recordings(id),vibe TEXT NOT NULL,"
           "score REAL NOT NULL CHECK(score BETWEEN 0 AND 1),support REAL NOT NULL CHECK(support BETWEEN 0 AND 1),"
           "source TEXT NOT NULL CHECK(source IN ('audio_measurement','provider_measurement','artist_genre','artist','genre','heuristic')),"
           "measurement_id INTEGER,prediction_id INTEGER,inputs_version TEXT NOT NULL,"
           "artist_key TEXT,subgenre_key TEXT REFERENCES genre_catalog(key),recordings INTEGER,variance REAL,"
           "PRIMARY KEY(recording_id,vibe),"
           "FOREIGN KEY(measurement_id,recording_id,vibe) REFERENCES measurements(id,recording_id,vibe),"
           "FOREIGN KEY(prediction_id,recording_id,vibe) REFERENCES predictions(id,recording_id,vibe),"
           "CHECK((source IN ('audio_measurement','provider_measurement') AND measurement_id IS NOT NULL AND prediction_id IS NULL)"
           " OR (source IN ('artist_genre','artist','genre') AND prediction_id IS NOT NULL AND measurement_id IS NULL)"
           " OR (source='heuristic' AND measurement_id IS NULL AND prediction_id IS NULL))) WITHOUT ROWID")
    yield 'CREATE INDEX idx_recording_vibe_rank ON recording_vibes(vibe,score DESC,recording_id)'
    yield 'CREATE INDEX idx_vibe_artist_genre ON recording_vibes(artist_key,subgenre_key,recording_id,vibe)'
    yield 'CREATE INDEX idx_vibe_genre ON recording_vibes(subgenre_key,recording_id,vibe)'
    for operation in ('INSERT', 'UPDATE'):
        yield (f'CREATE TRIGGER measurement_samples_{operation.lower()} BEFORE {operation} ON measurement_samples '
               'WHEN NOT EXISTS (SELECT 1 FROM measurements m JOIN sample_attributes s '
               'ON s.recording_id=m.recording_id WHERE m.id=NEW.measurement_id AND s.id=NEW.sample_id) '
               "BEGIN SELECT RAISE(ABORT,'Measurement sample belongs to another recording'); END")
        yield (f'CREATE TRIGGER prediction_inputs_{operation.lower()} BEFORE {operation} ON prediction_inputs '
               'WHEN NOT EXISTS (SELECT 1 FROM predictions p JOIN measurements m ON m.vibe=p.vibe '
               'WHERE p.id=NEW.prediction_id AND m.id=NEW.measurement_id AND m.recording_id<>p.recording_id) '
               'OR EXISTS (SELECT 1 FROM measurements b CROSS JOIN measurements a '
               'CROSS JOIN prediction_inputs i WHERE b.id=NEW.measurement_id '
               'AND a.recording_id=b.recording_id AND a.vibe=b.vibe AND i.prediction_id=NEW.prediction_id '
               'AND i.measurement_id=a.id AND a.id<>b.id) '
               "BEGIN SELECT RAISE(ABORT,'Prediction inputs require distinct original recordings of the same vibe'); END")
        yield (f'CREATE TRIGGER recording_vibes_{operation.lower()} BEFORE {operation} ON recording_vibes '
               'WHEN NEW.measurement_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM measurements m '
               'WHERE m.id=NEW.measurement_id AND m.source=NEW.source AND m.score=NEW.score AND m.support=NEW.support) '
               'OR NEW.prediction_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM predictions p '
               'WHERE p.id=NEW.prediction_id AND p.score=NEW.score AND p.support=NEW.support AND p.kind=NEW.source '
               'AND (p.lineage_complete=0 OR (EXISTS (SELECT 1 FROM prediction_inputs i WHERE i.prediction_id=p.id) '
               "AND (p.kind='genre' OR EXISTS (SELECT 1 FROM prediction_artists a WHERE a.prediction_id=p.id))))) "
               "BEGIN SELECT RAISE(ABORT,'Current vibe does not match its original score'); END")
    for operation in ('INSERT', 'UPDATE', 'DELETE'):
        sides = ('OLD', 'NEW') if operation == 'UPDATE' else (('OLD',) if operation == 'DELETE' else ('NEW',))
        writes = ''.join('INSERT INTO rec_content_pending(identity_key) SELECT identity_key FROM recordings '
                         f'WHERE id={side}.recording_id ON CONFLICT(identity_key) DO UPDATE SET revision=revision+1;'
                         for side in sides)
        yield f'CREATE TRIGGER sample_attributes_{operation.lower()} AFTER {operation} ON sample_attributes BEGIN {writes} END'
    yield ("CREATE TRIGGER sample_attributes_identity BEFORE UPDATE OF recording_id ON sample_attributes "
           "WHEN NEW.recording_id<>OLD.recording_id AND EXISTS (SELECT 1 FROM measurement_samples WHERE sample_id=OLD.id) "
           "BEGIN SELECT RAISE(ABORT,'Retire measurement sample links before reassociation'); END")
    for table in ('measurements', 'predictions'):
        yield (f'CREATE TRIGGER {table}_immutable BEFORE UPDATE ON {table} '
               "BEGIN SELECT RAISE(ABORT,'Vibe calculations are immutable'); END")
    # An exact-video correction moves its attribute file references, never its
    # old calculations. Historical input digests/scores remain immutable. Links
    # whose recording ownership no longer holds are retired before reassociation.
    writes = ''
    for kind, condition, artist, genre in (
            ('artist_genre', 'artist_key IS NOT NULL AND subgenre_key IS NOT NULL', 'artist_key', 'subgenre_key'),
            ('artist', 'artist_key IS NOT NULL', 'artist_key', 'NULL'),
            ('genre', 'subgenre_key IS NOT NULL', 'NULL', 'subgenre_key')):
        writes += (f"INSERT INTO rec_vibe_pending(group_key) SELECT DISTINCT json_array('{kind}',{artist},{genre}) "
                   f'FROM recording_vibes WHERE recording_id=OLD.recording_id AND {condition} '
                   "ON CONFLICT(group_key) DO UPDATE SET revision=revision+1,cursor='';")
    writes += ('DELETE FROM recording_vibes WHERE recording_id=OLD.recording_id;'
               'DELETE FROM measurement_samples WHERE sample_id IN (SELECT id FROM sample_attributes WHERE video_id=NEW.video_id);'
               'UPDATE sample_attributes SET recording_id=NEW.recording_id WHERE video_id=NEW.video_id;'
               'INSERT INTO rec_content_pending(identity_key) SELECT identity_key FROM recordings '
               'WHERE id IN (OLD.recording_id,NEW.recording_id) ON CONFLICT(identity_key) DO UPDATE SET revision=revision+1;')
    yield ('CREATE TRIGGER vibe_sample_reassociation AFTER UPDATE OF recording_id ON youtube_items '
           f'WHEN NEW.recording_id<>OLD.recording_id BEGIN {writes} END')


def apply(conn, context):
    # Retire old index names before creating their replacements.
    for name in ('idx_vibe_artist_genre', 'idx_vibe_artist', 'idx_vibe_genre'):
        conn.execute(f'DROP INDEX {name}')
    for sql in statements():
        conn.execute(sql)
    root = None
    for sid, run, point, raw, captured in conn.execute('SELECT * FROM audio_vibe_samples'):
        if root is None:
            root = sample_files.directory(conn)
        sample = json.loads(raw)
        filename = sample_files.publish(root, sample)
        rid = sample_files.recording(conn, sample, captured)
        conn.execute('INSERT INTO sample_attributes VALUES (?,?,?,?,?,?,?)',
                     (sid, rid, sample['video_id'], run, point, filename, captured))
    _scores(conn)
    conn.execute('DROP TABLE audio_vibe_samples')
    conn.execute('DROP TABLE rec_vibe_tracks')
    conn.execute('DROP TABLE rec_vibe_groups')
    conn.execute("INSERT INTO rec_vibe_pending(group_key) VALUES ('*') "
                 "ON CONFLICT(group_key) DO UPDATE SET revision=revision+1,cursor=''")


def _scores(conn):
    """Preserve existing computations and serving scores before bounded rebuilding."""
    from yt_playlist.util import genre_catalog
    tracks, measurements = {}, {}
    for key, artist, genre, raw, version in conn.execute('SELECT * FROM rec_vibe_tracks'):
        row = conn.execute('SELECT id FROM recordings WHERE identity_key=?', (key,)).fetchone()
        if row is None:
            raise ValueError(f'Vibe score has no recording: {key}')
        rid = row[0]
        if genre:
            conn.execute("INSERT OR IGNORE INTO genre_catalog(key,name,source) VALUES (?,?,'custom')",
                         (genre, genre_catalog.canonical_name(genre)))
        evidence = json.loads(raw) if raw else None
        tracks[rid] = (artist, genre, evidence, version)
        if not evidence:
            continue
        digest = hashlib.sha256(raw.encode()).hexdigest()
        for vibe, fit in evidence['fits'].items():
            mid = conn.execute('INSERT INTO measurements(recording_id,vibe,score,support,source,model_version,input_digest,measured_at) '
                'VALUES (?,?,?,?,?,?,?,?)', (rid, vibe, fit['score'], fit['support'], evidence['source'], version, digest, None)).lastrowid
            measurements[rid, vibe] = mid
            for sid in evidence.get('sample_ids', []):
                # Retain the measured score even if a raw excerpt was already retired.
                conn.execute('INSERT INTO measurement_samples SELECT ?,id FROM sample_attributes WHERE id=? AND recording_id=?',
                             (mid, sid, rid))
    for key, raw, prepared_at in conn.execute('SELECT identity_key,payload,prepared_at FROM rec_musical_profiles'):
        profile = json.loads(raw)
        vibes = profile.pop('vibes', None)
        if not vibes:
            continue
        rid = conn.execute('SELECT id FROM recordings WHERE identity_key=?', (key,)).fetchone()[0]
        artist, genre, _evidence, version = tracks.get(rid, (None, None, None, vibes.get('inputs_version', 'legacy')))
        version = vibes.get('inputs_version') or version
        for vibe, fit in vibes.get('fits', {}).items():
            source, mid, pid = fit['source'], None, None
            if source in ('audio_measurement', 'provider_measurement'):
                mid = measurements.get((rid, vibe))
                if mid is not None:
                    original = conn.execute('SELECT score,support,source,model_version FROM measurements WHERE id=?', (mid,)).fetchone()
                    if tuple(original) != (fit['score'], fit['support'], source, version):
                        mid = None  # Last-good serving result predates the current input cache.
                if mid is None:
                    mid = conn.execute('INSERT INTO measurements(recording_id,vibe,score,support,source,model_version,input_digest,measured_at) '
                        'VALUES (?,?,?,?,?,?,?,?)', (rid, vibe, fit['score'], fit['support'], source, version,
                        hashlib.sha256(raw.encode()).hexdigest(), None)).lastrowid
            elif source != 'heuristic':
                # The old JSON projection did not retain contributor IDs. Do
                # not invent historical causation from today's cohort members.
                pid = conn.execute('INSERT INTO predictions(recording_id,vibe,score,support,subgenre_key,kind,lineage_complete,policy_version,predicted_at) '
                    'VALUES (?,?,?,?,?,?,0,?,?)', (rid, vibe, fit['score'], fit['support'], genre if source != 'artist' else None,
                    source, version, prepared_at)).lastrowid
                for aid in json.loads(artist or '[]') if source != 'genre' else []:
                    conn.execute('INSERT INTO prediction_artists VALUES (?,?)', (pid, aid))
            conn.execute('INSERT INTO recording_vibes VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                (rid, vibe, fit['score'], fit['support'], source, mid, pid, version, artist, genre,
                 fit.get('recordings'), fit.get('variance')))
        encoded = json.dumps(profile, sort_keys=True, separators=(',', ':'), allow_nan=False)
        conn.execute('UPDATE rec_musical_profiles SET payload=?,digest=? WHERE identity_key=?',
                     (encoded, hashlib.sha256(encoded.encode()).hexdigest(), key))


def validate(conn):
    from . import v054_audio_vibes, v058_predicted_vibes
    from .current import _tokens
    from .runner import MigrationError
    retained = [sql for sql in v054_audio_vibes.statements() if sql.startswith('CREATE TABLE audio_vibe_feedback')]
    retained += [sql for sql in v058_predicted_vibes.statements()
                 if sql.startswith(('CREATE TABLE rec_vibe_pending', 'CREATE TRIGGER vibe_artists_'))]
    for sql in (*statements(), *retained):
        name = sql.split()[2]
        row = conn.execute('SELECT sql FROM sqlite_master WHERE name=?', (name,)).fetchone()
        if row is None or _tokens(row[0]) != _tokens(sql):
            raise MigrationError(f'Incompatible recording vibe structure: {name}')
    if conn.execute("SELECT 1 FROM sqlite_master WHERE name IN ('audio_vibe_samples','rec_vibe_tracks','rec_vibe_groups')").fetchone():
        raise MigrationError('Superseded vibe stores remain')
