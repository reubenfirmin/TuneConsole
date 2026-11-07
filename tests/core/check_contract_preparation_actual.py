"""Measure domain audits and future guards on an isolated actual-library copy."""
from dataclasses import asdict, replace
import hashlib
import json
import math
from pathlib import Path
import sqlite3
import sys
import time

from yt_playlist.core.migrations import regular
from yt_playlist.core.preparation import domain_contracts as audit


def fingerprint(conn,owner,keys):
    digest = hashlib.sha256()
    count = 0
    for row in conn.execute('SELECT * FROM main.' + owner + ' ORDER BY ' + ','.join(keys)):
        digest.update(json.dumps(tuple(row),ensure_ascii=False,default=lambda value:{'blob':value.hex()}).encode())
        count += 1
    return count,digest.hexdigest()


def typed(value):
    kind = 'null' if value is None else 'integer' if type(value) is int else 'real' if type(value) is float else 'blob' if isinstance(value,bytes) else 'text'
    captured = value.hex().upper() if kind == 'blob' else ('+Infinity' if value>0 else '-Infinity') if kind == 'real' and not math.isfinite(value) else value
    return {'type':kind,'value':captured}


def check(path,report_path):
    assert path.parent == Path('/tmp') and path.name.startswith('tuneconsole-')
    report = {'snapshot':str(path),'status':'failed','python':sys.version,'sqlite':sqlite3.sqlite_version,
        'dataset':'Isolated actual-library installed domain owners.',
        'controls':'No HTTP/provider/runtime workers, native changes or identity admission. One controlled no-op availability write and rolled-back copy failure.',
        'jobs':{},'not_installed':[]}
    conn = sqlite3.connect(path.as_uri() + '?mode=rw',uri=True,timeout=.1)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys=ON')
    jobs = {}
    for owner in audit.CONTRACTS:
        if conn.execute("SELECT 1 FROM main.sqlite_schema WHERE type='table' AND name=?",(owner,)).fetchone():
            jobs[owner] = audit.job(conn,owner)
        else:
            report['not_installed'].append(owner)
    before = {owner:fingerprint(conn,owner,job.keys[owner]) for owner,job in jobs.items()}
    settings = fingerprint(conn,'settings',('key',))
    history = list(map(tuple,conn.execute('SELECT * FROM schema_migrations ORDER BY version')))
    try:
        for owner,job in jobs.items():
            started = time.monotonic()
            job.install(conn)
            detail = report['jobs'][owner] = {'installation_seconds':time.monotonic()-started,'batches':[]}
            reservations = []
            def trace(sql):
                if sql == 'BEGIN IMMEDIATE':
                    reservations.append(time.monotonic())
            conn.set_trace_callback(trace)
            exercised = False
            for index in range(5000):
                reservations.clear()
                started = time.monotonic()
                progress = job.step(conn,batch_size=128)
                finished = time.monotonic()
                detail['batches'].append({'seconds':finished-started,'writer_seconds':finished-reservations[-1] if reservations else 0})
                if owner == 'youtube_items' and not exercised:
                    source = conn.execute('SELECT video_id FROM main.youtube_items ORDER BY video_id LIMIT 1').fetchone()[0]
                    started = time.monotonic()
                    with conn:
                        conn.execute('UPDATE main.youtube_items SET available=available WHERE video_id=?',(source,))
                    detail['continuing_write_seconds'] = time.monotonic()-started
                    checkpoint = job.progress(conn)
                    def fail(*args):
                        job.copy(*args)
                        raise OSError('controlled actual contract copy failure')
                    try:
                        replace(job,copy=fail).step(conn)
                    except OSError:
                        pass
                    else:
                        raise AssertionError('Controlled contract failure did not run')
                    failed = job.progress(conn)
                    assert failed.status == 'failed' and failed.processed == checkpoint.processed
                    started = time.monotonic()
                    regular.migrate(conn)
                    detail['already_installed_ddl_admission_while_failed_seconds'] = time.monotonic()-started
                    assert job.progress(conn) == failed
                    job.step(conn,retry=True)
                    exercised = True
                if progress.status == 'prepared':
                    break
            conn.set_trace_callback(None)
            detail['progress'] = asdict(audit.verify(conn,job))
            expected = 0
            keys = job.keys[owner]
            for source in conn.execute('SELECT * FROM main.' + owner + ' ORDER BY ' + ','.join(keys)):
                key = json.dumps([source[column] for column in keys],ensure_ascii=False,separators=(',',':'))
                staged = conn.execute('SELECT * FROM ' + audit.table(job,'rows') + ' WHERE source_key=?',(key,)).fetchone()
                assert staged is not None and staged['violation_count'] == 0
                original = audit.original(conn,job,key,staged['source_digest'])
                assert original == {'key':json.loads(key),'fields':{column:typed(source[column]) for column in audit.CONTRACTS[owner]}}
                expected += 1
            assert expected == conn.execute('SELECT count(*) FROM ' + audit.table(job,'rows')).fetchone()[0]
            detail['source_rows'] = expected
        report.update(status='passed',original_values_and_storage_types_exact=True,all_installed_owners_verified=True)
    except BaseException as exc:
        report['error'] = repr(exc)
        raise
    finally:
        conn.set_trace_callback(None)
        report['audited_sources_exact'] = before == {owner:fingerprint(conn,owner,job.keys[owner]) for owner,job in jobs.items()}
        report['settings_exact'] = fingerprint(conn,'settings',('key',)) == settings
        report['history_exact'] = list(map(tuple,conn.execute('SELECT * FROM schema_migrations ORDER BY version'))) == history
        report_path.write_text(json.dumps(report,indent=2))
        conn.close()
    assert report['audited_sources_exact'] and report['settings_exact'] and report['history_exact']


if __name__ == '__main__':
    check(Path(sys.argv[1]).resolve(),Path(sys.argv[2]).resolve())
