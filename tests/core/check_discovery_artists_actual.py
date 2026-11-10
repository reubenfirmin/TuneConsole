"""Measure artist file publication on an isolated actual-library database."""
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import time

from actual_publication_timing import WriterTimings
from yt_playlist.core.migrations import regular
from yt_playlist.core.preparation import discovery_artists as preparation, discovery_artist_publication as publication
from yt_playlist.core.store import Store


def fingerprint(conn, table, key):
    digest = hashlib.sha256()
    count = 0
    for row in conn.execute('SELECT * FROM main.'+table+' ORDER BY '+key):
        digest.update(json.dumps(tuple(row), ensure_ascii=False, allow_nan=False).encode())
        count += 1
    return {'count': count, 'sha256': digest.hexdigest()}


def check(path, destination, *, refresh=False):
    assert path.is_relative_to(Path.cwd()/'.pytest_cache') and path.name.startswith('tuneconsole-')
    report = {'status': 'failed', 'database': str(path), 'python': sys.version, 'sqlite': sqlite3.sqlite_version,
              'dataset': 'Own isolated v93 actual-library snapshot; no controlled discovery artist rows.',
              'controls': 'No provider calls, HTTP requests, native changes or runtime workers.'}
    store, other = Store(path), Store(path)
    timings = WriterTimings()
    timed = timings.timed
    report['timing'] = 'Maximum writer window from BEGIN IMMEDIATE through commit/rollback completion.'
    try:
        store.init_schema(defer_metadata=True)
        other.init_schema(defer_metadata=True)
        conn = store.conn
        if refresh:
            with conn:
                conn.execute('UPDATE main.discovered_artists SET thumbnail=thumbnail WHERE artist=(SELECT min(artist) FROM main.discovered_artists)')
            report['refresh_control'] = 'Same-value write to one actual candidate before source fingerprints; withdraws the later canonical publication so this file family is checked independently.'
        owners = {'schema_migrations': 'version', 'settings': 'key', 'discovered_artists': 'artist',
            'discovery_artist_sources': 'artist_id,source', 'discovery_visible_impressions': 'view_id,kind,item_id',
            'discovery_sources': 'recording_id,source_token', 'artist_scans': 'artist', 'artists': 'id',
            'discovery_memberships': 'recording_id', 'album_discovery_memberships': 'album_id'}
        before = {table: fingerprint(conn, table, key) for table, key in owners.items()}
        source = list(map(dict, conn.execute('SELECT * FROM main.discovered_artists ORDER BY artist')))
        report['source_rows'] = len(source)
        expected = []
        for row in source:
            try:
                because, fits = json.loads(row['because'] or '[]'), json.loads(row['fits'] or '[]')
            except (TypeError, ValueError):
                because, fits = [], []
            display = conn.execute('SELECT name FROM main.artists WHERE id=?', (row['artist_id'],)).fetchone()
            if display is None:
                continue
            genre = conn.execute('SELECT name FROM main.genre_catalog WHERE key=?', (row['genre_key'],)).fetchone()
            expected.append({'artist': display[0], 'artist_id': row['artist_id'], 'score': row['score'],
                'because': because or [], 'fits': fits or [], 'thumbnail': row['thumbnail'],
                'genre': genre[0] if genre else row['unresolved_genre'],
                **{key: row[key] for key in ('found_at', 'last_shown', 'first_shown', 'offered_count')}})
        expected.sort(key=lambda row: row['artist_id'])
        timings.watch(conn)
        owner = publication.owner(conn)
        owner.worker.install(conn)
        report['preparation_batches'] = []
        for _ in range(5000):
            progress, detail = timed(lambda: owner.worker.step(conn, batch_size=8))
            report['preparation_batches'].append(detail)
            if progress.status == 'prepared':
                break
        else:
            raise AssertionError('Actual artist preparation did not converge')
        report['map_batches'] = []
        for _ in range(5000):
            stage, detail = timed(lambda: owner.step(conn, batch_size=8))
            report['map_batches'].append(detail)
            if stage.complete:
                break
        else:
            raise AssertionError('Actual artist map did not converge')
        _, report['publication'] = timed(lambda: owner.publish(conn, stage=stage.id))
        for row in source:
            header = owner.capture(conn, stage.id, row['artist'])
            assert preparation.read_original(conn, owner.name, header) == row
        with other.read_view() as reader:
            def deny_old(action, table, column, *_):
                return sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_READ and table == 'discovered_artists' else sqlite3.SQLITE_OK
            reader.conn.set_authorizer(deny_old)
            try:
                actual = sorted(reader.discovery.get_discovered_artists(), key=lambda row: row['artist_id'])
                assert actual == expected
                rows = reader.discovery.discovery_artist_view(1000, 7)
                assert {row['artist_id'] for row in rows} == {row['artist_id'] for row in expected}
                ids = [row['artist_id'] for row in expected[:40]]
                assert set(reader.discovery.present_home_tiles([], ids)[1]) == set(ids)
                reader.discovery.artist_images_due(ids, time.time())
            finally:
                reader.conn.set_authorizer(None)
        assert sorted(other.discovery.get_discovered_artists(), key=lambda row: row['artist_id']) == expected
        header = conn.execute('SELECT * FROM '+owner.header+' ORDER BY source LIMIT 1').fetchone()
        assert header and Path(header['recovery_filename']).is_relative_to(path.with_name(path.name+'.preparation'))
        Path(header['recovery_filename']).unlink()
        _, report['admission_with_missing_file'] = timed(lambda: regular.migrate(conn))
        _, report['explicit_file_repair'] = timed(lambda: publication.repair(conn, header['source']))
        owner.verify(conn, stage.id)
        # Exercise an actual compatible writer without changing captured values.
        def write_same_value():
            with conn.atomic():
                conn.execute('UPDATE main.discovered_artists SET thumbnail=thumbnail WHERE artist=?', (header['source'],))
        _, report['continuing_writer'] = timed(write_same_value)
        assert owner.selected(conn) is None
        assert sorted(other.discovery.get_discovered_artists(), key=lambda row: row['artist_id']) == expected
        assert before == {table: fingerprint(conn, table, key) for table, key in owners.items()}
        report.update(status='passed', protected_sources=before, originals_exact=True, legacy_reads_denied=True,
                      installed_identities_counters_genres_exact=True)
    finally:
        timings.close()
        store.conn.close()
        other.conn.close()
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(json.dumps(report, indent=2)+'\n')


if __name__ == '__main__':
    check(Path(sys.argv[1]).resolve(), Path(sys.argv[2]).resolve(), refresh='--refresh' in sys.argv[3:])
