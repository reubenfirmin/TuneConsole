"""Measure exact Radio publication on an isolated actual-library database.

The original checkpoint is published first. An explicitly controlled 8192-item
queue then uses its existing upload IDs without providers or catalog admission.
The source document is restored even if a check fails; native storage is unused.
"""
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import time

from yt_playlist.core import radio_storage
from yt_playlist.core.migrations import MigrationError, regular
from yt_playlist.core.preparation import radio_publication as publication, radio_session as preparation
from yt_playlist.core.store import Store


def fingerprint(conn,table,order):
    digest = hashlib.sha256()
    count = 0
    for row in conn.execute('SELECT * FROM main.'+table+' ORDER BY '+order):
        digest.update(json.dumps(tuple(row),ensure_ascii=False).encode())
        count += 1
    return count,digest.hexdigest()


def check(path,report_path):
    assert path.parent == report_path.parent == Path('/tmp') and path.name.startswith('tuneconsole-')
    report = {'status':'failed','database':str(path),'python':sys.version,'sqlite':sqlite3.sqlite_version,
        'dataset':'Existing isolated actual-library v93 snapshot and its exact saved Radio checkpoint; subsequent 8192-item queue is a controlled repetition of actual upload IDs.',
        'limits':'No native database, provider calls, HTTP or runtime background workers. Source settings restored after controls.',
        'batches':[],'publications':{}}
    store = Store(path)
    conn = store.conn
    other = Store(path)
    raw = conn.execute('SELECT value FROM main.settings WHERE key=?',(preparation.KEY,)).fetchone()[0]
    original = json.loads(raw)
    protected = {'settings':'key','identities':'id','recordings':'id','youtube_items':'video_id','catalog_entries':'id'}
    before = {table:fingerprint(conn,table,order) for table,order in protected.items()}
    history = list(map(tuple,conn.execute('SELECT * FROM schema_migrations ORDER BY version')))
    reservations = []
    def trace(sql):
        if sql == 'BEGIN IMMEDIATE':
            reservations.append(time.monotonic())
    def finish():
        for _ in range(1000):
            reservations.clear()
            started = time.monotonic()
            progress = preparation.JOB.step(conn,batch_size=128)
            ended = time.monotonic()
            report['batches'].append({'seconds':ended-started,'writer_seconds':ended-reservations[-1] if reservations else 0})
            if progress.status == 'prepared':
                return progress.prepared_revision
        raise AssertionError('Radio preparation did not converge')
    def publish(label,revision):
        reservations.clear()
        started = time.monotonic()
        publication.publish(conn,revision=revision)
        ended = time.monotonic()
        report['publications'][label] = {'seconds':ended-started,
            'final_writer_seconds':ended-reservations[-1], 'revision':revision}
    try:
        store.init_schema(defer_metadata=True)
        other.init_schema(defer_metadata=True)
        conn.set_trace_callback(trace)
        preparation.JOB.install(conn)
        actual_revision = finish()
        actual_header = conn.execute('SELECT * FROM '+preparation.HEAD+' WHERE generation=?',(actual_revision,)).fetchone()
        publish('actual',actual_revision)
        assert other.settings.get_setting(preparation.KEY) == raw
        assert radio_storage.load_published(other.conn) == (original,None)
        assert preparation.read(actual_header) == raw
        assert before == {table:fingerprint(conn,table,order) for table,order in protected.items()}
        videos = [track['video_id'] for track in original['tracks']]
        assert videos
        controlled = {**original,'tracks':[{'video_id':videos[index % len(videos)]} for index in range(8192)],
            'initial_tracks':[],'pending':None,'position':1}
        controlled_raw = json.dumps(controlled,ensure_ascii=False,indent=2)
        with other.conn:
            other.conn.execute('UPDATE main.settings SET value=? WHERE key=?',(controlled_raw,preparation.KEY))
        assert publication.selected(conn) is None
        preparation.JOB.step(conn,batch_size=128)
        checkpoint = preparation.JOB.progress(conn)
        real_publish = preparation._publish_encoded
        def fail(*args):
            assert not conn.in_transaction
            raise OSError('controlled Radio file failure')
        preparation._publish_encoded = fail
        try:
            preparation.JOB.step(conn,batch_size=128)
        except OSError:
            pass
        else:
            raise AssertionError('Controlled Radio failure did not run')
        finally:
            preparation._publish_encoded = real_publish
        failed = preparation.JOB.progress(conn)
        assert failed.status == 'failed' and failed.processed == checkpoint.processed
        started = time.monotonic()
        regular.migrate(conn)
        report['regular_ddl_admission_while_failed_seconds'] = time.monotonic()-started
        assert preparation.JOB.progress(conn) == failed
        preparation.JOB.step(conn,batch_size=128,retry=True)
        revision = finish()
        def interrupt():
            raise KeyboardInterrupt('controlled Radio publication interruption')
        try:
            publication.publish(conn,revision=revision,clock=interrupt)
        except KeyboardInterrupt:
            pass
        else:
            raise AssertionError('Controlled publication interruption did not run')
        assert publication.selected(conn) is None
        assert list(map(tuple,conn.execute('SELECT revision FROM '+publication.GENERATIONS))) == [(actual_revision,)]
        real_read = preparation.read
        def racing(row):
            assert not conn.in_transaction
            with other.conn:
                other.conn.execute('UPDATE main.settings SET value=value WHERE key=?',(preparation.KEY,))
            return real_read(row)
        preparation.read = racing
        try:
            publication.publish(conn,revision=revision)
        except MigrationError as exc:
            assert 'changed during verification' in str(exc)
        else:
            raise AssertionError('Source revision race selected stale Radio state')
        finally:
            preparation.read = real_read
        revision = finish()
        publish('controlled_8192',revision)
        assert other.settings.get_setting(preparation.KEY) == controlled_raw
        assert radio_storage.load_published(other.conn) == (controlled,None)
        assert [row[0] for row in conn.execute('SELECT video_id FROM '+preparation.QUEUE+
            ' WHERE generation=? ORDER BY position',(revision,))] == [track['video_id'] for track in controlled['tracks']]
        started = time.monotonic()
        updated = {**controlled,'phase':'stopped','position':2}
        other.settings.set_setting(preparation.KEY,json.dumps(updated))
        report['compatible_save_seconds'] = time.monotonic()-started
        assert publication.selected(conn) is None
        assert radio_storage.load_published(conn) == (updated,None)
        assert preparation.read(actual_header) == raw
        report.update(status='passed',actual_queue_items=actual_header['queue_size'],actual_source_bytes=len(raw.encode()),
            controlled_queue_items=8192,exact_original_and_queue=True,stale_publication_rejected=True,
            interruption_and_explicit_retry=True,final_progress=asdict(preparation.JOB.progress(conn)))
    except BaseException as exc:
        report['error'] = repr(exc)
        raise
    finally:
        conn.set_trace_callback(None)
        conn.rollback()
        with other.conn:
            other.conn.execute('UPDATE main.settings SET value=? WHERE key=?',(raw,preparation.KEY))
        report['source_owners_exact_after_restore'] = before == {table:fingerprint(conn,table,order) for table,order in protected.items()}
        report['history_exact'] = history == list(map(tuple,conn.execute('SELECT * FROM schema_migrations ORDER BY version')))
        report_path.write_text(json.dumps(report,indent=2))
        other.conn.close()
        conn.close()
    assert report['source_owners_exact_after_restore'] and report['history_exact']


if __name__ == '__main__':
    check(*(Path(value).resolve() for value in sys.argv[1:]))
