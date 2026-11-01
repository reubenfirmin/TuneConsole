"""Exercise action publication on a supplied disposable actual-library copy."""
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import time

from yt_playlist.core.migrations import MigrationError, regular
from yt_playlist.core.preparation import action_ledger as preparation, action_publication as publication
from yt_playlist.core.store import Store


def fingerprint(conn, owner, order):
    digest, count = hashlib.sha256(), 0
    for row in conn.execute('SELECT * FROM main.'+owner+' ORDER BY '+order):
        digest.update(json.dumps(tuple(row), ensure_ascii=False).encode())
        count += 1
    return count, digest.hexdigest()


def check(path, report_path):
    assert path.parent == report_path.parent == Path('/tmp') and path.name.startswith('tuneconsole-')
    report = {'status':'failed', 'database':str(path), 'python':sys.version, 'sqlite':sqlite3.sqlite_version,
        'dataset':'Existing isolated genuine v93 library; all actual actions and backup custody. Controlled placement uses 4096 actual videos repeated into 8192 ordered positions, plus a draft and an undo link to actual history.',
        'limits':'No native writes, HTTP, providers, runtime workers, remote intents or full historical conversion. Controlled sources and observed sequence restored afterward.',
        'batches':[], 'maps':[], 'publications':{}}
    store, other = Store(path), Store(path)
    conn, owner = store.conn, publication.OWNER
    protected = {'settings':'key', 'actions':'id', 'identities':'id', 'playlist_scopes':'ref',
        'catalog_entries':'id', 'recordings':'id', 'youtube_items':'video_id'}
    before = {table:fingerprint(conn, table, order) for table, order in protected.items()}
    history = list(map(tuple, conn.execute('SELECT * FROM schema_migrations ORDER BY version')))
    sequence = list(map(tuple, conn.execute("SELECT * FROM sqlite_sequence WHERE name='actions'")))
    controlled, reservations = [], []
    def trace(sql):
        if sql == 'BEGIN IMMEDIATE':
            reservations.append(time.monotonic())
    def timed(category, function):
        reservations.clear()
        started = time.monotonic()
        result = function()
        ended = time.monotonic()
        report[category].append({'seconds':ended-started, 'writer_seconds':ended-reservations[-1] if reservations else 0})
        return result
    def prepare():
        owner.worker.install(conn)
        for _ in range(4000):
            if timed('batches', lambda:owner.worker.step(conn, batch_size=128)).status == 'prepared':
                return
        raise AssertionError('Action preparation did not finish')
    def stage():
        for _ in range(100):
            result = timed('maps', lambda:owner.step(conn, batch_size=32))
            if result.complete:
                return result.id
        raise AssertionError('Action map did not finish')
    def publish(label, identity):
        reservations.clear()
        started = time.monotonic()
        owner.publish(conn, stage=identity)
        ended = time.monotonic()
        assert owner.selected(other.conn) == identity
        report['publications'][label] = {'stage':identity, 'seconds':ended-started,
            'writer_seconds':ended-reservations[-1], 'documents':owner.stage(conn, identity).copied}
    try:
        store.init_schema(defer_metadata=True)
        other.init_schema(defer_metadata=True)
        conn.set_trace_callback(trace)
        prepare()
        installed = stage()
        publish('installed', installed)
        originals = {row['id']:dict(row) for row in conn.execute('SELECT * FROM main.actions')}
        for identity, value in originals.items():
            assert owner.value(conn, installed, str(identity)) == value
        report['installed_actions'] = len(originals)
        report['installed_backups'] = dict(conn.execute('SELECT b.backup_status,count(*) FROM '+owner.table('map')+
            ' m JOIN prepared_action_backups b ON b.source=m.source AND b.preparation_revision=m.generation '
            'WHERE m.stage=? GROUP BY b.backup_status', (installed,)))
        def authorizer(operation, table, column, *unused):
            if operation == sqlite3.SQLITE_READ and table == 'actions' and column in ('params_json','plan_json','undo_json'):
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK
        other.conn.set_authorizer(authorizer)
        try:
            observed = other.actions.get_actions()
        finally:
            other.conn.set_authorizer(None)
        assert [action.id for action in observed] == sorted(originals, reverse=True)
        for action in observed:
            assert (action.kind,action.params_json,action.plan_json,action.status,action.created_at,action.executed_at) == tuple(
                originals[action.id][key] for key in ('kind','params_json','plan_json','status','created_at','executed_at'))
        videos = [row[0] for row in conn.execute('SELECT video_id FROM youtube_items ORDER BY video_id LIMIT 4096')]
        assert len(videos) == 4096
        member = conn.execute('SELECT id FROM main.playlist_membership_refs WHERE scope_ref IS NOT NULL ORDER BY id LIMIT 1').fetchone()[0]
        plan = {'playlist_id':member, 'after_video_id':None, 'video_ids':videos*2,
            'set_video_ids':{videos[0]:'captured-occurrence'}}
        placed = store.actions.record_action('add_tracks', '{}', json.dumps(plan), 'pending', '{}', None)
        controlled.append(placed)
        draft = store.actions.record_action('merge_draft', '{"paid":"東京"}', 'controlled|signature', 'draft', 'plain evidence', None)
        controlled.append(draft)
        linked = store.actions.record_action('undo', json.dumps({'undid':min(originals)}), '{}', 'executed', '{}', 0)
        controlled.append(linked)
        assert owner.selected(conn) is None
        def fail(*args):
            preparation.copy(*args)
            raise OSError('controlled actual action publication failure')
        try:
            replace(owner.worker, copy=fail).step(conn)
        except OSError as exc:
            assert 'controlled actual action publication failure' in str(exc)
        else:
            raise AssertionError('Controlled failure did not run')
        failed = owner.worker.progress(conn)
        started = time.monotonic()
        store.init_schema(defer_metadata=True)
        regular.migrate(conn)
        report['installed_ddl_while_failed_seconds'] = time.monotonic()-started
        assert owner.worker.progress(conn) == failed and failed.status == 'failed'
        owner.worker.step(conn, retry=True)
        prepare()
        identity = stage()
        with conn:
            conn.execute('CREATE TRIGGER controlled_actual_action_cutover BEFORE INSERT ON '+owner.table('head')+
                " BEGIN SELECT RAISE(ABORT,'controlled cutover interruption'); END")
        try:
            owner.publish(conn, stage=identity)
        except sqlite3.IntegrityError as exc:
            assert 'controlled cutover interruption' in str(exc)
        else:
            raise AssertionError('Interrupted cutover committed')
        assert not conn.execute('SELECT 1 FROM '+owner.table('generations')+' WHERE revision=?', (identity,)).fetchone()
        with conn:
            conn.execute('DROP TRIGGER controlled_actual_action_cutover')
        publish('controlled', identity)
        assert json.loads(other.actions.get_action(placed).plan_json) == plan
        assert other.actions.get_draft('controlled|signature').id == draft
        assert preparation.head(conn, linked)['undo_of_action_id'] == min(originals)
        header = preparation.head(conn, placed)
        for key in ('plan_filename','recovery_filename'):
            Path(header[key]).unlink()
        Path(conn.execute('SELECT filename FROM prepared_action_parts WHERE source=? AND preparation_revision=? LIMIT 1',
            (str(placed), header['preparation_revision'])).fetchone()[0]).unlink()
        started = time.monotonic()
        store.init_schema(defer_metadata=True)
        regular.migrate(conn)
        report['installed_ddl_with_missing_file_seconds'] = time.monotonic()-started
        publication.repair(conn, str(placed))
        publish('repaired', identity)
        changed = False
        def race(connection, generation):
            nonlocal changed
            publication.verify_document(connection, generation)
            if not changed:
                changed = True
                with other.conn:
                    other.conn.execute("DELETE FROM sqlite_sequence WHERE name='actions'")
                    other.conn.execute("INSERT INTO sqlite_sequence(name,seq) VALUES ('actions',1000000)")
        try:
            replace(owner, verify_document=race).publish(conn, stage=identity)
        except MigrationError:
            pass
        else:
            raise AssertionError('Changed sequence was not fenced')
        publish('current-sequence', identity)
        assert conn.execute("SELECT seq FROM sqlite_sequence WHERE name='actions'").fetchone()[0] == 1000000
        started = time.monotonic()
        store.actions.update_action(placed, 'cancelled', 0)
        report['compatible_writer_seconds'] = time.monotonic()-started
        assert owner.selected(conn) is None
        prepare()
        publish('continuing-update', stage())
        assert other.actions.get_action(placed).status == 'cancelled'
        assert owner.value(conn, identity, str(placed))['status'] == 'pending'
        assert conn.execute("SELECT seq FROM sqlite_sequence WHERE name='actions'").fetchone()[0] == 1000000
        report.update(status='passed', controlled_placement_members=8192, original_archive_exact=True,
            preopened_readers_without_source_documents=True, cross_action_links_verified=True,
            interrupted_cutover_rolled_back=True, changed_sequence_fenced_and_preserved=True,
            exact_files_recovered=True, failed_job_retried_explicitly=True, continuing_writer_reconciled=True,
            no_remote_operation_admitted=True)
    except BaseException as exc:
        report['error'] = repr(exc)
        raise
    finally:
        conn.set_trace_callback(None)
        conn.rollback()
        try:
            with conn:
                for identity in controlled:
                    conn.execute('DELETE FROM main.actions WHERE id=?', (identity,))
                conn.execute("DELETE FROM sqlite_sequence WHERE name='actions'")
                conn.executemany('INSERT INTO sqlite_sequence VALUES (?,?)', sequence)
            assert before == {table:fingerprint(conn, table, order) for table, order in protected.items()}
            assert history == list(map(tuple, conn.execute('SELECT * FROM schema_migrations ORDER BY version')))
            assert sequence == list(map(tuple, conn.execute("SELECT * FROM sqlite_sequence WHERE name='actions'")))
            report['original_sources_sequence_references_history_restored_exact'] = True
        finally:
            report_path.write_text(json.dumps(report, indent=2))
            other.conn.close()
            conn.close()
    print(json.dumps({key:value for key,value in report.items() if key not in ('batches','maps')}, indent=2))


if __name__ == '__main__':
    check(Path(sys.argv[1]), Path(sys.argv[2]))
