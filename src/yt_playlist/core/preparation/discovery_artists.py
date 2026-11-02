"""Retain discovery candidate explanations and membership precision separately."""
import hashlib
import json
from pathlib import Path

from .. import collection_recovery, genre_identity, recommendation_outputs as files
from ..evidence_ledger import canonical
from ..schema_contract import MigrationError
from . import discovery_subjects, document_plan, taste_controls
from .ordered_rows import OrderedJob
from .rows import Job, main as run
from .workflow_receipts import number


MAX_SOURCE_BYTES = 32 * 1024 * 1024
PART_CHARACTERS = 64 * 1024


def table(name, suffix):
    return 'prepared_discovery_' + name + '_' + suffix


def definitions(name):
    yield table(name, 'generations'), f'''CREATE TABLE {table(name,'generations')} (
        source TEXT NOT NULL,preparation_revision INTEGER NOT NULL CHECK(preparation_revision>=0),
        captured_artist_id INTEGER,artist_id INTEGER,artist_resolution TEXT CHECK(artist_resolution='missing_canonical_artist'),
        score REAL,thumbnail TEXT,{number('found_at')},{number('first_shown')},{number('last_shown')},
        offered_count INTEGER NOT NULL CHECK(offered_count>=0),legacy_offered_count INTEGER NOT NULL CHECK(legacy_offered_count>=0),
        {number('legacy_first_shown')},{number('legacy_last_shown')},
        captured_genre TEXT,genre_key TEXT,genre_resolution TEXT CHECK(genre_resolution IN ('unknown_genre','ambiguous_genre','retained_unresolved')),
        explanation_status TEXT NOT NULL CHECK(explanation_status IN ('recorded','malformed')),
        raw_digest TEXT NOT NULL CHECK(length(raw_digest)=64),raw_size INTEGER NOT NULL CHECK(raw_size BETWEEN 1 AND {MAX_SOURCE_BYTES}),
        part_count INTEGER NOT NULL CHECK(part_count BETWEEN 1 AND 512),unit_count INTEGER NOT NULL CHECK(unit_count>=1),
        recovery_filename TEXT NOT NULL,recovery_digest TEXT NOT NULL CHECK(length(recovery_digest)=64),
        recovery_bytes INTEGER NOT NULL CHECK(recovery_bytes BETWEEN 1 AND {collection_recovery.MAX_BYTES}),
        plan_filename TEXT NOT NULL,plan_digest TEXT NOT NULL CHECK(length(plan_digest)=64),plan_bytes INTEGER NOT NULL CHECK(plan_bytes BETWEEN 1 AND {files.MAX_BYTES}),
        PRIMARY KEY(source,preparation_revision),CHECK((artist_id IS NULL)=(artist_resolution IS NOT NULL))) WITHOUT ROWID'''
    yield table(name, 'heads'), f'''CREATE TABLE {table(name,'heads')} (
        source TEXT PRIMARY KEY,preparation_revision INTEGER NOT NULL,
        FOREIGN KEY(source,preparation_revision) REFERENCES {table(name,'generations')}(source,preparation_revision)) WITHOUT ROWID'''
    yield table(name, 'parts'), f'''CREATE TABLE {table(name,'parts')} (
        source TEXT NOT NULL,preparation_revision INTEGER NOT NULL,position INTEGER NOT NULL CHECK(position>=0),filename TEXT NOT NULL,
        content_digest TEXT NOT NULL CHECK(length(content_digest)=64),byte_size INTEGER NOT NULL CHECK(byte_size BETWEEN 1 AND {files.MAX_BYTES}),
        PRIMARY KEY(source,preparation_revision,position),
        FOREIGN KEY(source,preparation_revision) REFERENCES {table(name,'generations')}(source,preparation_revision)) WITHOUT ROWID'''


def directory(conn):
    database = conn.execute('PRAGMA database_list').fetchone()[2]
    if not database:
        raise MigrationError('Discovery artist preparation requires a file-backed database')
    path = Path(database).resolve()
    return path.with_name(path.name + '.preparation') / 'discovery-artists'


def describe(name, conn, row):
    """Derive captured fields without publishing files or changing SQL owners."""
    original = {'artist': row['source']}
    for key, value in dict(row).items():
        if key == 'source':
            continue
        original[key] = value.decode() if isinstance(value, bytes) else value
    raw = canonical(original)
    if len(raw) > MAX_SOURCE_BYTES:
        raise ValueError('Discovery artist exceeds its preparation byte budget')
    try:
        because, fits = json.loads(original['because'] or '[]'), json.loads(original['fits'] or '[]')
        status = 'recorded'
    except (TypeError, ValueError):
        because, fits, status = [], [], 'malformed'
    # Valid unusual JSON shapes keep the native interpretation. Non-finite JSON
    # cannot enter recovery custody; leave an explicit failed source instead.
    canonical([because, fits])
    identity = discovery_subjects.artist(conn, original['artist_id'])
    if name == 'artists_legacy':
        captured_genre = original['genre']
        genre_key, reason = genre_identity.resolve(conn, captured_genre)
    else:
        captured_genre, genre_key = original['unresolved_genre'], original['genre_key']
        reason = 'retained_unresolved' if captured_genre is not None else None
    fields = {key: original[key] for key in ('score','thumbnail','found_at','first_shown','last_shown','offered_count',
        'legacy_offered_count','legacy_first_shown','legacy_last_shown')}
    fields.update(captured_artist_id=original['artist_id'], artist_id=identity,
        artist_resolution=None if identity is not None else 'missing_canonical_artist',
        captured_genre=captured_genre, genre_key=genre_key, genre_resolution=reason, explanation_status=status,
        raw_digest=hashlib.sha256(raw).hexdigest(), raw_size=len(raw), part_count=max(1,(len(raw.decode())+PART_CHARACTERS-1)//PART_CHARACTERS))
    return raw, fields, {'because': because or [], 'fits': fits or []}


def prepare(name, conn, row, *, revision, offset, rebuild=False):
    source = row['source']
    previous = conn.execute(f'SELECT plan_filename,plan_digest,plan_bytes FROM {table(name,"generations")} '
        'WHERE source=? AND preparation_revision=?', (source, revision)).fetchone() if offset else None
    if offset and not rebuild:
        if previous is None:
            raise MigrationError('Prepared discovery artist has no durable plan')
        return document_plan.load(source, previous)
    raw, fields, explanation = describe(name, conn, row)
    root = directory(conn)
    # Candidate-owned display output has no typed catalog members. Retain the
    # native interpretation in verified file custody and batch only original parts.
    identity = fields['artist_id']
    reference = taste_controls.recovery(root, 'discovery-candidate', identity if identity is not None else source, explanation)
    fields.update(zip(('recovery_filename','recovery_digest','recovery_bytes'), reference))
    text = raw.decode()
    units = ((position, *files.publish(root, files.encode(source, 'original:' + str(position),
              text[position*PART_CHARACTERS:(position+1)*PART_CHARACTERS]))) for position in range(fields['part_count']))
    result = document_plan.build(root, source, fields, units)
    if offset and (previous is None or tuple(previous) != result.manifest):
        raise MigrationError('Rebuilt discovery artist differs from its checkpoint')
    return result


def begin(name, conn, row, revision, plan):
    values = {'source': row['source'], 'preparation_revision': revision, **plan.fields,
        'plan_filename': plan.manifest[0], 'plan_digest': plan.manifest[1], 'plan_bytes': plan.manifest[2]}
    conn.execute(f'INSERT INTO {table(name,"generations")}({",".join(values)}) VALUES ({",".join("?" for _ in values)})', tuple(values.values()))


def copy(name, conn, row, revision, position, unit):
    conn.execute(f'INSERT INTO {table(name,"parts")} VALUES (?,?,?,?,?,?)', (row['source'], revision, *unit))


def complete(name, conn, row, revision, plan):
    conn.execute(f'INSERT INTO {table(name,"heads")} VALUES (?,?) ON CONFLICT(source) DO UPDATE SET preparation_revision=excluded.preparation_revision', (row['source'], revision))


def delete(name, conn, owner, key):
    source, = json.loads(key)
    conn.execute(f'DELETE FROM {table(name,"heads")} WHERE source=?', (source,))


def resume_read(conn, owner, key):
    source, = json.loads(key)
    return conn.execute('SELECT artist AS source FROM main.discovered_artists WHERE artist=? AND json_array(artist)=?', (source, key)).fetchone()


def head(conn, name, source):
    return conn.execute(f'SELECT g.* FROM {table(name,"heads")} h JOIN {table(name,"generations")} g '
                       'USING(source,preparation_revision) WHERE source=?', (source,)).fetchone()


def read_original(conn, name, generation):
    source = generation['source']
    parts = list(conn.execute(f'SELECT * FROM {table(name,"parts")} WHERE source=? AND preparation_revision=? ORDER BY position', (source, generation['preparation_revision'])))
    if len(parts) != generation['part_count'] or [row['position'] for row in parts] != list(range(len(parts))):
        raise MigrationError('Prepared discovery artist has incomplete originals')
    raw = ''.join(files.read(row['filename'], row['content_digest'], row['byte_size'], surface=source,
        chunk_key='original:' + str(row['position'])) for row in parts).encode()
    if len(raw) != generation['raw_size'] or hashlib.sha256(raw).hexdigest() != generation['raw_digest']:
        raise MigrationError('Prepared discovery artist differs from its captured bytes')
    return json.loads(raw)


def job(name, minimum, maximum):
    from functools import partial
    fields = ('because','fits','thumbnail','genre') if name == 'artists_legacy' else ('because','fits','thumbnail','unresolved_genre','genre_key')
    projection = ('artist AS source','artist_id','score','found_at','first_shown','last_shown','offered_count',
        'legacy_offered_count','legacy_first_shown','legacy_last_shown', *(
        f'CAST(substr(CAST({field} AS BLOB),1,{MAX_SOURCE_BYTES+1}) AS BLOB) AS {field}' for field in fields))
    definition = Job('discovery_' + name, minimum, maximum, {'discovered_artists': ('artist',)}, tuple(definitions(name)), None, None,
        dependencies=discovery_subjects.DEPENDENCIES, optional_dependencies=('genre_redirects',), refresh_highwater=False,
        projections={'discovered_artists': projection})
    return OrderedJob(definition, partial(prepare,name), partial(begin,name), partial(copy,name), partial(complete,name), partial(delete,name),
                      revision_aware=True, resume_read=resume_read)


JOBS = {'artists_legacy': job('artists_legacy',79,85), 'artists_classified': job('artists_classified',86,100)}


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(description=__doc__, add_help=False)
    parser.add_argument('job', choices=tuple(JOBS))
    args, remaining = parser.parse_known_args(argv)
    return run(JOBS[args.job], remaining)


if __name__ == '__main__':
    raise SystemExit(main())
