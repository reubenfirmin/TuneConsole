"""Observe legacy discovery subjects/origins without replacing serving owners."""
import hashlib
import json

from .. import artist_keys, discovery_provenance as provenance
from ..evidence_ledger import canonical
from .album_occurrences import clipped
from .rows import Job, main as run
from .workflow_receipts import number


MAX_ROW_BYTES = 128 * 1024
ORIGINALS = 'prepared_discovery_row_originals'
CLASSIFICATION_ORIGINALS = 'prepared_discovery_classification_originals'
SCANS = 'prepared_discovery_scan_rows'
SOURCES = 'prepared_discovery_source_rows'
ARTISTS = 'prepared_discovery_artist_source_rows'
IMPRESSIONS = 'prepared_discovery_impression_rows'
CANDIDATES = 'prepared_discovery_membership_rows'
CLASSIFICATIONS = 'prepared_discovery_classification_rows'
KEYS = {'artist_scans': ('artist',), 'discovery_sources': ('recording_id','source_token'),
    'discovery_artist_sources': ('artist_id','source'), 'discovery_visible_impressions': ('view_id','kind','item_id'),
    'discovery_memberships': ('recording_id',), 'album_discovery_memberships': ('album_id',)}
DEPENDENCIES = ('artists','artist_redirects','artist_aliases','artist_provider_ids','recordings','recording_redirects',
    'recording_aliases','youtube_items','albums','album_provider_ids','genre_catalog','genre_alias','identities')


def artist(conn, identity):
    if identity is None:
        return None
    known = conn.execute('SELECT id FROM main.artists WHERE id=?', (identity,)).fetchone()
    return artist_keys.canonical_id(conn, identity) if known else None


def upsert(conn, table, values):
    conn.execute(f'INSERT INTO {table}({",".join(values)}) VALUES ({",".join("?" for _ in values)}) '
        'ON CONFLICT(source_owner,source_key) DO UPDATE SET ' +
        ','.join(field + '=excluded.' + field for field in values if field not in ('source_owner','source_key')), tuple(values.values()))


def describe(conn, owner, key, row):
    """Resolve one retained input without changing preparation or live owners."""
    row = dict(row)
    for field, value in row.items():
        if isinstance(value, bytes):
            row[field] = value.decode()
    raw = canonical(row)
    if len(raw) > MAX_ROW_BYTES:
        raise ValueError('Discovery source exceeds its preparation byte budget')
    digest = hashlib.sha256(raw).hexdigest()
    values = {'source_owner': owner, 'source_key': key, 'source_digest': digest}
    if owner == 'artist_scans':
        identity = artist(conn, row['artist_id'])
        values.update(captured_artist_id=row['artist_id'], artist_id=identity, scanned_at=row['scanned_at'],
                      resolution_reason=None if identity is not None else 'missing_canonical_artist')
        target = SCANS
    elif owner in ('discovery_sources','discovery_artist_sources'):
        is_artist = owner == 'discovery_artist_sources'
        token = row['source'] if is_artist else row['source_token']
        origin = provenance.origin_fields(conn, token, account_id=row['identity_id'] if is_artist else None)
        values.update(origin)
        if is_artist:
            identity = artist(conn, row['artist_id'])
            values.update(captured_artist_id=row['artist_id'], subject_artist_id=identity, score=row['score'],
                          subject_resolution_reason=None if identity is not None else 'missing_canonical_artist')
        else:
            values.update(recording_id=row['recording_id'], input_video_id=row['input_video_id'],
                          first_found_at=row['found_at'], last_found_at=row['last_found_at'])
        target = ARTISTS if is_artist else SOURCES
    elif owner == 'discovery_visible_impressions':
        kind, value = row['kind'], row['item_id']
        if kind == 'artist' and value.isascii() and value.isdigit():
            value = int(value)
        identifier = provenance.subject(conn, kind, value) if kind in provenance.SUBJECTS else None
        reason = None
        if identifier is None:
            reason = 'unsupported_kind' if kind not in provenance.SUBJECTS else 'missing_owner'
            if kind == 'track' and len({r[0] for r in conn.execute(
                    'SELECT recording_id FROM main.recording_aliases WHERE alias=?', (value,))}) > 1:
                reason = 'ambiguous_owner'
        values.update(view_id=row['view_id'], original_kind=kind, original_key=row['item_id'],
            shown_at=row['shown_at'], recording_id=None, artist_id=None, album_id=None, resolution_reason=reason)
        if identifier is not None:
            values[provenance.SUBJECTS[kind]] = identifier
        target = IMPRESSIONS
    else:
        kind, field = ('track','recording_id') if owner == 'discovery_memberships' else ('album','album_id')
        values.update(kind=kind, recording_id=None, album_id=None, entry_id=row.get('entry_id'),
            found_at=row['found_at'], first_shown=row['first_shown'], last_shown=row['last_shown'],
            offered_count=row['offered_count'], legacy_offered_count=row.get('legacy_offered_count',0),
            legacy_first_shown=row.get('legacy_first_shown'), legacy_last_shown=row.get('legacy_last_shown'))
        values[field] = row[field]
        target = CANDIDATES
    return target, values, raw.decode()


def copy(conn, owner, key, row):
    target, values, raw = describe(conn, owner, key, row)
    conn.execute(f'INSERT INTO {ORIGINALS} VALUES (?,?,?,?) ON CONFLICT(source_owner,source_key,source_digest) DO NOTHING',
                 (owner, key, values['source_digest'], raw))
    upsert(conn, target, values)


TARGETS = {'artist_scans': SCANS, 'discovery_sources': SOURCES, 'discovery_artist_sources': ARTISTS,
    'discovery_visible_impressions': IMPRESSIONS, 'discovery_memberships': CANDIDATES, 'album_discovery_memberships': CANDIDATES}


def delete(conn, owner, key):
    conn.execute(f'DELETE FROM {TARGETS[owner]} WHERE source_owner=? AND source_key=?', (owner, key))


def original(conn, owner, key, digest):
    archive = CLASSIFICATION_ORIGINALS if owner == 'genre_classification_legacy' else ORIGINALS
    row = conn.execute(f'SELECT snapshot FROM {archive} WHERE source_owner=? AND source_key=? AND source_digest=?', (owner, key, digest)).fetchone()
    if row is None or hashlib.sha256(row[0].encode()).hexdigest() != digest:
        raise ValueError('Prepared discovery original differs from its captured bytes')
    return json.loads(row[0])


def copy_classification(conn, owner, key, row):
    raw = canonical(row)
    if len(raw) > MAX_ROW_BYTES:
        raise ValueError('Discovery classification exceeds its preparation byte budget')
    digest = hashlib.sha256(raw).hexdigest()
    conn.execute(f'INSERT INTO {CLASSIFICATION_ORIGINALS} VALUES (?,?,?,?) ON CONFLICT(source_owner,source_key,source_digest) DO NOTHING',
                 (owner, key, digest, raw.decode()))
    upsert(conn, CLASSIFICATIONS, {'source_owner': owner, 'source_key': key, 'source_digest': digest, **row})


def delete_classification(conn, owner, key):
    conn.execute(f'DELETE FROM {CLASSIFICATIONS} WHERE source_owner=? AND source_key=?', (owner, key))


def definitions():
    yield ORIGINALS, f'''CREATE TABLE {ORIGINALS} (
        source_owner TEXT NOT NULL,source_key TEXT NOT NULL CHECK(json_valid(source_key)),source_digest TEXT NOT NULL CHECK(length(source_digest)=64),
        snapshot TEXT NOT NULL CHECK(json_valid(snapshot)),PRIMARY KEY(source_owner,source_key,source_digest)) WITHOUT ROWID'''
    origin_columns = '''kind TEXT NOT NULL CHECK(kind IN ('album','radio','genre','artist','artist_start','discovery','spotify_export','spotify_model','legacy_unknown')),
        account_id INTEGER,album_id INTEGER,seed_video_id TEXT,genre_key TEXT,artist_id INTEGER,import_key TEXT,legacy_source_key TEXT,
        resolution_status TEXT NOT NULL CHECK(resolution_status IN ('resolved','unresolved','legacy_account')),resolution_reason TEXT'''
    origin_checks = '''CHECK((album_id IS NOT NULL)=(kind='album')),CHECK((seed_video_id IS NOT NULL)=(kind='radio')),
        CHECK((genre_key IS NOT NULL)=(kind='genre')),CHECK((artist_id IS NOT NULL)=(kind IN ('artist','artist_start'))),
        CHECK((legacy_source_key IS NOT NULL)=(kind='legacy_unknown')),
        CHECK((resolution_status='unresolved')=(kind='legacy_unknown')),
        CHECK((resolution_status='legacy_account')=(kind='spotify_export' AND import_key IS NULL)),
        CHECK((resolution_reason IS NOT NULL)=(kind='legacy_unknown'))'''
    # Put table constraints after all columns, including fields supplied by each
    # source owner. All references are observations, not foreign keys to mutable
    # serving owners: an incomplete copy cannot prevent their deletion.
    specs = {
        SCANS: (f'''captured_artist_id INTEGER,artist_id INTEGER,{number('scanned_at')},resolution_reason TEXT''',
                "CHECK((artist_id IS NULL)=(resolution_reason='missing_canonical_artist'))"),
        SOURCES: (f'''recording_id INTEGER NOT NULL,input_video_id TEXT,{number('first_found_at')},{number('last_found_at')},{origin_columns}''', origin_checks),
        ARTISTS: (f'''captured_artist_id INTEGER,subject_artist_id INTEGER,score REAL NOT NULL,subject_resolution_reason TEXT,{origin_columns}''',
                  origin_checks + ",CHECK((subject_artist_id IS NULL)=(subject_resolution_reason='missing_canonical_artist'))"),
        IMPRESSIONS: (f'''view_id TEXT NOT NULL,original_kind TEXT NOT NULL,original_key TEXT NOT NULL,
            {number('shown_at', required=True)},recording_id INTEGER,artist_id INTEGER,album_id INTEGER,resolution_reason TEXT''',
            "CHECK((recording_id IS NOT NULL)+(artist_id IS NOT NULL)+(album_id IS NOT NULL)=CASE WHEN resolution_reason IS NULL THEN 1 ELSE 0 END),"
            "CHECK(resolution_reason IS NULL OR resolution_reason IN ('missing_owner','ambiguous_owner','unsupported_kind'))"),
        CANDIDATES: (f'''kind TEXT NOT NULL CHECK(kind IN ('track','album')),recording_id INTEGER,album_id INTEGER,entry_id INTEGER,
            {number('found_at')},{number('first_shown')},{number('last_shown')},offered_count INTEGER NOT NULL CHECK(offered_count>=0),
            legacy_offered_count INTEGER NOT NULL CHECK(legacy_offered_count>=0),{number('legacy_first_shown')},{number('legacy_last_shown')}''',
            "CHECK((recording_id IS NOT NULL)=(kind='track')),CHECK((album_id IS NOT NULL)=(kind='album'))"),
    }
    for name, (columns, checks) in specs.items():
        yield name, f'''CREATE TABLE {name} (
            source_owner TEXT NOT NULL,source_key TEXT NOT NULL CHECK(json_valid(source_key)),source_digest TEXT NOT NULL CHECK(length(source_digest)=64),
            {columns},PRIMARY KEY(source_owner,source_key),{checks}) WITHOUT ROWID'''


PROJECTIONS = {
    'artist_scans': (clipped('artist') + ' AS artist','scanned_at','artist_id'),
    'discovery_sources': ('recording_id',clipped('source_token') + ' AS source_token','input_video_id','found_at',
                          clipped('source_type') + ' AS source_type',clipped('source_id') + ' AS source_id','last_found_at'),
    'discovery_artist_sources': ('artist_id',clipped('source') + ' AS source','identity_id','score'),
    'discovery_visible_impressions': (*(clipped(field) + ' AS ' + field for field in ('view_id','kind','item_id')),'shown_at'),
}
JOB = Job('discovery_subjects', 79, 100, KEYS, tuple(definitions()), copy, delete,
    dependencies=DEPENDENCIES, optional_dependencies=('genre_redirects',),
    batch_limit=16, refresh_highwater=False, projections=PROJECTIONS)
CLASSIFICATION_JOB = Job('discovery_classifications', 86, 100, {'genre_classification_legacy': ('id',)},
    ((CLASSIFICATION_ORIGINALS, tuple(definitions())[0][1].replace(ORIGINALS,CLASSIFICATION_ORIGINALS)), (CLASSIFICATIONS, f'''CREATE TABLE {CLASSIFICATIONS} (
        source_owner TEXT NOT NULL,source_key TEXT NOT NULL CHECK(json_valid(source_key)),source_digest TEXT NOT NULL CHECK(length(source_digest)=64),
        id INTEGER NOT NULL,recording_id INTEGER,album_id INTEGER,artist_id INTEGER,raw_label TEXT NOT NULL,genre_key TEXT,resolution_reason TEXT,
        PRIMARY KEY(source_owner,source_key),CHECK((recording_id IS NOT NULL)+(album_id IS NOT NULL)+(artist_id IS NOT NULL)=1)) WITHOUT ROWID''')),
    copy_classification, delete_classification, batch_limit=16, refresh_highwater=False,
    projections={'genre_classification_legacy': ('id','recording_id','album_id','artist_id',clipped('raw_label') + ' AS raw_label',
                                                clipped('genre_key') + ' AS genre_key','resolution_reason')})


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(description=__doc__, add_help=False)
    parser.add_argument('job', choices=('rows','classifications'))
    args, remaining = parser.parse_known_args(argv)
    return run(JOB if args.job == 'rows' else CLASSIFICATION_JOB, remaining)


if __name__ == '__main__':
    raise SystemExit(main())
