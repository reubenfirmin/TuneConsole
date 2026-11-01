"""Stage installed album rows without replacing publications or their writers."""
import hashlib
import json

from ..evidence_ledger import canonical
from ..recommendation_work import finite
from .rows import Job, main as run
from .workflow_receipts import integer, number, optional_text


MAX_ROW_BYTES = 128 * 1024
ORIGINALS = 'prepared_album_row_originals'
TRACKS = 'prepared_album_track_rows'
STATES = 'prepared_album_fetch_rows'


def clipped(field):
    return f'CAST(substr(CAST({field} AS BLOB),1,{MAX_ROW_BYTES+1}) AS TEXT)'


def entry(conn, identity):
    if identity is None:
        return None
    # Bound even a malformed installed display field before transferring it to
    # Python. An oversized result fails; it cannot become truncated evidence.
    fields = [('e.video_id', 'video_id'), ('e.source_ref', 'source_ref'),
              ('coalesce(y.title,u.title)', 'title'), ('coalesce(y.artist,u.artist)', 'artist')]
    row = conn.execute('SELECT e.id,e.recording_id,' + ','.join(clipped(field) + ' AS ' + name for field, name in fields) +
        ' FROM main.catalog_entries e LEFT JOIN main.youtube_items y ON y.video_id=e.video_id '
        'LEFT JOIN main.unresolved_items u ON u.entry_id=e.id WHERE e.id=?', (identity,)).fetchone()
    if row is None:
        raise ValueError('Album source requires its retained catalog entry')
    if len(canonical(dict(row))) > MAX_ROW_BYTES:
        raise ValueError('Album source metadata exceeds its preparation byte budget')
    return row


def provider(conn, album):
    row = conn.execute("SELECT provider_id FROM main.album_provider_ids WHERE album_id=? AND provider='youtube' "
                       'ORDER BY is_primary DESC,provider_id LIMIT 1', (album,)).fetchone()
    return row[0] if row else None


def copy(conn, owner, key, row):
    encoded = canonical(row)
    if len(encoded) > MAX_ROW_BYTES:
        raise ValueError('Album row exceeds its preparation byte budget')
    digest = hashlib.sha256(encoded).hexdigest()
    conn.execute(f'INSERT INTO {ORIGINALS} VALUES (?,?,?,?) ON CONFLICT(owner,source_key,digest) DO NOTHING',
                 (owner, key, digest, encoded.decode()))
    values = describe(conn, owner, row)
    target, identity = (STATES, 'album_id') if owner == 'album_fetch_state' else (TRACKS, 'row_id')
    conn.execute(f'INSERT INTO {target}({",".join(values)}) VALUES ({",".join("?" for _ in values)}) ON CONFLICT({identity}) DO UPDATE SET ' +
                 ','.join(field + '=excluded.' + field for field in values if field != identity), tuple(values.values()))


def describe(conn, owner, row):
    encoded = canonical(row)
    if len(encoded) > MAX_ROW_BYTES:
        raise ValueError('Album row exceeds its preparation byte budget')
    digest = hashlib.sha256(encoded).hexdigest()
    if owner == 'album_fetch_state':
        remote = provider(conn, row['album_id'])
        return dict(album_id=row['album_id'], source_digest=digest,
            fetched_at=finite(row['fetched_at']) if row['fetched_at'] is not None else None, complete=row['complete'],
            expected_track_count=integer(row['expected_track_count'], minimum=0), provider_id_snapshot=remote,
            resolution_reason=None if remote else 'missing_provider')
    observed = entry(conn, row['entry_id'])
    recording = observed['recording_id'] if observed else row['recording_id']
    source_ref = (observed['video_id'] or observed['source_ref']) if observed else None
    source_provider = ('youtube' if observed['video_id'] else 'local' if (observed['source_ref'] or '').startswith('local:') else 'unresolved') if observed else None
    remote = provider(conn, row['album_id']) if row['is_full'] else None
    return {'row_id': row['id'], 'source_digest': digest, 'album_id': row['album_id'], 'is_full': row['is_full'],
        'entry_id': row['entry_id'], 'captured_recording_id': row['recording_id'], 'recording_id': recording,
        'raw_identity_key': row['legacy_track_key'], 'raw_source_ref': source_ref,
        'position': row['position'], 'disc_number': row['disc_number'], 'track_number': row['track_number'],
        'captured_source': optional_text(row['source']), 'captured_title': observed['title'] if observed else None,
        'captured_credit': observed['artist'] if observed else None, 'source_provider': source_provider,
        'resolution_status': 'resolved' if recording is not None else 'unresolved',
        'provider_id_snapshot': remote, 'provider_resolution': 'missing_provider' if row['is_full'] and remote is None else None}


def delete(conn, owner, key):
    identity, = json.loads(key)
    target, field = (TRACKS, 'row_id') if owner == 'album_tracks' else (STATES, 'album_id')
    conn.execute(f'DELETE FROM {target} WHERE {field}=?', (identity,))


def original(conn, owner, identity, digest):
    key = json.dumps([identity], separators=(',', ':'))
    row = conn.execute(f'SELECT snapshot FROM {ORIGINALS} WHERE owner=? AND source_key=? AND digest=?', (owner, key, digest)).fetchone()
    if row is None or hashlib.sha256(row[0].encode()).hexdigest() != digest:
        raise ValueError('Prepared album original differs from its captured bytes')
    return json.loads(row[0])


DESTINATIONS = (
    (ORIGINALS, f'''CREATE TABLE {ORIGINALS} (
        owner TEXT NOT NULL CHECK(owner IN ('album_tracks','album_fetch_state')),source_key TEXT NOT NULL CHECK(json_valid(source_key)),
        digest TEXT NOT NULL CHECK(length(digest)=64),snapshot TEXT NOT NULL CHECK(json_valid(snapshot)),
        PRIMARY KEY(owner,source_key,digest)) WITHOUT ROWID'''),
    (TRACKS, f'''CREATE TABLE {TRACKS} (
        row_id INTEGER PRIMARY KEY,source_digest TEXT NOT NULL CHECK(length(source_digest)=64),album_id INTEGER NOT NULL,
        is_full INTEGER NOT NULL CHECK(is_full IN (0,1)),entry_id INTEGER,captured_recording_id INTEGER,recording_id INTEGER,raw_identity_key TEXT,raw_source_ref TEXT,
        position INTEGER CHECK(position>=0),disc_number INTEGER CHECK(disc_number>0),track_number INTEGER CHECK(track_number>0),
        captured_source TEXT NOT NULL CHECK(length(trim(captured_source))>0),captured_title TEXT,captured_credit TEXT,
        source_provider TEXT CHECK(source_provider IN ('youtube','local','unresolved')),
        resolution_status TEXT NOT NULL CHECK(resolution_status IN ('resolved','unresolved')),
        provider_id_snapshot TEXT,provider_resolution TEXT CHECK(provider_resolution='missing_provider'),
        CHECK((entry_id IS NULL)=(source_provider IS NULL)),
        CHECK((recording_id IS NULL)=(resolution_status='unresolved')),
        CHECK(is_full=0 OR (provider_id_snapshot IS NULL)=(provider_resolution IS NOT NULL)))'''),
    (STATES, f'''CREATE TABLE {STATES} (
        album_id INTEGER PRIMARY KEY,source_digest TEXT NOT NULL CHECK(length(source_digest)=64),{number('fetched_at')},
        complete INTEGER NOT NULL CHECK(complete IN (0,1)),expected_track_count INTEGER NOT NULL CHECK(expected_track_count>=0),
        provider_id_snapshot TEXT,resolution_reason TEXT CHECK(resolution_reason='missing_provider'),
        CHECK((provider_id_snapshot IS NULL)=(resolution_reason IS NOT NULL)))'''),
)
JOB = Job('album_occurrences', 79, 98, {'album_tracks': ('id',), 'album_fetch_state': ('album_id',)}, DESTINATIONS, copy, delete,
    dependencies=('albums','album_provider_ids','catalog_entries','youtube_items','unresolved_items','recordings'),
    batch_limit=32, refresh_highwater=False,
    projections={'album_tracks': ('id','album_id','entry_id','recording_id',clipped('legacy_track_key') + ' AS legacy_track_key',
                                 'position','disc_number','track_number','is_full',clipped('source') + ' AS source')})


def main(argv=None):
    return run(JOB, argv)


if __name__ == '__main__':
    raise SystemExit(main())
