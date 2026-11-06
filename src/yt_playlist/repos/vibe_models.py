"""Persistence for shared per-vibe model generations (#819).

SQL owns only calculation-to-generation identity. Parameters, original training
manifest, conditioning data, diagnostics and posterior bytes belong to this
domain. A generation is shared by every prediction from that per-vibe fit.
"""
from yt_playlist.core import file_paths
import hashlib
import json
import re
from collections import OrderedDict

from yt_playlist.core import fao, file_collection, vibe_model_files as files, vibe_versions
from yt_playlist.core.activity import WorkBudgetExceeded, check_stopping
from yt_playlist.core.evidence_ledger import canonical

MAX_BYTES = files.MAX_BYTES
HEAD_PREFIX = files.HEAD_PREFIX
STATE_PREFIX = files.STATE_PREFIX
REVISION = files.REVISION
_DIGEST = re.compile(r'[0-9a-f]{64}\Z')


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


def validate(conn):
    from yt_playlist.core.schema_contract import tokens
    for name, sql in schema(conn):
        saved = conn.execute('SELECT sql FROM sqlite_schema WHERE name=?', (name,)).fetchone()
        if saved is None or tokens(saved[0]) != tokens(sql):
            raise ValueError('Incompatible shared vibe model reference: ' + name)


def available(conn):
    return bool(conn.execute("SELECT 1 FROM sqlite_schema WHERE name='prediction_model_generations'").fetchone())


def directory(conn):
    return file_paths.directory(conn, 'vibe-models')


def relative(generation):
    return files.relative(generation)


def encode(value):
    return files.encode(value)


def read(conn, generation):
    value = files.read(directory(conn), generation)
    inputs = {row['measurement']:row for row in value['inputs']}
    identities, found = list(inputs), set()
    for start in range(0, len(identities), 128):
        check_stopping()
        batch = identities[start:start+128]
        rows = conn.execute('SELECT m.id,m.recording_id,m.vibe,m.score,m.support,m.source,m.input_digest,'
            + vibe_versions.expression(conn, 'measurements', 'm') + ' AS exact_version FROM measurements m '
            'WHERE m.id IN (' + ','.join('?' for _ in batch) + ')', batch)
        for row in rows:
            item = inputs[row['id']]
            expected = (item['recording'], value['vibe'], item['score'], item['support'],
                        item.get('input_digest'), item.get('version'), item['source'])
            actual = (row['recording_id'], row['vibe'], row['score'], row['support'],
                      row['input_digest'], row['exact_version'], row['source'] + '/' + row['exact_version'])
            if expected != actual:
                raise fao.CorruptFile('Shared vibe manifest differs from its original measurements')
            found.add(row['id'])
    if found != inputs.keys():
        raise fao.CorruptFile('Shared vibe manifest requires missing original measurements')
    return value


def stage(conn, value):
    return files.stage(directory(conn), value)


def measurement_ids(value):
    return files.measurement_ids(value)


def conditioning_albums(conn, recordings):
    """Resolve only unambiguous album identities in bounded indexed batches."""
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
        result.update((row[0], str(row[1])) for row in conn.execute(
            'SELECT recording_id,min(album_id) FROM (' + sql + ') GROUP BY recording_id '
            'HAVING count(DISTINCT album_id)=1', args))
    return result


def training_manifest(conn, vibe, limit=32768, max_bytes=8 * 1024 * 1024):
    """Read original-only inputs from a caller's consistent SQL snapshot.

    Also used by isolated research without constructing a Store or migrating
    its read-only source. The limit fails closed; it is not a sampling policy.
    """
    rows = conn.execute('SELECT v.recording_id,v.measurement_id,v.artist_key,v.subgenre_key,'
        'm.score,m.support,m.source,m.input_digest,' + vibe_versions.expression(conn, 'measurements', 'm') +
        ' AS exact_version FROM recording_vibes v JOIN measurements m ON m.id=v.measurement_id '
        'WHERE v.vibe=? AND m.support>0 ORDER BY v.recording_id LIMIT ?', (vibe, limit + 1)).fetchall()
    if len(rows) > limit:
        raise WorkBudgetExceeded('Joint vibe original manifest exceeds bounded admission')
    albums = conditioning_albums(conn, [row['recording_id'] for row in rows])
    result, used = [], 0
    for row in rows:
        check_stopping()
        item = {'recording': row['recording_id'], 'measurement': row['measurement_id'],
            'artists': json.loads(row['artist_key'] or '[]'), 'genre': row['subgenre_key'],
            'album': albums.get(row['recording_id']),
            'source': row['source'] + '/' + row['exact_version'],
            'score': row['score'], 'support': row['support'],
            'input_digest': row['input_digest'], 'version': row['exact_version']}
        used += len(canonical(item))
        if len(result) == limit or used > max_bytes:
            raise WorkBudgetExceeded('Joint vibe original manifest exceeds bounded admission')
        result.append(item)
    return result


def receipt(generation, artists, mean_variance, new_track_variance, *, conditioning=None):
    return files.receipt(generation, artists, mean_variance, new_track_variance, conditioning=conditioning)


def is_receipt(value):
    return files.is_receipt(value)


def linked(conn, prediction):
    if not available(conn):
        return None
    row = conn.execute('SELECT generation FROM prediction_model_generations WHERE prediction_id=?',
                       (prediction,)).fetchone()
    return row[0].hex() if row else None


def subject(conn, prediction):
    return conn.execute('SELECT vibe,recording_id FROM predictions WHERE id=?', (prediction,)).fetchone()


def same_receipt(conn, prediction, value):
    row = conn.execute('SELECT input_fingerprint,state FROM prediction_provenance WHERE prediction_id=?',
                       (prediction,)).fetchone()
    return bool(row and row[1] == 'ready' and row[0] == hashlib.sha256(canonical(value)).digest()
                and linked(conn, prediction) == value['generation'])


def root_measurements(conn, identities):
    conn.executemany('INSERT OR IGNORE INTO prediction_measurement_roots VALUES (?)', [(mid,) for mid in identities])


def publish_receipts(conn, values, *, stage_receipts, load_manifest):
    """Rebuild compact references without expanding shared contributor graphs."""
    loaded = OrderedDict()
    for pid, receipt, artists in values:
        generation = receipt['generation']
        if generation not in loaded:
            loaded[generation] = load_manifest(conn, generation)
            if len(loaded) > 8:
                loaded.popitem(last=False)
        loaded.move_to_end(generation)
        model, prediction = loaded[generation], subject(conn, pid)
        if prediction is None or prediction[0] != model['vibe'] or prediction[1] in model['recordings']:
            raise ValueError('Shared model does not match rebuilt prediction')
        if sorted(set(artists)) != receipt['artists']:
            raise ValueError('Shared model artist conditioning changed')
        for artist in artists:
            if type(artist) is not int or not conn.execute('SELECT 1 FROM artists WHERE id=?', (artist,)).fetchone():
                raise ValueError('Unknown conditioning artist')
        if receipt != files.receipt(generation, artists, receipt['mean_variance'], receipt['new_track_variance'],
                                    conditioning=receipt.get('conditioning')):
            raise ValueError('Invalid shared model receipt')
        root_measurements(conn, model['inputs'])
        previous = linked(conn, pid)
        if previous is not None and previous != generation:
            raise ValueError('Rebuilt calculation changed its shared model identity')
        marker = conn.execute('SELECT 1 FROM prediction_provenance WHERE prediction_id=?', (pid,)).fetchone()
        if marker and not same_receipt(conn, pid, receipt):
            raise ValueError('Rebuilt calculation changed its shared model receipt')
        conn.execute('INSERT OR IGNORE INTO prediction_model_generations VALUES (?,?)', (pid,bytes.fromhex(generation)))
    staged, markers, _roots = stage_receipts(conn, values)
    try:
        for publication in staged:
            publication.publish()
        conn.executemany("INSERT INTO prediction_provenance VALUES (?,?,?,'ready') "
                         'ON CONFLICT(prediction_id) DO NOTHING', markers)
    finally:
        for publication in staged:
            publication.close()


def references(conn):
    if available(conn):
        seen = set()
        # Expired history no longer requires its model. The SQL identity remains
        # until bounded prediction-history retirement removes that calculation.
        for generation, in conn.execute("SELECT DISTINCT g.generation FROM prediction_model_generations g "
                "JOIN prediction_provenance p ON p.prediction_id=g.prediction_id WHERE p.state='ready'"):
            seen.add(generation.hex())
            yield directory(conn) / relative(generation.hex())
        for generation, in conn.execute("SELECT value FROM settings WHERE key GLOB ?", (HEAD_PREFIX + '*',)):
            if generation and generation not in seen:
                seen.add(generation)
                yield directory(conn) / relative(generation)


def _referenced(conn, generation):
    return (conn.execute('SELECT 1 FROM settings WHERE key GLOB ? AND value=? LIMIT 1',
                         (HEAD_PREFIX + '*', generation)).fetchone()
            or conn.execute("SELECT 1 FROM prediction_model_generations g JOIN prediction_provenance p "
                            "ON p.prediction_id=g.prediction_id WHERE g.generation=? AND p.state='ready' LIMIT 1",
                            (bytes.fromhex(generation),)).fetchone())


def collect(conn, now, limit=16):
    """At most limit raw entries, reader/build fence plus writer reservation."""
    if conn.in_transaction or not 1 <= limit <= 128:
        raise ValueError('Shared vibe collection requires committed SQL and a bounded limit')
    root = directory(conn)
    with file_collection.collection(conn, root, name='.collection.json',
                                    valid_shard=lambda name: bool(re.fullmatch('[0-9a-f]{2}', name))) as work:
        with file_collection.collector(root) as admitted:
            if not admitted:
                return 0
            conn.execute('BEGIN IMMEDIATE')
            removed = 0
            try:
                for _ in range(limit):
                    path = work.next()
                    if path is None or path.suffix != '.json' or not _DIGEST.fullmatch(path.stem):
                        continue
                    if path.relative_to(root) != relative(path.stem) or _referenced(conn, path.stem):
                        continue
                    # Includes verified-but-unpublished files after rollback.
                    if (fao.read(path).info.ctime_ns / 1e9 <= now - 30*86400):
                        fao.delete(path)
                        removed += 1
                work.save()
                return removed
            finally:
                conn.rollback()
