"""Measure incremental calendar indexes with actual unresolved event scopes.

Use a read-only isolated v93 source and a separately installed genuine v105
target. Actual coarse observations have no unresolved calendar-day rows, so the
long foreign-calendar portion is explicitly a controlled fixture.
"""
from dataclasses import asdict, replace
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import time

from tests.core.actual_publication_timing import WriterTimings
from yt_playlist.core import listening, listening_calendar
from yt_playlist.core.migrations import registry, regular, runner
from yt_playlist.core.preparation import calendar_indexes as indexes
from yt_playlist.core.store import Store


def fingerprint(conn,owner,order='id'):
    digest = hashlib.sha256()
    count = 0
    for row in conn.execute('SELECT * FROM main.'+owner+' ORDER BY '+order):
        digest.update(json.dumps(tuple(row),ensure_ascii=False).encode())
        count += 1
    return count,digest.hexdigest()


def check(source_path,target_path,report_path):
    assert (source_path.parent==Path('/tmp') or source_path.is_relative_to(Path('.pytest_cache').resolve()))
    assert (target_path.parent==Path('/tmp') or target_path.is_relative_to(Path('.pytest_cache').resolve()))
    assert target_path.name.startswith('tuneconsole-')
    assert source_path != target_path and not target_path.exists()
    target_path.parent.mkdir(parents=True,exist_ok=True)
    source = sqlite3.connect(source_path.as_uri()+'?mode=ro',uri=True)
    source.row_factory = sqlite3.Row
    source_before = fingerprint(source,'play_events')
    actual = list(source.execute('SELECT * FROM main.play_events WHERE recording_id IS NULL ORDER BY id'))
    assert actual
    report = {'status':'failed','source':str(source_path),'target':str(target_path),
        'python':sys.version,'sqlite':sqlite3.sqlite_version,
        'dataset':'Actual unresolved events and accounts/calendars on a separately installed genuine v105 fixture; 8192 controlled events and 8192 controlled foreign-day observations use actual unresolved keys.',
        'limits':'No full actual-library conversion. Actual v93 source has zero unresolved calendar-day observations. Source read-only, no native changes or HTTP/provider/runtime workers.',
        'actual_unresolved_events':len(actual),'actual_unresolved_calendar_observations':source.execute(
            "SELECT count(*) FROM main.listening_observations WHERE recording_id IS NULL AND precision='calendar_day'").fetchone()[0],
        'batches':[]}
    store = Store(target_path)
    conn = store.conn
    timer = WriterTimings()
    try:
        runner.migrate(conn,registry=registry.MIGRATIONS[:105])
        store.init_schema(defer_metadata=True)
        conn.execute('PRAGMA journal_mode=WAL')
        with conn:
            conn.executemany('INSERT INTO main.accounts VALUES (?,?,?,?,?,?,?)',map(tuple,source.execute('SELECT * FROM main.identities ORDER BY id')))
            conn.executemany('INSERT INTO main.listening_calendars VALUES (?,?,?)',map(tuple,source.execute('SELECT * FROM main.listening_calendars ORDER BY identity_id')))
            for row in actual:
                conn.execute('INSERT INTO main.play_events(id,account_id,raw_identity_key,recording_id,unresolved_subject_key,video_id,played_at,playlist_ytm_id,membership_ref_id,like_status,is_generated) '
                    'VALUES (?,?,?,NULL,?,?,?,?,NULL,?,?)',
                    (row['id'],row['identity_id'],row['raw_identity_key'],row['identity_key'],row['video_id'],row['played_at'],row['playlist_ytm_id'],row['like_status'],row['is_generated']))
                conn.executemany('INSERT INTO main.play_event_calendar VALUES (?,?,?)',map(tuple,source.execute('SELECT * FROM main.play_event_calendar WHERE event_id=?',(row['id'],))))
                conn.executemany('INSERT INTO main.play_event_sources VALUES (?,?)',map(tuple,source.execute('SELECT * FROM main.play_event_sources WHERE event_id=?',(row['id'],))))
            for index in range(8192):
                row = actual[index % len(actual)]
                at = row['played_at'] + (index // len(actual))*86400
                event = listening.append_event(conn,row['identity_id'],row['identity_key'],at,video_id=row['video_id'])
                listening.project_event(conn,event,row['identity_id'],at,source='controlled')
                listening.observe_day(conn,row['identity_id'],row['identity_key'],listening_calendar.day_at(at,'America/New_York'),
                    'controlled-'+str(index),'America/New_York',at,reconcile=False)
        protected = ('play_events','listening_observations','play_event_calendar','play_event_sources')
        before = {owner:fingerprint(conn,owner,'event_id,source' if owner == 'play_event_sources' else 'event_id' if owner == 'play_event_calendar' else 'id') for owner in protected}
        history = list(map(tuple,conn.execute('SELECT * FROM schema_migrations ORDER BY version')))
        scopes = {}
        for row in conn.execute('SELECT account_id,unresolved_subject_key,source_day,timezone FROM main.listening_observations ORDER BY id LIMIT 32'):
            scopes.setdefault(row['account_id'],set()).add((row['unresolved_subject_key'],row['source_day'],row['timezone']))
        expected = {account:sorted(map(tuple,listening_calendar._foreign_rows(conn,account,affected,None))) for account,affected in scopes.items()}
        job = indexes.job(conn)
        started = time.monotonic()
        job.install(conn)
        report['installation_seconds'] = time.monotonic()-started
        assert conn.execute('SELECT count(*) FROM '+indexes.EVENTS).fetchone()[0] == 0
        assert conn.execute('SELECT count(*) FROM '+indexes.OBSERVATIONS).fetchone()[0] == 0
        reservations = []
        def trace(sql):
            if sql == 'BEGIN IMMEDIATE':
                reservations.append(time.monotonic())
        conn.set_trace_callback(trace)
        exercised = False
        for index in range(1000):
            reservations.clear()
            started = time.monotonic()
            progress = job.step(conn,batch_size=128)
            finished = time.monotonic()
            report['batches'].append({'seconds':finished-started,'writer_seconds':finished-reservations[-1] if reservations else 0})
            if not exercised:
                def fail(*args):
                    job.copy(*args)
                    raise OSError('controlled calendar lookup failure')
                try:
                    replace(job,copy=fail).step(conn)
                except OSError:
                    pass
                else:
                    raise AssertionError('Controlled calendar failure did not run')
                failed = job.progress(conn)
                assert failed.status == 'failed' and failed.processed == progress.processed
                started = time.monotonic()
                regular.migrate(conn)
                report['installed_regular_ddl_admission_while_failed_seconds'] = time.monotonic()-started
                assert job.progress(conn) == failed
                job.step(conn,retry=True)
                exercised = True
            if progress.status == 'prepared':
                break
        conn.set_trace_callback(None)
        timer.watch(conn)
        _,report['selection'] = timer.timed(lambda:indexes.publish(conn,revision=progress.input_revision))
        report['publication_seconds'] = report['selection']['seconds']
        report['progress'] = asdict(job.progress(conn))
        for owner,target in indexes.OWNERS.items():
            fields = ','.join(indexes.FIELDS[owner])
            assert list(map(tuple,conn.execute('SELECT * FROM '+target+' ORDER BY id'))) == list(map(tuple,
                conn.execute('SELECT '+fields+' FROM main.'+owner+' WHERE '+indexes.PREDICATES[owner].format(prefix='')+' ORDER BY id')))
        report['lookup_seconds'] = {}
        for account,affected in scopes.items():
            started = time.monotonic()
            assert sorted(map(tuple,listening_calendar._foreign_rows(conn,account,affected,None))) == expected[account]
            report['lookup_seconds'][str(account)] = time.monotonic()-started
        assert before == {owner:fingerprint(conn,owner,'event_id,source' if owner == 'play_event_sources' else 'event_id' if owner == 'play_event_calendar' else 'id') for owner in protected}
        assert list(map(tuple,conn.execute('SELECT * FROM schema_migrations ORDER BY version'))) == history
        row = actual[0]
        started = time.monotonic()
        with conn:
            event = listening.append_event(conn,row['identity_id'],'controlled-later',row['played_at']+123)
            listening.project_event(conn,event,row['identity_id'],row['played_at']+123,source='controlled')
        report['continuing_write_seconds'] = time.monotonic()-started
        assert conn.execute('SELECT unresolved_subject_key FROM '+indexes.EVENTS+' WHERE id=?',(event,)).fetchone()[0] == 'controlled-later'
        assert indexes.selected(conn)
        report.update(status='passed',controlled_extra_events=8192,controlled_observations=8192,source_and_history_exact_before_controlled_write=True,
            index_rows={target:conn.execute('SELECT count(*) FROM '+target).fetchone()[0] for target in indexes.OWNERS.values()})
    except BaseException as exc:
        report['error'] = repr(exc)
        raise
    finally:
        timer.close()
        conn.set_trace_callback(None)
        report['read_only_source_exact'] = source_before == fingerprint(source,'play_events')
        report_path.write_text(json.dumps(report,indent=2))
        conn.close()
        source.close()


if __name__ == '__main__':
    check(*(Path(value).resolve() for value in sys.argv[1:]))
