"""Prepare discovery fetch, image retry and provider frontier owners separately."""
import hashlib
import json
import math

from .. import discovery_checkpoints, discovery_sources, provider_frontiers, recommendation_outputs
from .rows import Job, main as run


MAX_BYTES = 64 * 1024
MAX_KEY_BYTES = 4096
PREFIXES = {'fetch-history': discovery_checkpoints.PREFIX, 'artist-images': 'discovery:artist-image:',
            'provider-refresh': provider_frontiers.PREFIX}
TABLES = {'fetch-history': 'prepared_discovery_fetch_history', 'artist-images': 'prepared_discovery_artist_images',
          'provider-refresh': 'prepared_provider_refresh_frontiers'}


def originals(name):
    return TABLES[name] + '_originals'


def original_value(row):
    if row['value'] is None or len(row['value']) > MAX_BYTES or len(row['key'].encode()) > MAX_KEY_BYTES:
        raise ValueError('Discovery checkpoint exceeds its source budget or has no recorded value')
    raw = bytes(row['value']).decode('utf-8')
    digest = hashlib.sha256(raw.encode()).hexdigest()
    return raw, digest


def _original(conn, name, row):
    raw, digest = original_value(row)
    conn.execute(f'INSERT INTO {originals(name)} VALUES (?,?,?) ON CONFLICT(source,digest) DO NOTHING',
                 (row['key'], digest, raw))
    return raw, digest


def fetch(conn, owner, key, row):
    raw, digest = _original(conn, 'fetch-history', row)
    values = fetch_values(conn, row, raw, digest)
    conn.execute(f'INSERT INTO {TABLES["fetch-history"]} VALUES (?,?,?,?,?,?) '
        'ON CONFLICT(source) DO UPDATE SET source_type=excluded.source_type,source_id=excluded.source_id,'
        'album_id=excluded.album_id,unresolved_reason=excluded.unresolved_reason,source_digest=excluded.source_digest', values)


def fetch_values(conn, row, raw, digest):
    if raw != '1':
        raise ValueError('Unsupported discovery fetch marker; original retained')
    kind, identifier = discovery_sources.source_identity(row['key'][len(PREFIXES['fetch-history']):])
    if not identifier:
        raise ValueError('Discovery fetch history requires its exact nonempty source token')
    album = (conn.execute("SELECT album_id FROM album_provider_ids WHERE provider='youtube' AND provider_id=?", (identifier,)).fetchone()
             if kind == 'album' else None)
    return (row['key'], kind, identifier, album[0] if album else None,
            'missing_album' if kind == 'album' and not album else None, digest)


def image(conn, owner, key, row):
    raw, digest = _original(conn, 'artist-images', row)
    values = image_values(conn, row, raw, digest)
    conn.execute(f'INSERT INTO {TABLES["artist-images"]} VALUES (?,?,?,?,?,?) '
        'ON CONFLICT(source) DO UPDATE SET raw_artist_id=excluded.raw_artist_id,artist_id=excluded.artist_id,'
        'unresolved_artist_id=excluded.unresolved_artist_id,retry_at=excluded.retry_at,source_digest=excluded.source_digest', values)


def image_values(conn, row, raw, digest):
    artist = row['key'][len(PREFIXES['artist-images']):]
    value = recommendation_outputs.decode_json(raw)
    if (not artist.isdecimal() or not 0 < int(artist) < 2**63
            or type(value) not in (int, float) or not math.isfinite(value)):
        raise ValueError('Artist retry requires its exact artist ID and a finite retry time')
    known = conn.execute('SELECT id FROM artists WHERE id=?', (int(artist),)).fetchone()
    return row['key'], artist, known[0] if known else None, None if known else int(artist), value, digest


def frontier(conn, owner, key, row):
    raw, digest = _original(conn, 'provider-refresh', row)
    values = frontier_values(conn, row, raw, digest)
    conn.execute(f'INSERT INTO {TABLES["provider-refresh"]} VALUES (?,?,?,?,?,?,?,?,?) '
        'ON CONFLICT(source) DO UPDATE SET raw_provider=excluded.raw_provider,provider=excluded.provider,'
        'unresolved_reason=excluded.unresolved_reason,cursor=excluded.cursor,cutoff=excluded.cutoff,'
        'fingerprint=excluded.fingerprint,revision=excluded.revision,source_digest=excluded.source_digest', values)


def frontier_values(conn, row, raw, digest):
    provider = row['key'][len(PREFIXES['provider-refresh']):]
    value = recommendation_outputs.decode_json(raw)
    if (not provider or not isinstance(value, dict) or set(value)-{'cursor','cutoff','fingerprint','revision'}
            or not {'cursor','cutoff','fingerprint'} <= value.keys() or not isinstance(value['fingerprint'], str)
            or any(type(value.get(field, 1)) is not int or not minimum <= value.get(field, 1) < 2**63
                   for field, minimum in (('cursor', 0), ('cutoff', 0), ('revision', 1)))):
        raise ValueError('Unsupported provider refresh frontier; original retained')
    known = conn.execute('SELECT provider FROM enrichment_provider_config WHERE provider=?', (provider,)).fetchone()
    return (row['key'], provider, known[0] if known else None, None if known else 'missing_provider',
            value['cursor'], value['cutoff'], value['fingerprint'], value.get('revision', 1), digest)


def definition(name, columns):
    archive = originals(name)
    table = TABLES[name]
    return (
        (archive, f'''CREATE TABLE {archive} (
            source TEXT NOT NULL, digest TEXT NOT NULL CHECK(length(digest)=64),
            raw_value TEXT NOT NULL CHECK(length(CAST(raw_value AS BLOB))<={MAX_BYTES}),
            PRIMARY KEY(source,digest)) WITHOUT ROWID'''),
        (table, f'''CREATE TABLE {table} (
            source TEXT PRIMARY KEY, {columns},
            FOREIGN KEY(source,source_digest) REFERENCES {archive}(source,digest)) WITHOUT ROWID'''),
    )


def job(name, columns, copy, dependencies):
    prefix = PREFIXES[name]
    # Indexed range predicates keep even initial/highwater selection inside the
    # installed prefix; unrelated settings do not become a per-batch scan.
    predicate = '{prefix}key>=' + repr(prefix) + ' AND {prefix}key<' + repr(prefix[:-1] + ';')
    def delete(conn, owner, changed_key):
        source = json.loads(changed_key)[0]
        if source.startswith(prefix):
            conn.execute(f'DELETE FROM {TABLES[name]} WHERE source=?', (source,))
    return Job(name.replace('-', '_'), 79, 93, {'settings': ('key',)}, definition(name, columns), copy, delete,
        dependencies=dependencies, filters={'settings': predicate}, batch_limit=16,
        projections={'settings': ('key',
            f"CASE WHEN value IS NULL THEN NULL ELSE COALESCE(substr(CAST(value AS BLOB),1,{MAX_BYTES + 1}),X'') END AS value")})


COLUMNS = {
    'fetch-history': '''source_type TEXT NOT NULL CHECK(source_type IN ('album','artist','genre','radio')),
        source_id TEXT NOT NULL CHECK(length(source_id)>0), album_id INTEGER, unresolved_reason TEXT, source_digest TEXT NOT NULL,
        CHECK((source_type='album' AND ((album_id IS NOT NULL AND unresolved_reason IS NULL)
               OR (album_id IS NULL AND unresolved_reason IS NOT NULL AND unresolved_reason='missing_album')))
           OR (source_type<>'album' AND album_id IS NULL AND unresolved_reason IS NULL))''',
    'artist-images': '''raw_artist_id TEXT NOT NULL, artist_id INTEGER, unresolved_artist_id INTEGER,
        retry_at REAL NOT NULL CHECK(abs(retry_at)<=1.7976931348623157e308), source_digest TEXT NOT NULL,
        CHECK((artist_id IS NOT NULL AND unresolved_artist_id IS NULL)
           OR (artist_id IS NULL AND unresolved_artist_id IS NOT NULL AND unresolved_artist_id>0))''',
    'provider-refresh': '''raw_provider TEXT NOT NULL CHECK(length(raw_provider)>0), provider TEXT,
        unresolved_reason TEXT, cursor INTEGER NOT NULL CHECK(cursor>=0), cutoff INTEGER NOT NULL CHECK(cutoff>=0),
        fingerprint TEXT NOT NULL, revision INTEGER NOT NULL CHECK(revision>=1), source_digest TEXT NOT NULL,
        CHECK((provider IS NOT NULL AND provider=raw_provider AND unresolved_reason IS NULL)
           OR (provider IS NULL AND unresolved_reason IS NOT NULL AND unresolved_reason='missing_provider'))''',
}
VALUES = {'fetch-history': fetch_values, 'artist-images': image_values, 'provider-refresh': frontier_values}
JOBS = {name: job(name, COLUMNS[name], copy, dependencies) for name, copy, dependencies in (
    ('fetch-history', fetch, ('album_provider_ids',)), ('artist-images', image, ('artists',)),
    ('provider-refresh', frontier, ('enrichment_provider_config',)))}


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(description=__doc__, add_help=False)
    parser.add_argument('job', choices=tuple(JOBS))
    args, remaining = parser.parse_known_args(argv)
    return run(JOBS[args.job], remaining)


if __name__ == '__main__':
    raise SystemExit(main())
