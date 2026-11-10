"""Measure discovery custody on an isolated actual-library copy."""
from dataclasses import asdict, replace
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import time

from yt_playlist.core import collection_recovery, discovery_provenance as provenance
from yt_playlist.core.migrations import regular
from yt_playlist.core.preparation import discovery_artists as documents, discovery_subjects as rows


def fingerprint(conn, owner, key):
    digest = hashlib.sha256()
    count = 0
    for row in conn.execute(f'SELECT * FROM main.{owner} ORDER BY {key}'):
        digest.update(json.dumps(tuple(row),ensure_ascii=False).encode())
        count += 1
    return count,digest.hexdigest()


def check(path, report_path, *, populated=False):
    assert path.parent == Path('/tmp') and path.name.startswith('tuneconsole-')
    report = {'snapshot': str(path),'status': 'failed','python': sys.version,'sqlite': sqlite3.sqlite_version,
        'dataset': 'Isolated actual-library discovery owners' + (' with controlled extra visits and a large artist explanation.' if populated else '.'),
        'controls': 'No HTTP/provider/runtime workers, native changes or identity admission.','jobs': {}}
    conn = sqlite3.connect(path.as_uri() + '?mode=rw',uri=True,timeout=.1)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys=ON')
    owners = [(owner,','.join(keys)) for owner,keys in rows.KEYS.items()]
    owners += [('discovered_artists','artist'),('genre_classification_legacy','id'),('settings','key')]
    before = {owner:fingerprint(conn,owner,key) for owner,key in owners}
    history = list(map(tuple,conn.execute('SELECT * FROM schema_migrations ORDER BY version')))
    counts = {owner:conn.execute('SELECT count(*) FROM '+owner).fetchone()[0] for owner in ('recordings','artists','albums','catalog_entries','youtube_items')}
    name = 'artists_classified'
    artist_job = documents.JOBS[name]
    jobs = [('rows',rows.JOB),('classifications',rows.CLASSIFICATION_JOB),('artists',artist_job)]
    seeded = False
    saved = None
    try:
        if populated:
            saved = dict(conn.execute('SELECT * FROM main.discovered_artists ORDER BY artist LIMIT 1').fetchone())
            keys = [row[0] for row in conn.execute('SELECT identity_key FROM recordings ORDER BY id LIMIT 4096')]
            assert len(keys) == 4096
            source = saved['artist']
            with conn:
                conn.executemany('INSERT INTO main.discovery_visible_impressions VALUES (?,\'track\',?,1)',
                    [('@controlled-622-discovery-' + str(index),key) for index,key in enumerate(keys*2)])
                conn.execute('UPDATE main.discovered_artists SET because=? WHERE artist=?',
                    (json.dumps(keys*6),source))
            seeded = True
        for job_name,job in jobs:
            started = time.monotonic()
            job.install(conn)
            detail = report['jobs'][job_name] = {'installation_seconds':time.monotonic()-started,'batches':[]}
            reservations = []
            def trace(sql):
                if sql == 'BEGIN IMMEDIATE':
                    reservations.append(time.monotonic())
            conn.set_trace_callback(trace)
            changed = failed = False
            for index in range(3000):
                reservations.clear()
                started = time.monotonic()
                progress = job.step(conn,batch_size=1 if populated and job_name == 'artists' else 128)
                finished = time.monotonic()
                detail['batches'].append({'seconds':finished-started,'writer_seconds':finished-reservations[-1] if reservations else 0})
                cursor = json.loads(progress.cursor) if progress.cursor else None
                if populated and job_name == 'artists' and isinstance(cursor,dict) and json.loads(cursor['key']) == [source]:
                    if not changed:
                        started = time.monotonic()
                        with conn:
                            conn.execute('UPDATE main.discovered_artists SET because=because||char(10) WHERE artist=?',(source,))
                        detail['continuing_write_seconds'] = time.monotonic()-started
                        changed = True
                    elif not failed:
                        checkpoint = job.progress(conn)
                        def fail(*args):
                            documents.copy(name,*args)
                            raise OSError('controlled actual discovery copy failure')
                        try:
                            replace(job,copy=fail).step(conn,batch_size=1)
                        except OSError:
                            pass
                        else:
                            raise AssertionError('Controlled discovery failure did not run')
                        failure = job.progress(conn)
                        assert failure.status == 'failed' and failure.processed == checkpoint.processed
                        started = time.monotonic()
                        regular.migrate(conn)
                        detail['already_installed_ddl_admission_while_failed_seconds'] = time.monotonic()-started
                        assert job.progress(conn) == failure
                        job.step(conn,batch_size=1,retry=True)
                        failed = True
                if progress.status == 'prepared':
                    break
            conn.set_trace_callback(None)
            assert job.progress(conn).status == 'prepared'
            if populated and job_name == 'artists':
                assert changed and failed
            detail['progress'] = asdict(job.progress(conn))
        report['source_rows'] = {}
        for owner,target in rows.TARGETS.items():
            expected = {json.dumps([row[key] for key in rows.KEYS[owner]],ensure_ascii=False,separators=(',',':')):dict(row)
                for row in conn.execute('SELECT * FROM main.' + owner)}
            captured = list(conn.execute(f'SELECT * FROM {target} WHERE source_owner=?',(owner,)))
            assert len(captured) == len(expected)
            for row in captured:
                original = rows.original(conn,owner,row['source_key'],row['source_digest'])
                assert original == expected[row['source_key']]
                if owner in ('discovery_sources','discovery_artist_sources'):
                    fields = provenance.origin_fields(conn,original['source_token'] if owner == 'discovery_sources' else original['source'],
                        account_id=original['identity_id'] if owner == 'discovery_artist_sources' else None)
                    assert all(row[field] == value for field,value in fields.items())
            report['source_rows'][owner] = len(captured)
        archived = list(conn.execute('SELECT * FROM '+rows.CLASSIFICATIONS))
        assert len(archived) == conn.execute('SELECT count(*) FROM main.genre_classification_legacy').fetchone()[0]
        for row in archived:
            original = dict(conn.execute('SELECT * FROM main.genre_classification_legacy WHERE id=?',(row['id'],)).fetchone())
            assert rows.original(conn,'genre_classification_legacy',row['source_key'],row['source_digest']) == original
        report['source_rows']['genre_classification_legacy'] = len(archived)
        captured_artists = 0
        for row in conn.execute('SELECT * FROM main.discovered_artists ORDER BY artist'):
            original = dict(row)
            captured = documents.head(conn,name,row['artist'])
            assert documents.read_original(conn,name,captured) == original
            recovery = collection_recovery.read(captured['recovery_filename'],captured['recovery_digest'],captured['recovery_bytes'],
                kind='discovery-candidate',owner=captured['artist_id'] if captured['artist_id'] is not None else captured['source'])
            try:
                expected = {'because':json.loads(row['because'] or '[]') or [],'fits':json.loads(row['fits'] or '[]') or []}
            except (TypeError,ValueError):
                expected = {'because':[],'fits':[]}
            assert recovery == expected
            for field in ('offered_count','legacy_offered_count','first_shown','last_shown','legacy_first_shown','legacy_last_shown'):
                assert captured[field] == row[field]
            captured_artists += 1
        report['source_rows']['discovered_artists'] = captured_artists
        report.update(status='passed',originals_exact=True,origin_interpretation_exact=True,explanations_and_counters_exact=True)
    except BaseException as exc:
        report['error'] = repr(exc)
        raise
    finally:
        conn.set_trace_callback(None)
        if seeded:
            with conn:
                conn.execute("DELETE FROM main.discovery_visible_impressions WHERE view_id GLOB '@controlled-622-discovery-*'")
                conn.execute('UPDATE main.discovered_artists SET because=? WHERE artist=?',(saved['because'],source))
            for _,job in jobs:
                if job.progress(conn).status != 'failed':
                    for _ in range(3000):
                        if job.step(conn).status == 'prepared':
                            break
        report['source_owners_restored_exact'] = before == {owner:fingerprint(conn,owner,key) for owner,key in owners}
        report['history_exact'] = list(map(tuple,conn.execute('SELECT * FROM schema_migrations ORDER BY version'))) == history
        report['identity_counts_exact'] = counts == {owner:conn.execute('SELECT count(*) FROM '+owner).fetchone()[0] for owner in counts}
        report_path.write_text(json.dumps(report,indent=2))
        conn.close()
    assert report['source_owners_restored_exact'] and report['history_exact'] and report['identity_counts_exact']


if __name__ == '__main__':
    check(Path(sys.argv[1]).resolve(),Path(sys.argv[2]).resolve(),populated='--populated' in sys.argv[3:])
