"""Measure retained trial controls on an isolated genuine installed library."""
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import time

from yt_playlist.core.migrations import MigrationError, regular
from yt_playlist.core.preparation import model_trials as preparation, trial_publication as publication
from yt_playlist.core.store import Store


def fingerprint(conn, table, order):
    digest, count = hashlib.sha256(), 0
    for row in conn.execute('SELECT * FROM main.'+table+' ORDER BY '+order):
        digest.update(json.dumps(tuple(row), ensure_ascii=False).encode())
        count += 1
    return count, digest.hexdigest()


def check(path, report_path):
    assert path.parent == report_path.parent == Path('/tmp') and path.name.startswith('tuneconsole-')
    report = {'status':'failed', 'database':str(path), 'python':sys.version, 'sqlite':sqlite3.sqlite_version,
        'dataset':'Existing isolated genuine v93 library. Installed controls are recorded separately. Controlled documents use an existing selection run when available, an unadmitted run reference, exact decision/assignment tokens and a full 4096-value monitoring baseline.',
        'limits':'No native writes, HTTP, providers, model fitting, runtime workers or full historical conversion. Original proposals restored afterward.',
        'batches':[], 'publications':{}}
    store, other = Store(path), Store(path)
    conn = store.conn
    originals = {source:conn.execute('SELECT payload,built_at FROM rec_proposals WHERE surface=?', (source,)).fetchone()
        for source in publication.SOURCES}
    originals = {source:tuple(row) if row else None for source, row in originals.items()}
    protected = {'settings':'key', 'rec_proposals':'surface', 'identities':'id', 'playlist_scopes':'ref',
        'catalog_entries':'id', 'recordings':'id', 'youtube_items':'video_id', 'rec_model_selection_runs':'id'}
    before = {table:fingerprint(conn, table, order) for table, order in protected.items()}
    history = list(map(tuple, conn.execute('SELECT * FROM schema_migrations ORDER BY version')))
    reservations = []
    def trace(sql):
        if sql == 'BEGIN IMMEDIATE':
            reservations.append(time.monotonic())
    def ready(owner):
        for _ in range(100):
            reservations.clear()
            started = time.monotonic()
            progress = owner.worker.step(conn, batch_size=128)
            ended = time.monotonic()
            report['batches'].append({'source':owner.source, 'seconds':ended-started,
                'writer_seconds':ended-reservations[-1] if reservations else 0})
            if progress.status == 'prepared':
                return progress.prepared_revision
        raise AssertionError('Trial preparation did not finish')
    def publish(name, owner, revision):
        reservations.clear()
        started = time.monotonic()
        owner.publish(conn, revision=revision)
        ended = time.monotonic()
        assert owner.selected(other.conn) == revision
        report['publications'][name+':'+owner.source] = {'revision':revision, 'seconds':ended-started,
            'writer_seconds':ended-reservations[-1]}
    try:
        store.init_schema(defer_metadata=True)
        other.init_schema(defer_metadata=True)
        conn.set_trace_callback(trace)
        report['installed'] = {source:{'present':row is not None,
            'bytes':len(row[0].encode()) if row and row[0] else 0} for source, row in originals.items()}
        for owner in publication.OWNERS.values():
            owner.worker.install(conn)
            if owner.worker.progress(conn).status == 'failed':
                report.setdefault('explicit_retry_on_checker_resume', []).append(owner.source)
                owner.worker.step(conn, retry=True)
            publish('installed', owner, ready(owner))
            assert other.rec_proposals._proposal_json(owner.source) == (originals[owner.source][0] if originals[owner.source] else None)
        known = conn.execute('SELECT id FROM rec_model_selection_runs ORDER BY id LIMIT 1').fetchone()
        known = known[0] if known else None
        missing = 9223372036854775807
        assert not conn.execute('SELECT 1 FROM rec_model_selection_runs WHERE id=?', (missing,)).fetchone()
        report['existing_run_reference'] = known
        values = {'model_live_comparison':{'phase':'observing', 'attempt':2, 'seed':'0007',
            'policy':{'version':'controlled-original', 'horizon_days':7, 'family_alpha':.05},
            'previous':{'method':'svd', 'dim':2}, 'challenger':None, 'alpha':.025, 'run_id':known,
            'started_at':10, 'exposure_until':100, 'generation':'captured-generation', 'vectors':'captured-artifact'},
            'model_listening_monitor':{'run_id':missing, 'generation':'prior-generation', 'setup':{'method':'svd'},
                'baseline':[index/4095 for index in range(4096)], 'complete':False, 'next_at':300},
            'model_album_assignment':{'epoch':'0002', 'comparison':{'run_id':missing, 'arm':'challenger',
                'format':'albums', 'probability':.5, 'window':'002', 'unit':'0', 'controlled':True}}}
        raw = {source:json.dumps(value, ensure_ascii=False, indent=2) for source, value in values.items()}
        report['controlled_bytes'] = {source:len(value.encode()) for source, value in raw.items()}
        with conn:
            conn.executemany('INSERT INTO rec_proposals VALUES (?,?,NULL) ON CONFLICT(surface) DO UPDATE '
                'SET payload=excluded.payload,built_at=excluded.built_at', list(raw.items()))
        for owner in publication.OWNERS.values():
            revision = ready(owner)
            if owner.source == 'model_listening_monitor':
                with conn:
                    conn.execute('CREATE TRIGGER controlled_actual_trial_cutover BEFORE INSERT ON '+owner.tables[1]+
                        " BEGIN SELECT RAISE(ABORT,'controlled trial cutover interruption'); END")
                try:
                    owner.publish(conn, revision=revision)
                except sqlite3.IntegrityError as exc:
                    assert 'controlled trial cutover interruption' in str(exc)
                else:
                    raise AssertionError('Interrupted trial publication was accepted')
                assert not conn.execute('SELECT 1 FROM '+owner.tables[0]+' WHERE revision=?', (revision,)).fetchone()
                with conn:
                    conn.execute('DROP TRIGGER controlled_actual_trial_cutover')
                def race(connection, header, value):
                    publication.verify_rows(connection, header, value)
                    with other.conn:
                        other.conn.execute('UPDATE rec_proposals SET built_at=built_at WHERE surface=?', (owner.source,))
                try:
                    replace(owner, verify_rows=race).publish(conn, revision=revision)
                except MigrationError:
                    pass
                else:
                    raise AssertionError('Stale trial publication was accepted')
                revision = ready(owner)
            publish('controlled', owner, revision)
            assert owner.value(other.conn, revision) == raw[owner.source]
            assert other.rec_proposals.get_proposals(owner.source) == values[owner.source]
            assert owner.capture(conn)['built_at'] is None
        owner = publication.OWNERS['live-trial']
        changed = {**values[owner.source], 'phase':'retained', 'reason':'controlled user decision', 'ended_at':200}
        started = time.monotonic()
        store.rec_proposals.put_proposals(owner.source, changed, 2000000000)
        report['compatible_writer_seconds'] = time.monotonic()-started
        assert owner.selected(other.conn) is None
        assert publication.OWNERS['album-assignment'].selected(other.conn) is not None
        def fail(*args, **kwargs):
            assert not conn.in_transaction
            raise OSError('controlled trial preparation failure')
        try:
            replace(owner.worker, prepare=fail).step(conn)
        except OSError:
            pass
        failed = owner.worker.progress(conn)
        assert failed.status == 'failed'
        started = time.monotonic()
        store.init_schema(defer_metadata=True)
        regular.migrate(conn)
        report['installed_ddl_while_failed_seconds'] = time.monotonic()-started
        assert owner.worker.progress(conn) == failed
        owner.worker.step(conn, retry=True)
        publish('continuing-decision', owner, ready(owner))
        assert other.rec_proposals.get_proposals(owner.source) == changed
        assert not conn.execute('SELECT 1 FROM rec_model_selection_runs WHERE id=?', (missing,)).fetchone()
        regular.validate(conn)
        report.update(status='passed', interrupted_cutover_rolled_back=True, stale_cutover_rejected=True,
            unknown_runs_not_admitted=True, failed_preparation_retried_explicitly=True, preopened_readers_use_publications=True)
    except BaseException as exc:
        report['error'] = repr(exc)
        raise
    finally:
        conn.set_trace_callback(None)
        conn.rollback()
        try:
            with conn:
                for source, row in originals.items():
                    if row is None:
                        conn.execute('DELETE FROM rec_proposals WHERE surface=?', (source,))
                    else:
                        conn.execute('INSERT INTO rec_proposals VALUES (?,?,?) ON CONFLICT(surface) DO UPDATE '
                            'SET payload=excluded.payload,built_at=excluded.built_at', (source, *row))
            assert before == {table:fingerprint(conn, table, order) for table, order in protected.items()}
            assert history == list(map(tuple, conn.execute('SELECT * FROM schema_migrations ORDER BY version')))
            report['sources_references_history_preserved_after_restore'] = True
        finally:
            report_path.write_text(json.dumps(report, indent=2))
            other.conn.close()
            conn.close()
    print(json.dumps({key:value for key, value in report.items() if key != 'batches'}, indent=2))


if __name__ == '__main__':
    check(Path(sys.argv[1]), Path(sys.argv[2]))
