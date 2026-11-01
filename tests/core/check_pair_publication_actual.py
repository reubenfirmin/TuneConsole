"""Measure paired projection/root cutover using an isolated actual catalog."""
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import time

from yt_playlist.core import group_evidence, recommendation_custody
from yt_playlist.core.migrations import MigrationError, regular
from yt_playlist.core.preparation import pair_publication as publication, ranked_state as preparation
from yt_playlist.core.store import Store
from yt_playlist.rec import comparison_storage


def fingerprint(conn, table, order, *, roots=False):
    digest, count = hashlib.sha256(), 0
    for row in conn.execute('SELECT * FROM main.'+table+
        (" WHERE NOT(kind='comparison' AND external_key GLOB 'preparation:model-pair:*')" if roots else '')+' ORDER BY '+order):
        digest.update(json.dumps(tuple(row), ensure_ascii=False, default=lambda value:{'blob':value.hex()}).encode())
        count += 1
    return count, digest.hexdigest()


def check(path, report_path):
    assert path.parent == report_path.parent == Path('/tmp') and path.name.startswith('tuneconsole-')
    report = {'status':'failed', 'database':str(path), 'python':sys.version, 'sqlite':sqlite3.sqlite_version,
        'dataset':'Existing isolated genuine v93 library. Actual pair presence is recorded separately. Six controlled projections use 8192 actual recording identities, one actual policy and existing group/recommendation custody. The controlled run/mode IDs remain unadmitted; original comparison source roots are released after publication to check retained pins.',
        'limits':'No native writes, HTTP, providers, runtime workers, fitting or full historical conversion. Controlled source rows/roots are removed afterward; additive immutable preparation maps and pins remain retained.',
        'batches':[], 'maps':[], 'publications':{}}
    store, other = Store(path), Store(path)
    conn, owner = store.conn, publication.OWNER
    run = 9223372036854775807
    sources = [f'model_pair:{run}:{arm}:{kind}' for arm in ('incumbent', 'challenger') for kind in ('mixes', 'albums', 'radio')]
    keys = [f'proposal:model_pair:{run}:incumbent:radio:context', f'proposal:model_pair:{run}:challenger:mixes:context']
    assert not conn.execute('SELECT 1 FROM rec_model_selection_runs WHERE id=?', (run,)).fetchone()
    assert not conn.execute('SELECT 1 FROM taste_modes WHERE mode_id=?', (run,)).fetchone()
    for source in sources:
        assert not conn.execute('SELECT 1 FROM rec_proposals WHERE surface=?', (source,)).fetchone()
    for table in preparation.ROOT_JOB.keys:
        for key in keys:
            assert not conn.execute('SELECT 1 FROM '+table+" WHERE kind='comparison' AND external_key=?", (key,)).fetchone()
    protected = {'settings':'key', 'rec_proposals':'surface', 'identities':'id', 'playlist_scopes':'ref',
        'catalog_entries':'id', 'recordings':'id', 'youtube_items':'video_id', 'rec_model_selection_runs':'id',
        'taste_modes':'mode_id', 'group_generations':'id', 'group_versions':'id', 'group_version_aliases':'external_version',
        'recommendation_generations':'id'}
    before = {table:fingerprint(conn, table, order) for table, order in protected.items()}
    root_before = {table:fingerprint(conn, table, ','.join(columns), roots=True)
        for table, columns in preparation.ROOT_JOB.keys.items()}
    history = list(map(tuple, conn.execute('SELECT * FROM schema_migrations ORDER BY version')))
    reservations = []
    def trace(sql):
        if sql == 'BEGIN IMMEDIATE':
            reservations.append(time.monotonic())
    def ready():
        for worker in (owner.worker, preparation.ROOT_JOB):
            for _ in range(3000):
                reservations.clear()
                started = time.monotonic()
                progress = worker.step(conn, batch_size=128 if worker is owner.worker else 8)
                ended = time.monotonic()
                report['batches'].append({'job':worker.name, 'seconds':ended-started,
                    'writer_seconds':ended-reservations[-1] if reservations else 0})
                if progress.status == 'prepared':
                    break
            else:
                raise AssertionError('Pair preparation did not finish')
        for _ in range(1000):
            started = time.monotonic()
            stage = owner.step(conn, batch_size=8)
            report['maps'].append({'seconds':time.monotonic()-started,
                'stage':stage.id, 'roots_copied':stage.roots_copied, 'complete':stage.complete})
            if stage.complete:
                return stage.id
        raise AssertionError('Comparison publication maps did not finish')
    def publish(name, stage):
        reservations.clear()
        started = time.monotonic()
        owner.publish(conn, stage=stage)
        ended = time.monotonic()
        assert owner.selected(other.conn) == stage
        report['publications'][name] = {'stage':stage, 'seconds':ended-started,
            'writer_seconds':ended-reservations[-1]}
    try:
        store.init_schema(defer_metadata=True)
        other.init_schema(defer_metadata=True)
        conn.set_trace_callback(trace)
        owner.worker.install(conn)
        preparation.ROOT_JOB.install(conn)
        report['installed_pair_documents'] = conn.execute("SELECT count(*) FROM rec_proposals WHERE surface GLOB 'model_pair:*'").fetchone()[0]
        publish('installed', ready())
        actual_keys = [row[0] for row in conn.execute('SELECT identity_key FROM recordings ORDER BY id LIMIT 8192')]
        assert len(actual_keys) == 8192
        policy = conn.execute('SELECT version FROM recommendation_policies ORDER BY created_at DESC,version LIMIT 1').fetchone()[0]
        items = [{'key':key, 'score':1/(index+1), 'pool_policy':policy} for index, key in enumerate(actual_keys)]
        radio = {'policy_version':policy, 'encoding_model':None,
            'modes':[{'mode_id':run, 'space':'', 'centroid':[1., -0., .25], 'label':'Controlled captured mode'}],
            'bundles':{'captured':{'wheelhouse':items}, '_radio':{'space':'', 'members':{'captured':actual_keys},
                'scores':{key:.5 for key in actual_keys}}, '_ppr':{'captured':list(reversed(actual_keys))}}}
        raw = {}
        for source in sources:
            value = radio if source.endswith(':radio') else {'items':[], 'context':{}, 'caption':'Controlled 東京'}
            raw[source] = (comparison_storage.encode(run, value) if ':challenger:' in source
                else json.dumps({'run_id':run, 'data':value}, ensure_ascii=False, indent=2))
        report['controlled_bytes'] = {source:len(value.encode()) for source, value in raw.items()}
        reference = conn.execute('SELECT g.external_key,x.external_version,t.external_key FROM generation_groups x '
            'JOIN group_publications p ON p.generation_id=x.generation_id JOIN group_generations g ON g.id=x.generation_id '
            'JOIN taste_groups t ON t.id=x.group_id ORDER BY x.generation_id,x.position LIMIT 1').fetchone()
        assert reference is not None
        context = {'group_generation':reference[0], 'musical_groups':[{'version':reference[1], 'group_id':reference[2]}]}
        recommendation = conn.execute("SELECT id FROM recommendation_generations WHERE status='published' ORDER BY id LIMIT 1").fetchone()[0]
        with conn.atomic():
            conn.executemany('INSERT INTO rec_proposals VALUES (?,?,NULL)', list(raw.items()))
            group_evidence.retain(conn, 'comparison', keys[0], context, 2000000000)
            recommendation_custody.retain(conn, 'comparison', keys[0], recommendation)
            group_evidence.retain_unknown_context(conn, 'comparison', keys[1], 'controlled:original:unavailable', 'missing_context', 2000000000)
        stage = ready()
        with conn:
            conn.execute('CREATE TRIGGER controlled_actual_pair_cutover BEFORE INSERT ON '+owner.table('head')+
                " BEGIN SELECT RAISE(ABORT,'controlled pair cutover interruption'); END")
        try:
            owner.publish(conn, stage=stage)
        except sqlite3.IntegrityError as exc:
            assert 'controlled pair cutover interruption' in str(exc)
        else:
            raise AssertionError('Interrupted comparison publication was accepted')
        for suffix, field in (('generations', 'revision'), ('root_publications', 'stage')):
            assert not conn.execute('SELECT 1 FROM '+owner.table(suffix)+' WHERE '+field+'=?', (stage,)).fetchone()
        with conn:
            conn.execute('DROP TRIGGER controlled_actual_pair_cutover')
        publish('six-projections-65536-candidates', stage)
        report['retained_root_links'] = owner.root_stage(conn, stage)['copied']
        for source in sources:
            assert owner.value(other.conn, stage, source) == raw[source]
            assert other.rec_proposals.get_proposals(source) == json.loads(raw[source])
            header = owner.capture(conn, stage, source)
            assert header['built_at'] is None and header['run_resolution'] == 'missing_run'
            if source.endswith(':radio'):
                assert conn.execute('SELECT count(*) FROM prepared_model_pairs_candidates WHERE surface=? AND preparation_revision=?',
                    (source, header['preparation_revision'])).fetchone()[0] == 32768
        source = sources[-1]
        filename = conn.execute("SELECT filename FROM prepared_model_pairs_parts WHERE surface=? AND preparation_revision=? AND kind='display' LIMIT 1",
            (source, owner.capture(conn, stage, source)['preparation_revision'])).fetchone()[0]
        Path(filename).unlink()
        started = time.monotonic()
        store.init_schema(defer_metadata=True)
        regular.migrate(conn)
        report['installed_ddl_with_missing_file_seconds'] = time.monotonic()-started
        publication.repair(conn, source)
        publish('repaired-display', stage)
        with conn.atomic():
            group_evidence.release(conn, 'comparison', keys[0])
            recommendation_custody.release(conn, 'comparison', keys[0])
            group_evidence.retain_unknown_context(conn, 'comparison', keys[0], 'controlled:next:unavailable', 'missing_context', 2000000001)
        assert owner.selected(other.conn) is None
        retained_key = owner.pin_key(stage, keys[0])
        for table in ('group_evidence_generations', 'group_evidence_versions', 'recommendation_generation_refs'):
            assert conn.execute('SELECT 1 FROM '+table+" WHERE kind='comparison' AND external_key=?", (retained_key,)).fetchone()
        def fail(*args, **kwargs):
            raise OSError('controlled actual root preparation failure')
        try:
            replace(preparation.ROOT_JOB, copy=fail).step(conn)
        except OSError:
            pass
        failed = preparation.ROOT_JOB.progress(conn)
        assert failed.status == 'failed'
        started = time.monotonic()
        store.init_schema(defer_metadata=True)
        regular.migrate(conn)
        report['installed_ddl_while_root_failed_seconds'] = time.monotonic()-started
        assert preparation.ROOT_JOB.progress(conn) == failed
        preparation.ROOT_JOB.step(conn, retry=True)
        publish('released-source-roots', ready())
        changed = {'run_id':run, 'data':{'items':[], 'caption':'Compatible writer'}}
        started = time.monotonic()
        with conn.atomic():
            store.rec_proposals.publish_prepared_home(json.dumps(changed), 2000000002, surface=sources[0])
        report['compatible_writer_seconds'] = time.monotonic()-started
        assert owner.selected(other.conn) is None
        publish('continuing-source-edit', ready())
        assert other.rec_proposals.get_proposals(sources[0]) == changed
        assert not conn.execute('SELECT 1 FROM rec_model_selection_runs WHERE id=?', (run,)).fetchone()
        assert not conn.execute('SELECT 1 FROM taste_modes WHERE mode_id=?', (run,)).fetchone()
        regular.validate(conn)
        report.update(status='passed', interrupted_cutover_rolled_back=True, released_source_roots_keep_pins=True,
            exact_files_recovered=True, failed_roots_retried_explicitly=True, preopened_readers_use_publications=True)
    except BaseException as exc:
        report['error'] = repr(exc)
        raise
    finally:
        conn.set_trace_callback(None)
        conn.rollback()
        try:
            with conn.atomic():
                conn.executemany('DELETE FROM rec_proposals WHERE surface=?', [(source,) for source in sources])
                for key in keys:
                    group_evidence.release(conn, 'comparison', key)
                    recommendation_custody.release(conn, 'comparison', key)
            assert before == {table:fingerprint(conn, table, order) for table, order in protected.items()}
            assert root_before == {table:fingerprint(conn, table, ','.join(columns), roots=True)
                for table, columns in preparation.ROOT_JOB.keys.items()}
            assert history == list(map(tuple, conn.execute('SELECT * FROM schema_migrations ORDER BY version')))
            report['original_sources_roots_references_history_preserved_after_restore'] = True
        finally:
            report_path.write_text(json.dumps(report, indent=2))
            other.conn.close()
            conn.close()
    print(json.dumps({key:value for key, value in report.items() if key not in ('batches', 'maps')}, indent=2))


if __name__ == '__main__':
    check(Path(sys.argv[1]), Path(sys.argv[2]))
