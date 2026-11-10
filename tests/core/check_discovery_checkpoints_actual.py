"""Explicit checkpoint cutover audit of an existing disposable actual library."""
import json
from pathlib import Path
import sqlite3
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from actual_publication_timing import WriterTimings
from yt_playlist.core.db_diagnostics import DiagnosticConnection
from tests.core.check_home_publication_actual import fingerprint
from yt_playlist.core import discovery_checkpoints, provider_frontiers
from yt_playlist.core.migrations import regular
from yt_playlist.core.preparation import discovery_checkpoints as preparation
from yt_playlist.core.preparation.discovery_checkpoint_publication import OWNERS
from yt_playlist.core.store import Store


def check(database, destination, *, refresh=False):
    cache = Path.cwd()/'.pytest_cache'
    assert database.is_relative_to(cache) and database.name.startswith('tuneconsole-') and database.exists()
    assert destination.is_relative_to(cache)
    destination.parent.mkdir(parents=True, exist_ok=True)
    report = {'status':'failed', 'database':str(database), 'python':sys.version, 'sqlite':sqlite3.sqlite_version,
        'dataset':'Existing isolated genuine v93 actual-library snapshot; no controlled checkpoint rows or native access.',
        'limits':'Checkpoint custody and runtime read cutover only; provider refresh has no source rows in this snapshot.',
        'families':{}}
    store = Store(database)
    conn = sqlite3.connect(database, timeout=.1, factory=DiagnosticConnection)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys=ON')
    protected = ('schema_migrations','settings','play_events','listening_observations','discovery_sources',
        'discovery_memberships','discovered_artists','album_provider_ids','artists','enrichment_provider_config')
    before = {owner:fingerprint(conn, owner) for owner in protected}
    original = {name:dict(conn.execute('SELECT key,value FROM main.settings WHERE '+job._filter('settings')))
                for name, job in preparation.JOBS.items()}
    timings = WriterTimings(field='writer_seconds')
    timed = timings.timed
    report['timing'] = 'Maximum writer window from BEGIN IMMEDIATE through commit/rollback completion.'
    try:
        store.init_schema(defer_metadata=True)
        if refresh:
            conn.execute('BEGIN IMMEDIATE')
            try:
                for job in preparation.JOBS.values():
                    conn.execute('UPDATE main.settings SET value=value WHERE key=(SELECT min(key) '
                        'FROM main.settings WHERE '+job._filter('settings')+')')
                conn.commit()
            finally:
                conn.rollback()
            report['refresh_control'] = ('Same-value write to one existing source per nonempty family; '
                'source fingerprints unchanged. Forces new publication maps after bounded reconciliation; '
                'empty provider-refresh has no fabricated source.')
        timings.watch(conn)
        for name, owner in OWNERS.items():
            detail = report['families'][name] = {'source_rows':len(original[name]), 'preparation':[], 'staging':[]}
            owner.job.install(conn)
            for _ in range(10000):
                progress, duration = timed(lambda:owner.job.step(conn, batch_size=16))
                detail['preparation'].append(duration)
                if progress.status == 'prepared':
                    break
            else:
                raise AssertionError('Actual checkpoint preparation did not complete')
            for _ in range(10000):
                stage, duration = timed(lambda:owner.step(conn, batch_size=16))
                detail['staging'].append(duration)
                if stage.complete:
                    break
            else:
                raise AssertionError('Actual checkpoint staging did not complete')
            _, detail['selection'] = timed(lambda:owner.publish(conn, stage=stage.id))
            detail['selected'] = owner.selected(conn)
            assert detail['selected'] == stage.id
            retained = dict(conn.execute('SELECT source,raw_value FROM '+owner.table('rows')+' WHERE stage=?', (stage.id,)))
            assert retained == original[name]
        with store.read_view() as reader:
            def mask(operation, table, column, *unused):
                return sqlite3.SQLITE_IGNORE if operation == sqlite3.SQLITE_READ and table == 'settings' and column == 'value' else sqlite3.SQLITE_OK
            reader.conn.set_authorizer(mask)
            prefix = preparation.PREFIXES['fetch-history']
            for source, raw in original['fetch-history'].items():
                assert raw == '1' and discovery_checkpoints.fetched(reader.conn, source[len(prefix):])
            assert not discovery_checkpoints.fetched(reader.conn, 'isolated-absent-checkpoint')
            for source, raw in original['provider-refresh'].items():
                expected = json.loads(raw)
                expected.setdefault('revision', 1)
                assert provider_frontiers.read(reader.conn, source[len(provider_frontiers.PREFIX):]) == expected
            assert provider_frontiers.read(reader.conn, 'isolated-absent-provider') is None
            image_prefix = preparation.PREFIXES['artist-images']
            ids = [row[0] for row in conn.execute('SELECT artist_id FROM main.discovered_artists ORDER BY artist_id LIMIT 40')]
            ids = list(dict.fromkeys([int(source[len(image_prefix):]) for source in original['artist-images']] + ids))[:40]
            cohort = [tuple(row) for row in conn.execute('SELECT d.artist_id,a.name,d.thumbnail FROM main.discovered_artists d '
                'JOIN main.artists a ON a.id=d.artist_id WHERE d.artist_id IN ('+','.join('?' for _ in ids)+') ORDER BY d.artist_id', ids)] if ids else []
            clocks = [float(raw) for raw in original['artist-images'].values()]
            for now in [time.time(), *sorted(clocks)[:3]]:
                expected = [{'artist_id':identity,'artist':name} for identity, name, thumbnail in cohort
                    if not thumbnail and float(original['artist-images'].get(image_prefix+str(identity), '0')) <= now]
                assert reader.discovery.artist_images_due(ids, now) == expected
            report['image_cohort'] = len(cohort)
            reader.conn.set_authorizer(None)
        _, report['regular_admission_selected'] = timed(lambda:regular.migrate(conn))
        report.update(status='passed', exact_originals=True, masked_source_reads_passed=True)
    except BaseException as error:
        report['error'] = repr(error)
        raise
    finally:
        timings.close()
        report['sources_exact'] = before == {owner:fingerprint(conn, owner) for owner in protected}
        store.conn.close()
        conn.close()
        destination.write_text(json.dumps(report, indent=2)+'\n')
    assert report['sources_exact']
    return report


if __name__ == '__main__':
    result = check(Path(sys.argv[1]).resolve(), Path(sys.argv[2]).resolve(), refresh='--refresh' in sys.argv[3:])
    print(json.dumps({key:result[key] for key in ('status','exact_originals','masked_source_reads_passed','sources_exact')}))
