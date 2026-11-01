"""Check canonical discovery selection on an isolated actual-library snapshot."""
from collections import defaultdict
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import time

from yt_playlist.core import genre_identity
from yt_playlist.core.db_diagnostics import DiagnosticConnection
from yt_playlist.core.migrations import regular
from yt_playlist.core.preparation import discovery_publication as publication
from yt_playlist.core.store import Store


def fingerprint(conn, table, key):
    digest = hashlib.sha256()
    count = 0
    for row in conn.execute('SELECT * FROM main.'+table+' ORDER BY '+key):
        digest.update(json.dumps(tuple(row), ensure_ascii=False, allow_nan=False).encode())
        count += 1
    return {'count': count, 'sha256': digest.hexdigest()}


def expected(conn):
    """Fold installed facts independently of preparation and publication code."""
    redirects = dict(conn.execute('SELECT retired_id,artist_id FROM main.artist_redirects'))
    artist_ids = {row[0] for row in conn.execute('SELECT id FROM main.artists')}
    artist_aliases, provider_artists, recordings = defaultdict(set), defaultdict(set), defaultdict(set)
    for alias, aid in conn.execute('SELECT alias,artist_id FROM main.artist_aliases'):
        artist_aliases[alias].add(redirects.get(aid, aid))
    for provider, aid in conn.execute("SELECT provider_id,artist_id FROM main.artist_provider_ids WHERE provider='youtube'"):
        provider_artists[provider].add(redirects.get(aid, aid))
    recording_redirects = dict(conn.execute('SELECT retired_id,recording_id FROM main.recording_redirects'))
    for token, rid in conn.execute('SELECT alias,recording_id FROM main.recording_aliases UNION SELECT identity_key,id FROM main.recordings'):
        recordings[token].add(recording_redirects.get(rid, rid))
    album_ids = dict(conn.execute("SELECT provider_id,album_id FROM main.album_provider_ids WHERE provider='youtube'"))
    uploads = {row[0] for row in conn.execute('SELECT video_id FROM main.youtube_items')}
    accounts = {row[0] for row in conn.execute('SELECT id FROM main.identities')}
    one = lambda values: next(iter(values)) if len(values) == 1 else None
    def artist(value):
        if value.isascii() and value.isdigit() or value.startswith('@artist:'):
            try:
                aid = int(value.removeprefix('@artist:'))
            except ValueError:
                return None
            return redirects.get(aid, aid) if aid in artist_ids else None
        return one(artist_aliases[value])
    visits, unresolved = {}, []
    for view, kind, token, clock in conn.execute('SELECT * FROM main.discovery_visible_impressions'):
        subject = (artist(token) if kind == 'artist' else one(recordings[token]) if kind == 'track'
                   else album_ids.get(token) if kind == 'album' else None)
        if subject is None:
            reason = 'unsupported_kind' if kind not in ('artist', 'track', 'album') else (
                'ambiguous_owner' if kind == 'track' and len(recordings[token]) > 1 else 'missing_owner')
            unresolved.append((view, kind, token, clock, reason))
            continue
        key = (view, kind, subject)
        visits[key] = min(visits.get(key, clock), clock)
    counts = defaultdict(list)
    for (_, kind, subject), clock in visits.items():
        counts[kind, subject].append(clock)
    legacy = defaultdict(lambda: [0, [], []])
    artist_rows = list(map(dict, conn.execute('SELECT rowid source_ordinal,* FROM main.discovered_artists ORDER BY rowid')))
    for kind, field, rows in (
            ('artist', 'artist_id', artist_rows),
            ('track', 'recording_id', list(map(dict, conn.execute('SELECT * FROM main.discovery_memberships')))),
            ('album', 'album_id', list(map(dict, conn.execute('SELECT * FROM main.album_discovery_memberships'))))):
        for row in rows:
            subject = redirects.get(row[field], row[field]) if kind == 'artist' else row[field]
            if subject is None:
                continue
            residue = max(0, row['offered_count']-len(counts[kind, subject]))
            retained = legacy[kind, subject]
            retained[0] += row.get('legacy_offered_count', 0)+residue
            for position, clock_field in ((1, 'first_shown'), (2, 'last_shown')):
                retained[position] += [value for value in (row.get('legacy_'+clock_field), row[clock_field] if residue else None) if value is not None]
    legacy = {key: (value[0], min(value[1]) if value[1] else None, max(value[2]) if value[2] else None)
              for key, value in legacy.items() if value[0] or value[1] or value[2]}
    def origin(token, account=None):
        kind, separator, value = token.partition(':')
        # Identities are tuples of retained subjects, never captured display text.
        if token in ('discovery', 'spotify_model'):
            return (token, account if token == 'spotify_model' else None)
        if kind == 'spotify_export' and separator:
            try:
                identifier = int(value)
            except ValueError:
                identifier = None
            if identifier in accounts:
                assert account is None or account == identifier
                return (kind, identifier, None)
        if kind in ('artist', 'artist_start') and separator:
            identifier = one(provider_artists[value])
            if identifier is not None:
                return (kind, identifier)
        if kind == 'radio' and separator and value in uploads:
            return (kind, value)
        if kind == 'genre' and separator:
            # Genre identity is shared business policy; no publication resolver
            # participates in subject, visit, source or counter expectations.
            key, _ = genre_identity.resolve(conn, value)
            if key is not None:
                return (kind, key)
        if not separator and token in album_ids:
            return ('album', album_ids[token])
        return ('legacy_unknown', account, token)
    sources = {}
    raw_sources = list(map(dict, conn.execute('SELECT * FROM main.discovery_sources')))
    for row in raw_sources:
        key = (row['recording_id'], origin(row['source_token']), row['input_video_id'])
        first, last = sources.setdefault(key, ([], []))
        first.extend([row['found_at']] if row['found_at'] is not None else [])
        last.extend([row['last_found_at']] if row['last_found_at'] is not None else [])
    sources = {key: (min(value[0]) if value[0] else None, max(value[1]) if value[1] else None) for key, value in sources.items()}
    contributions = {}
    for row in map(dict, conn.execute('SELECT * FROM main.discovery_artist_sources')):
        aid = redirects.get(row['artist_id'], row['artist_id'])
        key = (aid, origin(row['source'], row['identity_id']))
        contributions[key] = max(contributions.get(key, row['score']), row['score'])
    return dict(visits=visits, unresolved=unresolved, counts=counts, legacy=legacy, sources=sources,
                contributions=contributions, artist_rows=artist_rows, redirects=redirects)


def check(path, destination):
    assert path.is_relative_to(Path.cwd()/'.pytest_cache') and path.name.startswith('tuneconsole-')
    report = {'status': 'failed', 'database': str(path), 'python': sys.version, 'sqlite': sqlite3.sqlite_version,
              'dataset': 'Own isolated v93 actual-library snapshot; no controlled discovery rows.',
              'controls': 'No providers, HTTP, runtime workers or native changes. Only genre label resolution reuses business policy.'}
    store, other = Store(path), Store(path)
    try:
        store.init_schema(defer_metadata=True)
        other.init_schema(defer_metadata=True)
        conn = store.conn
        protected = {'schema_migrations': 'version', 'settings': 'key', 'discovered_artists': 'artist',
            'discovery_artist_sources': 'artist_id,source', 'discovery_visible_impressions': 'view_id,kind,item_id',
            'discovery_sources': 'recording_id,source_token', 'artist_scans': 'artist', 'artists': 'id',
            'discovery_memberships': 'recording_id', 'album_discovery_memberships': 'album_id', 'genre_classification_legacy': 'id'}
        before = {table: fingerprint(conn, table, key) for table, key in protected.items()}
        wanted = expected(conn)
        windows, started = [], None
        finish = DiagnosticConnection._finish
        def trace(sql):
            nonlocal started
            if sql == 'BEGIN IMMEDIATE':
                started = time.monotonic()
        def measured_finish(connection, sql, target, *args):
            nonlocal started
            try:
                return finish(connection, sql, target, *args)
            finally:
                if connection is conn and started is not None and sql in ('COMMIT', 'ROLLBACK'):
                    windows.append(time.monotonic()-started)
                    started = None
        DiagnosticConnection._finish = measured_finish
        conn.set_trace_callback(trace)
        def timed(callback):
            windows.clear()
            beginning = time.monotonic()
            result = callback()
            return result, {'seconds': time.monotonic()-beginning, 'max_writer_seconds': max(windows, default=0)}
        def converge(step, done, limit=10000):
            details = []
            for _ in range(limit):
                result, detail = timed(step)
                details.append(detail)
                if done(result):
                    return result, details
            raise AssertionError('Actual discovery preparation did not converge')
        owner = publication.owner(conn)
        artifact = owner.artist
        artifact.worker.install(conn)
        _, report['artist_preparation'] = converge(lambda: artifact.worker.step(conn, batch_size=8), lambda state: state.status == 'prepared')
        state, report['artist_staging'] = converge(lambda: artifact.step(conn, batch_size=8), lambda state: state.complete)
        _, report['artist_selection'] = timed(lambda: artifact.publish(conn, stage=state.id))
        for job in owner.jobs:
            job.install(conn)
            _, report[job.name] = converge(lambda: job.step(conn, batch_size=16), lambda state: state.status == 'prepared')
        state, report['staging'] = converge(lambda: owner.step(conn, batch_size=16), lambda state: state.complete)
        _, report['selection'] = timed(lambda: owner.publish(conn, stage=state.id))
        report['retained_inputs'] = {kind: conn.execute('SELECT count(*) FROM '+owner.table('input_'+kind)+' WHERE stage=?', (state.id,)).fetchone()[0] for kind in owner.kinds}
        with other.read_view() as reader:
            owners = publication.read_owners(reader.conn)
            assert owners.revision == state.id
            forbidden = set(protected)-{'artists', 'settings', 'schema_migrations', 'genre_classification_legacy'}
            def deny_old(action, table, column, *_):
                return sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_READ and table in forbidden else sqlite3.SQLITE_OK
            reader.conn.set_authorizer(deny_old)
            try:
                report['reads'] = {}
                def read(name):
                    result, detail = timed(lambda: list(map(dict, reader.conn.execute('SELECT * FROM '+owners.relation(name)))))
                    report['reads'][name] = {**detail, 'rows': len(result)}
                    return result
                visits = read('discovery_impressions')
                assert {(row['view_id'], kind, row[field]): row['shown_at'] for row in visits
                    for kind, field in (('track', 'recording_id'), ('album', 'album_id'), ('artist', 'artist_id')) if row[field] is not None} == wanted['visits']
                assert sorted(((row['view_id'], row['original_kind'], row['original_key'], row['shown_at'], row['reason'])
                    for row in read('unresolved_discovery_impressions')), key=repr) == sorted(wanted['unresolved'], key=repr)
                legacy = read('discovery_legacy_offers')
                assert {(row['kind'], row['subject_id']): (row['offered_count'], row['first_shown'], row['last_shown']) for row in legacy} == wanted['legacy']
                origins = {row['id']: row for row in read('discovery_origins')}
                def identity(row):
                    kind = row['kind']
                    field = {'album': 'album_id', 'radio': 'seed_video_id', 'genre': 'genre_key', 'artist': 'artist_id', 'artist_start': 'artist_id'}
                    if kind in field:
                        return kind, row[field[kind]]
                    if kind == 'spotify_export':
                        return kind, row['account_id'], row['import_key']
                    if kind == 'legacy_unknown':
                        return kind, row['account_id'], row['legacy_source_key']
                    return kind, row['account_id']
                assert {(row['recording_id'], identity(origins[row['origin_id']]), row['input_video_id']):
                    (row['first_found_at'], row['last_found_at']) for row in read('discovery_sources')} == wanted['sources']
                assert {(row['artist_id'], identity(origins[row['origin_id']])): row['score']
                    for row in read('discovery_artist_sources')} == wanted['contributions']
                for name, kind, field in (('discovered_artists', 'artist', 'artist_id'), ('discovery_memberships', 'track', 'recording_id'),
                        ('album_discovery_memberships', 'album', 'album_id')):
                    for row in read(name):
                        clocks = wanted['counts'][kind, row[field]]
                        assert (row['offered_count'], row['first_shown'], row['last_shown']) == (
                            len(clocks), min(clocks) if clocks else None, max(clocks) if clocks else None)
                for name, callback in (
                        ('artist_display', reader.discovery.get_discovered_artists), ('album_display', reader.discovery.get_discovered_albums),
                        ('track_display', reader.discovery.get_discovered_tracks), ('starter_artists', reader.discovery.starter_artists),
                        ('starter_tracks', reader.discovery.starter_tracks), ('scan_queue', lambda: reader.discovery.artists_due_for_scan(time.time()))):
                    rows, detail = timed(callback)
                    report['reads'][name] = {**detail, 'rows': len(rows)}
            finally:
                reader.conn.set_authorizer(None)
        # Check retained originals, including classifications that do not enter
        # display. SQL readers above were denied every mutable legacy subject.
        for kind in owner.kinds:
            if kind == 'artists':
                continue
            for row in conn.execute('SELECT source_owner,source_key,source_snapshot FROM '+owner.table('input_'+kind)+' WHERE stage=?', (state.id,)):
                job = owner.jobs[-1] if kind == 'classifications' else owner.jobs[0]
                assert json.loads(row['source_snapshot']) == dict(job._read(conn, row['source_owner'], row['source_key']))
        _, report['regular_admission'] = timed(lambda: regular.migrate(conn))
        def continuing_write():
            with conn.atomic():
                conn.execute('UPDATE main.discovery_sources SET last_found_at=last_found_at WHERE (recording_id,source_token)='
                    '(SELECT recording_id,source_token FROM main.discovery_sources LIMIT 1)')
        _, report['continuing_writer'] = timed(continuing_write)
        assert owner.selected(conn) is None
        assert before == {table: fingerprint(conn, table, key) for table, key in protected.items()}
        # Leave the disposable copy selected for the subsequent startup matrix.
        for job in owner.jobs:
            converge(lambda: job.step(conn, batch_size=16), lambda state: state.status == 'prepared')
        newer, report['reconciliation_staging'] = converge(lambda: owner.step(conn, batch_size=16), lambda state: state.complete)
        _, report['reconciliation_selection'] = timed(lambda: owner.publish(conn, stage=newer.id))
        assert newer.id != state.id
        report.update(status='passed', originals_exact=True, legacy_reads_denied=True,
                      independent_visits_sources_counters=True, selected=owner.selected(conn), protected_sources=before)
    finally:
        if 'finish' in locals():
            DiagnosticConnection._finish = finish
        store.conn.close()
        other.conn.close()
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(json.dumps(report, indent=2)+'\n')


if __name__ == '__main__':
    check(Path(sys.argv[1]).resolve(), Path(sys.argv[2]).resolve())
