"""Measure saved-output cutover and recovery on an isolated actual-library copy."""
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import time

from yt_playlist.core.migrations import MigrationError, regular
from yt_playlist.core.preparation import saved_output_publication as publication, saved_outputs as preparation
from yt_playlist.core.store import Store


def fingerprint(conn,owner,order):
    digest = hashlib.sha256()
    count = 0
    for row in conn.execute('SELECT * FROM main.'+owner+' ORDER BY '+order):
        digest.update(json.dumps(tuple(row),ensure_ascii=False,default=lambda value:value.hex()).encode())
        count += 1
    return count,digest.hexdigest()


def check(path,report_path):
    assert path.parent == report_path.parent == Path('/tmp') and path.name.startswith('tuneconsole-')
    report = {'status':'failed','database':str(path),'python':sys.version,'sqlite':sqlite3.sqlite_version,
        'dataset':'Saved Radio seed offer and historical manual autotune report from an isolated actual-library v93 copy. The later 1000-track/1000-policy seed is a controlled fixture using actual upload IDs and explicit unknown references.',
        'limits':'No native changes, HTTP/provider/runtime workers or full-library conversion. Original settings restored after controlled writes.',
        'batches':[],'publications':{}}
    store = Store(path)
    other = Store(path)
    conn = store.conn
    sources = {source:conn.execute('SELECT value FROM main.settings WHERE key=?',(source,)).fetchone()[0] for source in preparation.PREFIXES}
    protected = {'settings':'key','identities':'id','catalog_entries':'id','youtube_items':'video_id',
        'recommendation_policies':'version','rec_model_selection_runs':'id'}
    before = {owner:fingerprint(conn,owner,order) for owner,order in protected.items()}
    history = list(map(tuple,conn.execute('SELECT * FROM schema_migrations ORDER BY version')))
    reservations = []
    def trace(sql):
        if sql == 'BEGIN IMMEDIATE':
            reservations.append(time.monotonic())
    def finish(name):
        job = preparation.JOBS[name]
        job.install(conn)
        for _ in range(1000):
            reservations.clear()
            started = time.monotonic()
            progress = job.step(conn,batch_size=128)
            ended = time.monotonic()
            report['batches'].append({'job':name,'seconds':ended-started,
                'writer_seconds':ended-reservations[-1] if reservations else 0})
            if progress.status == 'prepared':
                return progress.prepared_revision
        raise AssertionError('Saved output preparation did not converge')
    def publish(label,source,revision):
        reservations.clear()
        started = time.monotonic()
        publication.publish(conn,source,revision=revision)
        ended = time.monotonic()
        report['publications'][label] = {'seconds':ended-started,
            'final_writer_seconds':ended-reservations[-1],'revision':revision}
    try:
        store.init_schema(defer_metadata=True)
        other.init_schema(defer_metadata=True)
        conn.set_trace_callback(trace)
        retained = {}
        for name,source in preparation.SOURCES.items():
            revision = finish(name)
            retained[source] = publication.capture(conn,source)
            publish('actual-'+name,source,revision)
            assert other.settings.get_setting(source) == sources[source]
            assert preparation.read_original(retained[source]) == sources[source]
        assert before == {owner:fingerprint(conn,owner,order) for owner,order in protected.items()}
        source = 'radio_seed_offer'
        job = preparation.JOBS['radio-seed']
        Path(retained[source]['filename']).unlink()
        real_publish = preparation.files.publish
        def failure(*args):
            assert not conn.in_transaction
            raise OSError('controlled saved-output recovery failure')
        preparation.files.publish = failure
        try:
            job.step(conn,retry=True)
        except OSError:
            pass
        else:
            raise AssertionError('Controlled file recovery failure did not run')
        finally:
            preparation.files.publish = real_publish
        failed = job.progress(conn)
        assert failed.status == 'failed'
        started = time.monotonic()
        regular.migrate(conn)
        report['regular_ddl_admission_while_recovery_failed_seconds'] = time.monotonic()-started
        assert job.progress(conn) == failed
        job.step(conn,retry=True)
        assert job.progress(conn).status == 'prepared'
        assert other.settings.get_setting(source) == sources[source]
        actual = json.loads(sources[source])
        videos = [track['video_id'] for track in actual['tracks']]
        controlled = {**actual,'tracks':[{'video_id':videos[index % len(videos)] if index % 3 else 'controlled-unadmitted',
            'pool_policy':'controlled-unrecorded-policy-'+str(index)} for index in range(1000)]}
        controlled_raw = json.dumps(controlled,ensure_ascii=False)
        other.settings.set_setting(source,controlled_raw)
        assert publication.selected(conn,source) is None
        assert publication.selected(conn,'rec_autotune_result') is not None
        revision = finish('radio-seed')
        def interrupt():
            raise KeyboardInterrupt('controlled saved-output publication interruption')
        try:
            publication.publish(conn,source,revision=revision,clock=interrupt)
        except KeyboardInterrupt:
            pass
        else:
            raise AssertionError('Controlled cutover interruption did not run')
        assert publication.selected(conn,source) is None
        real_read = preparation.read_original
        def racing(row):
            assert not conn.in_transaction
            with other.conn:
                other.conn.execute('UPDATE main.settings SET value=value WHERE key=?',(source,))
            return real_read(row)
        preparation.read_original = racing
        try:
            publication.publish(conn,source,revision=revision)
        except MigrationError as exc:
            assert 'reconciled preparation' in str(exc) or 'changed during verification' in str(exc)
        else:
            raise AssertionError('Stale seed publication succeeded')
        finally:
            preparation.read_original = real_read
        revision = finish('radio-seed')
        publish('controlled-1000-tracks-1000-policies',source,revision)
        assert other.settings.get_setting(source) == controlled_raw
        assert [row[0] for row in conn.execute('SELECT video_id FROM '+preparation.SUBJECTS+
            ' WHERE preparation_revision=? ORDER BY position',(revision,))] == [track['video_id'] for track in controlled['tracks']]
        assert conn.execute('SELECT count(*) FROM '+preparation.POLICIES+' WHERE preparation_revision=?',(revision,)).fetchone()[0] == 1000
        assert not conn.execute("SELECT 1 FROM youtube_items WHERE video_id='controlled-unadmitted'").fetchone()
        started = time.monotonic()
        other.settings.set_setting(source,'{"tracks":[]}')
        report['compatible_save_seconds'] = time.monotonic()-started
        assert publication.selected(conn,source) is None
        assert store.settings.get_setting(source) == '{"tracks":[]}'
        assert preparation.read_original(retained[source]) == sources[source]
        report.update(status='passed',actual_bytes={key:len(raw.encode()) for key,raw in sources.items()},
            actual_seed_tracks=len(actual['tracks']),controlled_tracks=1000,controlled_policies=1000,
            controlled_unresolved_subjects=334,missing_file_recovered_explicitly=True,
            stale_publication_rejected=True,interruption_retried=True,
            final_progress={name:asdict(job.progress(conn)) for name,job in preparation.JOBS.items()})
    except BaseException as exc:
        report['error'] = repr(exc)
        raise
    finally:
        conn.set_trace_callback(None)
        conn.rollback()
        with other.conn:
            other.conn.executemany('UPDATE main.settings SET value=? WHERE key=?',[(raw,key) for key,raw in sources.items()])
        report['source_owners_exact_after_restore'] = before == {owner:fingerprint(conn,owner,order) for owner,order in protected.items()}
        report['history_exact'] = history == list(map(tuple,conn.execute('SELECT * FROM schema_migrations ORDER BY version')))
        report_path.write_text(json.dumps(report,indent=2))
        other.conn.close()
        conn.close()
    assert report['source_owners_exact_after_restore'] and report['history_exact']


if __name__ == '__main__':
    check(*(Path(value).resolve() for value in sys.argv[1:]))
