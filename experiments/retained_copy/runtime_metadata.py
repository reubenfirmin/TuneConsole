"""Isolated #681/#682 experiment; the baseline is opened read-only throughout.

The new candidate retains the installed DDL/history but leaves obsolete metadata
owners empty. Their nullable credit/identifier custody links become NULL; their
actual current facts are retained. General compatibility retirement stays #626.
"""
import argparse
from contextlib import closing
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import tempfile
import threading
import time

from yt_playlist.core import metadata_native, metadata_runtime_files, retained_copy
from yt_playlist.core.preparation import metadata_runtime as preparation
from yt_playlist.core.store import Store
from yt_playlist.core.work_diagnostics import MaintenanceDiagnostics

RETIRED = frozenset(('metadata_deliveries','metadata_observations','metadata_legacy_records',
    'metadata_capture_heads','recording_metadata_selections','entry_metadata_selections','metadata_choice_heads',
    'metadata_conflicts','metadata_conflict_candidates','metadata_hints','provider_attempts','provider_attempt_fields',
    'provider_state_fields','native_metadata_observations','musical_source_observations'))
NULLS = {'artist_credit_observations': {'metadata_delivery_id'},
         'recording_identifiers': {'delivery_id','observation_id'}}
Q = retained_copy.quote


def reader(path):
    host = Store.__new__(Store)
    host._db_path, host._evidence_key = path, '681-read-only-experiment'
    host.transient_radio_playlists = set()
    host._analytics_readers = threading.BoundedSemaphore(4)
    host.maintenance_diagnostics = MaintenanceDiagnostics()
    host._schema_compatibility = True
    return host._make_reader()


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def baseline(path, now):
    from yt_playlist.rec import (artist_model, daily_model_selection, embedding_training,
                                 embedding_vectors, musical_preparation, progressive, vibe_preparation)
    store = reader(path)
    try:
        store.conn.execute('PRAGMA query_only=ON')
        baskets = embedding_training.canonical_baskets(store.rec_evidence.rec_baskets())
        native = hashlib.sha256()
        for (key,) in store.conn.execute('SELECT source_key FROM rec_musical_sources ORDER BY source_key'):
            native.update(fingerprint((key, metadata_native.source_values(store.conn, key, max_bytes=8*1024*1024))).encode())
        groups = store.taste_groups.head(members=True)
        prep = progressive.Preparation(store)
        return {'catalog':fingerprint([tuple(row) for row in store.conn.execute('SELECT * FROM catalog_tracks ORDER BY id')]),
            'training_baskets':(len(baskets),fingerprint(baskets)), 'native_model_inputs':native.hexdigest(),
            'artist_baskets':fingerprint(embedding_training.canonical_baskets(artist_model.artist_baskets(store))),
            'content_inputs':fingerprint(embedding_vectors.content_inputs(store)),
            'history_bounds':list(store.rec_evidence.evaluation_history_bounds()),
            'daily_comparison_due':daily_model_selection.due(store,now),
            'full_retraining_due':prep.full_due(now), 'musical_work_due':musical_preparation.due(store),
            'group_work_due':prep.groups.due(now), 'vibe_work_due':vibe_preparation.due(store),
            'groups':fingerprint(groups), 'group_count':len(groups['groups']) if groups else 0,
            'queues':{table:list(map(tuple,store.conn.execute('SELECT * FROM '+table+' ORDER BY 1')))
                for table in ('rec_change_queue','rec_preparation','rec_content_pending','rec_musical_pending','rec_vibe_pending','enrichment_provider_jobs')},
            'settings':fingerprint(list(map(tuple,store.conn.execute('SELECT * FROM settings ORDER BY key'))))}
    finally:
        store.conn.close()


def compact(source, target):
    with closing(sqlite3.connect(source.as_uri()+'?mode=ro',uri=True)) as conn:
        retained_copy._configure(conn)
        schema = conn.execute("SELECT type,name,sql FROM sqlite_schema WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%'").fetchall()
        tables = [name for kind,name,_ in schema if kind=='table']
        with closing(sqlite3.connect(target)) as destination:
            for kind, name, sql in schema:
                if kind == 'table':
                    destination.execute(sql)
            destination.commit()
        conn.execute('ATTACH DATABASE ? AS compacted', (str(target),))
        conn.execute('BEGIN')
        for table in tables:
            if table in RETIRED:
                continue
            fields, _ = retained_copy._fields(conn, table)
            columns = ','.join(map(Q,fields))
            expressions = ','.join('NULL' if field in NULLS.get(table, ()) else Q(field) for field in fields)
            conn.execute('INSERT INTO compacted.'+Q(table)+'('+columns+') SELECT '+expressions+' FROM main.'+Q(table))
        conn.execute('DELETE FROM compacted.sqlite_sequence')
        conn.execute('INSERT INTO compacted.sqlite_sequence SELECT * FROM main.sqlite_sequence')
        for pragma in ('user_version','application_id'):
            conn.execute('PRAGMA compacted.'+pragma+'='+str(conn.execute('PRAGMA main.'+pragma).fetchone()[0]))
        conn.commit()
        # All owners other than explicitly retired history and nullable custody
        # links retain every value, SQLite type, hidden rowid and primary key.
        for table in tables:
            if table not in RETIRED and table not in NULLS:
                retained_copy._equal_rows(conn, table, '1')
        conn.execute('DETACH DATABASE compacted')
    with closing(sqlite3.connect(target)) as conn:
        retained_copy._configure(conn)
        for kind in ('index','view','trigger'):
            for actual,name,sql in schema:
                if actual==kind:
                    conn.execute(sql)
        conn.commit()
        if conn.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
            raise ValueError('Candidate integrity check failed')
        violations = conn.execute('PRAGMA foreign_key_check').fetchmany(10)
        if violations:
            raise ValueError(('Candidate FK violations',violations))
        sizes = dict(conn.execute('SELECT name,sum(pgsize) FROM dbstat GROUP BY name'))
        return {'sql_bytes':target.stat().st_size, 'freelist_pages':conn.execute('PRAGMA freelist_count').fetchone()[0],
                'object_bytes':sizes, 'retired_rows':{table:conn.execute('SELECT count(*) FROM '+table).fetchone()[0] for table in RETIRED}}


def exercise(candidate):
    """Continue real-library writes in a disposable copy; retain only the report."""
    from yt_playlist.core import catalog, metadata_runtime as runtime
    candidate = Path(candidate).resolve(strict=True)
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix='tuneconsole-runtime-acceptance-') as temporary:
        path = Path(temporary)/'state.db'
        with closing(sqlite3.connect(candidate.as_uri()+'?mode=ro', uri=True)) as source, closing(sqlite3.connect(path)) as target:
            before_queues = {table:list(source.execute('SELECT * FROM '+table+' ORDER BY 1')) for table in
                ('rec_change_queue','rec_content_pending','rec_musical_pending','rec_vibe_pending','enrichment_provider_jobs')}
            source.backup(target)
        shutil.copytree(candidate.with_name(candidate.name+'.metadata-runtime'), path.with_name(path.name+'.metadata-runtime'))
        store = Store(path)
        try:
            phase = time.monotonic()
            store.init_schema()
            initialization_seconds = time.monotonic()-phase
            assert {table:list(map(tuple, store.conn.execute('SELECT * FROM '+table+' ORDER BY 1')))
                    for table in before_queues} == before_queues, 'Opening scheduled new work'
            track = store.conn.execute('SELECT e.id,e.video_id,r.id recording_id,r.identity_key FROM catalog_entries e '
                'JOIN recordings r ON r.id=e.recording_id WHERE e.video_id IS NOT NULL ORDER BY e.id LIMIT 1').fetchone()
            track = dict(track)
            before_sql = store.conn.execute('PRAGMA page_count').fetchone()[0]*store.conn.execute('PRAGMA page_size').fetchone()[0]
            before_rows = store.conn.execute('SELECT count(*) FROM enrichment_provider_state').fetchone()[0]
            now = time.time()
            for number in range(100):
                fields = {'bpm':120+number}
                run = 'actual-library-'+str(number)
                captured, _ = store.enrichment_state.capture_provider_result(track['id'], '681-experiment', fields,
                    run_id=run, now=now+number/1000)
                store.enrichment.apply_provider_fields(track['id'], '681-experiment', fields,
                    providers=('681-experiment',), delivery_id=captured)
                store.enrichment_state.record_enrichment_attempt(track['identity_key'], '681-experiment', fields,
                    video_id=track['video_id'], run_id=run, delivery_id=captured, now=now+number/1000)
            with store.conn:
                catalog.choose_field(store.conn, track['id'], 'bpm', 130)
            store.enrichment.apply_provider_fields(track['id'],'681-experiment',{'bpm':140},providers=('681-experiment',))
            assert store.conn.execute('SELECT bpm FROM catalog_tracks WHERE id=?',(track['id'],)).fetchone()[0] == 130
            after_sql = store.conn.execute('PRAGMA page_count').fetchone()[0]*store.conn.execute('PRAGMA page_size').fetchone()[0]
            after_rows = store.conn.execute('SELECT count(*) FROM enrichment_provider_state').fetchone()[0]
            assert after_rows == before_rows+1
            retired_rows = {table:store.conn.execute('SELECT count(*) FROM '+table).fetchone()[0] for table in RETIRED}
            assert not any(retired_rows.values()), retired_rows
            alternatives = metadata_runtime_files.directory(store.conn)/metadata_runtime_files.relative(
                'alternatives',(track['recording_id'],runtime.provider_id(store.conn,'681-experiment',create=False)),float(now))
            json_stats = [item.stat() for item in alternatives.parent.glob('*.json')]
        finally:
            store.conn.close()
        reopened = Store(path)
        try:
            reopened.init_schema()
            with reopened.conn:
                catalog.withdraw_choice(reopened.conn,track['id'],'bpm')
            assert reopened.conn.execute('SELECT bpm FROM catalog_tracks WHERE id=?',(track['id'],)).fetchone()[0] == 140
            assert reopened.enrichment_state.enrichment_provider_state(track['recording_id'])['681-experiment']['status'] == 'found'
            assert not reopened.conn.execute('PRAGMA foreign_key_check').fetchone()
        finally:
            reopened.conn.close()
        return {'dataset':'disposable copy of '+str(candidate), 'schema_initialization_seconds':initialization_seconds,
            'initialization_queues_unchanged':True, 'provider_results':100, 'provider_state_rows_added':after_rows-before_rows,
            'sql_page_bytes_added':after_sql-before_sql, 'alternative_documents':len(json_stats),
            'alternative_bytes':sum(item.st_size for item in json_stats), 'retired_rows':retired_rows,
            'manual_override_and_reset_after_reopen':True, 'seconds':time.monotonic()-started,
            'web_application_started':False}


def retire_locators(candidate, report_path):
    """Verify #682's physical cleanup on a disposable actual-library copy."""
    from yt_playlist.core.preparation import metadata_locators
    from yt_playlist.core import catalog
    candidate = Path(candidate).resolve(strict=True)
    started, now = time.monotonic(), time.time()
    with candidate.open('rb') as stream:
        source_digest = hashlib.file_digest(stream, 'sha256').hexdigest()
    report = {'source': str(candidate), 'source_sql_bytes': candidate.stat().st_size}
    with tempfile.TemporaryDirectory(prefix='tuneconsole-locator-retirement-') as temporary:
        path = Path(temporary) / 'state.db'
        with closing(sqlite3.connect(candidate.as_uri()+'?mode=ro', uri=True)) as origin, closing(sqlite3.connect(path)) as target:
            origin.backup(target)
        shutil.copytree(candidate.with_name(candidate.name+'.metadata-runtime'), path.with_name(path.name+'.metadata-runtime'))
        store = Store(path)
        try:
            store.init_schema()
            before = baseline(path, now)
            histories = {table: list(map(tuple, store.conn.execute('SELECT * FROM '+table+' ORDER BY version')))
                         for table in ('schema_migrations', 'schema_changes')}
            report['before_columns'] = [row[1] for row in store.conn.execute('PRAGMA table_info(metadata_deliveries)')]
            with store.conn:
                store.conn.execute('BEGIN')
                manifest = metadata_runtime_files.export(store.conn, Path(temporary) / 'recovery')
            phase = time.monotonic()
            report['retirement'] = metadata_locators.retire(store.conn, manifest)
            report['retirement_seconds'] = time.monotonic() - phase
            report['after_columns'] = [row[1] for row in store.conn.execute('PRAGMA table_info(metadata_deliveries)')]
            assert not metadata_locators.COLUMNS.intersection(report['after_columns'])
            assert not metadata_locators.REMOVED.intersection(row[0] for row in store.conn.execute('SELECT name FROM sqlite_schema'))
            assert histories == {table: list(map(tuple, store.conn.execute('SELECT * FROM '+table+' ORDER BY version')))
                                 for table in histories}
            report['histories_unchanged'] = True
            after = baseline(path, now)
            report['input_differences'] = [key for key in before if before[key] != after[key]]
            assert not report['input_differences'], report['input_differences']
            store.init_schema()
            entry = store.conn.execute('SELECT id FROM catalog_entries WHERE video_id IS NOT NULL ORDER BY id LIMIT 1').fetchone()[0]
            fields = {'bpm': 141}
            delivery, _ = store.enrichment_state.capture_provider_result(entry, '682-retirement', fields, run_id='continued')
            store.enrichment.apply_provider_fields(entry, '682-retirement', fields, providers=('682-retirement',), delivery_id=delivery)
            with store.conn:
                catalog.choose_field(store.conn, entry, 'bpm', 139)
            assert store.conn.execute('SELECT bpm FROM catalog_tracks WHERE id=?', (entry,)).fetchone()[0] == 139
            store.conn.close()
            store = Store(path)
            store.init_schema()
            with store.conn:
                catalog.withdraw_choice(store.conn, entry, 'bpm')
            assert store.conn.execute('SELECT bpm FROM catalog_tracks WHERE id=?', (entry,)).fetchone()[0] == 141
            assert not store.conn.execute('PRAGMA foreign_key_check').fetchone()
            report['continued_enrichment_and_reopened_choice_reset'] = True
            with store.conn:
                store.conn.execute('BEGIN')
                manifest = metadata_runtime_files.export(store.conn, Path(temporary) / 'continued-recovery')
            moved = Path(temporary) / 'relocated.db'
            with closing(sqlite3.connect(moved)) as target:
                store.conn.backup(target)
            with closing(sqlite3.connect(moved)) as target:
                metadata_runtime_files.restore(target, manifest)
                report['relocated_members'] = metadata_runtime_files.verify_backup(target, manifest)
            report['baseline'] = before
        finally:
            store.conn.close()
    with candidate.open('rb') as stream:
        assert hashlib.file_digest(stream, 'sha256').hexdigest() == source_digest
    report['source_sha256_unchanged'] = source_digest
    report['seconds'] = time.monotonic() - started
    Path(report_path).write_text(json.dumps(report, indent=2, sort_keys=True)+'\n')
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path)
    parser.add_argument('output', type=Path)
    args = parser.parse_args()
    source, output = args.source.resolve(strict=True), args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    started, now = time.monotonic(), time.time()
    report = {'source':str(source), 'source_sql_bytes':source.stat().st_size, 'retired_owners':sorted(RETIRED),
              'nulled_custody_links':{key:sorted(value) for key,value in NULLS.items()}, 'phases':{}}
    with source.open('rb') as stream:
        original_digest = hashlib.file_digest(stream, 'sha256').hexdigest()
    target = output/'candidate'/'state.db'
    target.parent.mkdir()
    try:
        print('Reading actual-library baseline', flush=True)
        phase = time.monotonic()
        report['baseline'] = baseline(source, now)
        report['phases']['baseline_seconds'] = time.monotonic()-phase
        with tempfile.TemporaryDirectory(prefix='.metadata-work-', dir=output) as temporary:
            work = Path(temporary)/'state.db'
            phase = time.monotonic()
            with closing(sqlite3.connect(source.as_uri()+'?mode=ro',uri=True)) as origin, closing(sqlite3.connect(work)) as destination:
                origin.backup(destination)
            report['phases']['isolated_backup_seconds'] = time.monotonic()-phase
            store = Store(work)
            try:
                store.init_schema()
                phase = time.monotonic()
                progress = Path(temporary)/'progress.json'
                stage = -1
                while True:
                    result = preparation.step(store.conn, progress, batch_size=128)
                    if result['stage'] != stage:
                        stage = result['stage']
                        print('Prepared stages:', stage, result['counts'], flush=True)
                    if result['complete']:
                        break
                preparation.activate(store.conn, progress)
                report['preparation'] = result
                report['phases']['preparation_and_cutover_seconds'] = time.monotonic()-phase
            finally:
                store.conn.close()
            print('Creating runtime-only SQL copy', flush=True)
            phase = time.monotonic()
            report['candidate'] = compact(work, target)
            report['phases']['fresh_copy_and_verification_seconds'] = time.monotonic()-phase
            shutil.move(str(work.with_name(work.name+'.metadata-runtime')), str(target.with_name(target.name+'.metadata-runtime')))
        print('Checking application inputs on the candidate', flush=True)
        phase = time.monotonic()
        after = baseline(target, now)
        differences = [key for key in report['baseline'] if report['baseline'][key] != after[key]]
        report['differences'] = differences
        if differences:
            raise ValueError('Application inputs changed: '+repr(differences))
        report['phases']['candidate_baseline_seconds'] = time.monotonic()-phase
        root = target.with_name(target.name+'.metadata-runtime')
        stats = [path.stat() for path in root.rglob('*.json')]
        report['json'] = {'files':len(stats),'bytes':sum(stat.st_size for stat in stats),
                          'allocated_bytes':sum(stat.st_blocks*512 for stat in stats)}
        with source.open('rb') as stream:
            if hashlib.file_digest(stream,'sha256').hexdigest() != original_digest:
                raise ValueError('Baseline database changed')
        report['source_sha256_unchanged'] = original_digest
        report['seconds'] = time.monotonic()-started
        report['candidate_path'] = str(target)
        report['app_started'] = False
        (output/'report.json').write_text(json.dumps(report, indent=2, sort_keys=True)+'\n')
        print(json.dumps({key:report[key] for key in ('candidate_path','json','phases','seconds','differences')},sort_keys=True),flush=True)
        print('SQL bytes:',report['candidate']['sql_bytes'],flush=True)
    except BaseException:
        shutil.rmtree(target.parent)
        raise


if __name__=='__main__':
    main()
