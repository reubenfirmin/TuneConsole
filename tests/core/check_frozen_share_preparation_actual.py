"""Exercise the independent fence with actual captured choices on a future layout.

The source is an isolated actual-library copy, opened read-only. The target is
a separately installed genuine v104 fixture. This does not claim conversion or
runtime acceptance of the complete actual library.
"""
from dataclasses import asdict, replace
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import time

from yt_playlist.core import albums, album_tracklists
from yt_playlist.core.migrations import registry, regular, runner
from yt_playlist.core.preparation import frozen_shares as audit
from yt_playlist.core.store import Store


def fingerprint(conn,owner,order):
    digest = hashlib.sha256()
    count = 0
    for row in conn.execute('SELECT * FROM main.'+owner+' ORDER BY '+order):
        digest.update(json.dumps(tuple(row),ensure_ascii=False).encode())
        count += 1
    return count,digest.hexdigest()


def check(source_path,target_path,report_path):
    assert source_path.parent == target_path.parent == Path('/tmp') and target_path.name.startswith('tuneconsole-')
    assert source_path != target_path and not target_path.exists()
    report = {'status':'failed','source':str(source_path),'target':str(target_path),
        'dataset':'Actual saved choices, title and order reproduced on a genuine empty-installed v104 controlled fixture; catalog entries remain absent and choices unresolved.',
        'limits':'Not a full actual-library conversion or runtime cutover. Source read-only; no native changes, providers or HTTP/runtime workers.',
        'python':sys.version,'sqlite':sqlite3.sqlite_version,'batches':[]}
    source = sqlite3.connect(source_path.as_uri()+'?mode=ro',uri=True)
    source.row_factory = sqlite3.Row
    originals = list(source.execute('SELECT browse_id,title,video_ids_json FROM main.album_share_snapshots ORDER BY browse_id'))
    source_before = fingerprint(source,'album_share_snapshots','browse_id')
    store = Store(target_path)
    conn = store.conn
    try:
        runner.migrate(conn,registry=registry.MIGRATIONS[:104])
        conn.execute('PRAGMA journal_mode=WAL')
        captures = []
        with conn:
            for row in originals:
                album = albums.observe(conn,row['browse_id'],title=row['title'],artist='',rank=1)
                choices = json.loads(row['video_ids_json'])
                identity = album_tracklists.freeze(conn,album,row['browse_id'],row['title'],choices,now=None,legacy=True)
                captures.append((identity,choices))
            album = albums.observe(conn,'controlled-long',title='Long controlled capture',artist='',rank=1)
            observed = [video for _,choices in captures for video in choices]
            assert observed
            long_choices = [observed[index % len(observed)] for index in range(8192)]
            long_id = album_tracklists.freeze(conn,album,'controlled-long','Controlled repeated actual choices',long_choices,now=None)
            captures.append((long_id,long_choices))
        before = {owner:fingerprint(conn,owner,','.join(keys)) for owner,keys in audit.JOB.keys.items()}
        history = list(map(tuple,conn.execute('SELECT * FROM schema_migrations ORDER BY version')))
        started = time.monotonic()
        audit.JOB.install(conn)
        report['installation_seconds'] = time.monotonic()-started
        assert audit.JOB.progress(conn).processed == 0
        with conn:
            try:
                conn.execute('INSERT INTO main.album_share_items SELECT snapshot_id,8192,tracklist_id,occurrence_id,entry_id,raw_source_ref '
                             'FROM main.album_share_items WHERE snapshot_id=? AND position=0',(long_id,))
            except sqlite3.IntegrityError:
                pass
            else:
                raise AssertionError('Future append was admitted before the audit')
        reservations = []
        def trace(sql):
            if sql == 'BEGIN IMMEDIATE':
                reservations.append(time.monotonic())
        conn.set_trace_callback(trace)
        exercised = False
        for index in range(1000):
            reservations.clear()
            started = time.monotonic()
            progress = audit.JOB.step(conn,batch_size=128)
            finished = time.monotonic()
            report['batches'].append({'seconds':finished-started,'writer_seconds':finished-reservations[-1] if reservations else 0})
            if not exercised and progress.processed>32:
                checkpoint = progress
                def fail(*args):
                    audit.JOB.copy(*args)
                    raise OSError('controlled actual-choice copy failure')
                try:
                    replace(audit.JOB,copy=fail).step(conn)
                except OSError:
                    pass
                else:
                    raise AssertionError('Controlled failure did not run')
                failed = audit.JOB.progress(conn)
                assert failed.status == 'failed' and failed.processed == checkpoint.processed
                started = time.monotonic()
                regular.migrate(conn)
                report['installed_regular_ddl_admission_while_failed_seconds'] = time.monotonic()-started
                assert audit.JOB.progress(conn) == failed
                audit.JOB.step(conn,retry=True)
                exercised = True
            if progress.status == 'prepared':
                break
        conn.set_trace_callback(None)
        report['progress'] = asdict(audit.verify(conn))
        for identity,choices in captures:
            observed = [row[0] for row in conn.execute('SELECT raw_source_ref FROM main.album_share_items WHERE snapshot_id=? ORDER BY position',(identity,))]
            assert observed == choices
            summary = conn.execute('SELECT * FROM '+audit.SUMMARIES+' WHERE snapshot_id=?',(identity,)).fetchone()
            assert summary['member_count'] == summary['expected_count'] == len(choices) and summary['blocked'] == 0
        for owner,keys in audit.JOB.keys.items():
            target = audit.SUMMARIES if owner == 'album_share_snapshots' else audit.MEMBERS
            predicate = 'snapshot_id=?' if owner == 'album_share_snapshots' else 'snapshot_id=? AND position=?'
            for row in conn.execute('SELECT * FROM main.'+owner):
                values = [row[column] for column in keys]
                key = json.dumps(values,separators=(',',':'))
                digest = conn.execute('SELECT source_digest FROM '+target+' WHERE '+predicate,values).fetchone()[0]
                assert audit.original(conn,owner,key,digest) == dict(row)
        assert before == {owner:fingerprint(conn,owner,','.join(keys)) for owner,keys in audit.JOB.keys.items()}
        assert list(map(tuple,conn.execute('SELECT * FROM schema_migrations ORDER BY version'))) == history
        assert conn.execute('SELECT count(*) FROM main.catalog_entries').fetchone()[0] == 0
        report.update(status='passed',captured_original_shares=len(originals),captured_original_members=sum(len(json.loads(row['video_ids_json'])) for row in originals),
                      controlled_extra_members=len(long_choices),sources_and_history_exact=True,identities_admitted=0)
    except BaseException as exc:
        report['error'] = repr(exc)
        raise
    finally:
        conn.set_trace_callback(None)
        report['read_only_source_exact'] = source_before == fingerprint(source,'album_share_snapshots','browse_id')
        report_path.write_text(json.dumps(report,indent=2))
        conn.close()
        source.close()


if __name__ == '__main__':
    check(*(Path(value).resolve() for value in sys.argv[1:]))
