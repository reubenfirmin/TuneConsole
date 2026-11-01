"""Measure retained Radio origins on an isolated actual-library snapshot."""
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import time

from yt_playlist.core import radio_provenance
from yt_playlist.core.migrations import MigrationError, regular
from yt_playlist.core.preparation import radio_origin_publication as publication, radio_origins as preparation
from yt_playlist.core.store import Store


def fingerprint(conn,owner,order):
    digest = hashlib.sha256()
    count = 0
    for row in conn.execute('SELECT * FROM main.'+owner+' ORDER BY '+order):
        digest.update(json.dumps(tuple(row),ensure_ascii=False).encode())
        count += 1
    return count,digest.hexdigest()


def check(path,report_path):
    assert path.parent == report_path.parent == Path('/tmp') and path.name.startswith('tuneconsole-')
    report = {'status':'failed','database':str(path),'python':sys.version,'sqlite':sqlite3.sqlite_version,
        'dataset':'Four Radio origin settings from an existing isolated genuine v93 actual-library snapshot. Subsequent 8192-member list is controlled, repeating actual provider IDs plus an explicitly unknown ID.',
        'limits':'No native writes, provider calls, HTTP/runtime background workers or full-library conversion. Original settings restored after controlled writes.',
        'batches':[],'publications':{}}
    store = Store(path)
    other = Store(path)
    conn = store.conn
    sources = dict(conn.execute('SELECT key,value FROM main.settings WHERE key IN (?,?,?,?)',preparation.KEYS))
    protected = {'settings':'key','identities':'id','playlist_scopes':'ref','catalog_entries':'id'}
    before = {owner:fingerprint(conn,owner,order) for owner,order in protected.items()}
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
        raise AssertionError('Radio origin preparation did not converge')
    def publish(label,revision):
        reservations.clear()
        started = time.monotonic()
        publication.publish(conn,revision=revision)
        ended = time.monotonic()
        report['publications'][label] = {'seconds':ended-started,'final_writer_seconds':ended-reservations[-1],
            'revision':revision}
    try:
        store.init_schema(defer_metadata=True)
        other.init_schema(defer_metadata=True)
        expected = radio_provenance.ids(other.conn)
        conn.set_trace_callback(trace)
        preparation.JOB.install(conn)
        revision = finish()
        retained = publication.capture(conn)
        publish('actual',revision)
        assert radio_provenance.ids(other.conn) == expected
        assert {key:other.settings.get_setting(key) for key in sources} == sources
        assert before == {owner:fingerprint(conn,owner,order) for owner,order in protected.items()}
        first = conn.execute('SELECT filename FROM '+preparation.PARTS+' WHERE source=? AND generation=? LIMIT 1',
            (retained[0]['source'],retained[0]['generation'])).fetchone()
        assert first
        Path(first[0]).unlink()
        real_publish = preparation.files.publish
        def failure(*args):
            assert not conn.in_transaction
            raise OSError('controlled origin recovery failure')
        preparation.files.publish = failure
        try:
            preparation.JOB.step(conn,retry=True)
        except OSError:
            pass
        else:
            raise AssertionError('Controlled origin recovery failure did not run')
        finally:
            preparation.files.publish = real_publish
        failed = preparation.JOB.progress(conn)
        assert failed.status == 'failed'
        started = time.monotonic()
        regular.migrate(conn)
        report['regular_ddl_admission_while_recovery_failed_seconds'] = time.monotonic()-started
        assert preparation.JOB.progress(conn) == failed
        preparation.JOB.step(conn,retry=True)
        assert preparation.JOB.progress(conn).status == 'prepared'
        assert radio_provenance.ids(other.conn) == expected
        assert {key:other.settings.get_setting(key) for key in sources} == sources
        choices = [*expected,'controlled-unresolved-origin']
        controlled = [choices[index % len(choices)] for index in range(8192)]
        raw = json.dumps(controlled,ensure_ascii=False,indent=2)
        other.settings.set_setting(preparation.KEYS[0],raw)
        assert publication.selected(conn) is None
        revision = finish()
        def interrupted():
            raise KeyboardInterrupt('controlled origin publication interruption')
        try:
            publication.publish(conn,revision=revision,clock=interrupted)
        except KeyboardInterrupt:
            pass
        else:
            raise AssertionError('Controlled publication interruption did not run')
        assert publication.selected(conn) is None
        real_read = preparation.read_original
        changed = False
        def racing(connection,row):
            nonlocal changed
            assert not connection.in_transaction
            if not changed:
                with other.conn:
                    other.conn.execute('UPDATE main.settings SET value=value WHERE key=?',(preparation.KEYS[0],))
                changed = True
            return real_read(connection,row)
        preparation.read_original = racing
        try:
            publication.publish(conn,revision=revision)
        except MigrationError as exc:
            assert 'reconciled preparation' in str(exc) or 'changed during verification' in str(exc)
        else:
            raise AssertionError('Stale origins were selected')
        finally:
            preparation.read_original = real_read
        revision = finish()
        publish('controlled-8192',revision)
        assert other.settings.get_setting(preparation.KEYS[0]) == raw
        assert radio_provenance.ids(other.conn) == list(dict.fromkeys([*controlled,
            *(sources[key] for key in preparation.KEYS[1:] if sources.get(key))]))
        current = conn.execute('SELECT generation FROM '+preparation.CURRENT+' WHERE source=?',(preparation.KEYS[0],)).fetchone()[0]
        assert [row[0] for row in conn.execute('SELECT provider_playlist_id FROM '+preparation.ORIGINS+
            ' WHERE source=? AND generation=? ORDER BY position',(preparation.KEYS[0],current))] == controlled
        started = time.monotonic()
        with other.conn:
            radio_provenance.remember(other.conn,'controlled-later-origin')
        report['compatible_remember_seconds'] = time.monotonic()-started
        assert publication.selected(conn) is None
        assert radio_provenance.ids(conn)[-1] == 'controlled-later-origin'
        assert not conn.execute("SELECT 1 FROM main.playlist_scopes WHERE provider_id IN ('controlled-unresolved-origin','controlled-later-origin')").fetchone()
        for row in retained:
            assert preparation.read_original(conn,row) == sources[row['source']]
        report.update(status='passed',actual_sources=len(sources),actual_bytes={key:len(raw.encode()) if raw else 0 for key,raw in sources.items()},
            actual_distinct_origins=len(expected),actual_member_count=sum(row['item_count'] for row in retained),
            controlled_members=8192,missing_file_recovered_explicitly=True,stale_publication_rejected=True,
            interruption_retried=True,final_progress=asdict(preparation.JOB.progress(conn)))
    except BaseException as exc:
        report['error'] = repr(exc)
        raise
    finally:
        conn.set_trace_callback(None)
        conn.rollback()
        with other.conn:
            other.conn.execute('DELETE FROM main.settings WHERE key IN (?,?,?,?)',preparation.KEYS)
            other.conn.executemany('INSERT INTO main.settings VALUES (?,?)',sources.items())
        report['source_owners_exact_after_restore'] = before == {owner:fingerprint(conn,owner,order) for owner,order in protected.items()}
        report['history_exact'] = history == list(map(tuple,conn.execute('SELECT * FROM schema_migrations ORDER BY version')))
        report_path.write_text(json.dumps(report,indent=2))
        other.conn.close()
        conn.close()
    assert report['source_owners_exact_after_restore'] and report['history_exact']


if __name__ == '__main__':
    check(*(Path(value).resolve() for value in sys.argv[1:]))
