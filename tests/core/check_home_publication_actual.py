"""Measure Home publication using actual identities in an isolated v90 fixture."""
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from actual_publication_timing import WriterTimings
from tests.core.test_home_publication import real_roots
from yt_playlist.core import home_outputs, recommendation_outputs as files
from yt_playlist.core.migrations import registry, regular, runner
from yt_playlist.core.preparation import home_outputs as preparation, home_publication as publication
from yt_playlist.core.store import Store


def fingerprint(conn, owner):
    digest = hashlib.sha256()
    keys = sorted((row[5], row[1]) for row in conn.execute('PRAGMA main.table_info('+owner+')') if row[5])
    count = 0
    for row in conn.execute('SELECT * FROM main.'+owner+' ORDER BY '+','.join(key for _, key in keys)):
        digest.update(json.dumps(tuple(row), ensure_ascii=False, default=lambda value:bytes(value).hex()).encode())
        count += 1
    return count, digest.hexdigest()


def check(actual_path, fixture_path, report_path):
    workspace = Path.cwd()/'.pytest_cache'
    assert all(path.resolve().is_relative_to(workspace) for path in (actual_path, fixture_path, report_path))
    assert actual_path != fixture_path and not fixture_path.exists()
    fixture_path.parent.mkdir(parents=True, exist_ok=True)
    report = {'status': 'failed', 'actual_database': str(actual_path), 'fixture': str(fixture_path),
        'python': sys.version, 'sqlite': sqlite3.sqlite_version, 'batches': [], 'maps': [], 'publications': {},
        'dataset': 'Read-only actual-library snapshot. Native Home presence is recorded separately. A genuine v90 fixture uses 8192 exact actual recording identities in controlled Home documents across all ten surfaces, 600 controlled policy references, and independently published fixture group/recommendation custody.',
        'limits': 'Controlled displays and fixture retention roots are not actual historical Home artifacts. No native writes, HTTP, providers, complete historical conversion or production fitting.'}
    actual = sqlite3.connect(actual_path.resolve().as_uri()+'?mode=ro', uri=True)
    actual.row_factory = sqlite3.Row
    protected = ('settings', 'schema_migrations', 'recordings', 'home_display_publications', 'home_display_heads',
        'home_display_files', 'group_generations', 'recommendation_generations')
    before = {owner: fingerprint(actual, owner) for owner in protected}
    report['actual_home_publications'] = before['home_display_publications'][0]
    report['actual_home_heads'] = before['home_display_heads'][0]
    keys = [row[0] for row in actual.execute('SELECT identity_key FROM main.recordings ORDER BY id LIMIT 8192')]
    assert len(keys) == 8192
    documents = {surface: json.dumps({'tracks': [{'key': key, 'pool_policy': 'policy-'+str(index % 600)}
        for index, key in enumerate(keys)]}, ensure_ascii=False, indent=2) if surface == 'home_prepared'
        else json.dumps({'schema': 2, 'generation': 'controlled-actual-keys', 'menus': {'wheelhouse': keys[:12]}})
        if surface == 'adaptive_group_preview' else '{ "caption": "Controlled 東京" }'
        for surface in home_outputs.SURFACES}
    report['actual_recording_keys'] = len(keys)
    report['controlled_source_bytes'] = sum(len(raw.encode()) for raw in documents.values())
    store, second, owner = Store(fixture_path), None, publication.OWNER
    timings = WriterTimings(field='writer_seconds')
    timed = timings.timed
    report['timing'] = 'Maximum writer window from BEGIN IMMEDIATE through commit/rollback completion on both watched connections.'
    def ready():
        for job in (preparation.JOB, preparation.ROOT_JOB):
            job.install(store.conn)
            for _ in range(1000):
                current, duration = timed(lambda: job.step(store.conn, batch_size=8))
                report['batches'].append({'job': job.name, **duration})
                if current.status == 'prepared':
                    break
            else:
                raise AssertionError('Home fixture preparation did not finish')
        for _ in range(1000):
            stage, duration = timed(lambda: owner.step(store.conn, batch_size=8))
            report['maps'].append(duration)
            if stage.complete:
                return stage.id
        raise AssertionError('Home fixture map did not finish')
    def publish(label, stage):
        _, report['publications'][label] = timed(lambda: owner.publish(store.conn, stage=stage))
        assert owner.selected(second.conn) == stage
    try:
        runner.migrate(store.conn, registry=registry.MIGRATIONS[:90])
        store.init_schema(defer_metadata=True)
        with store.conn:
            store.conn.executemany('INSERT INTO main.rec_proposals VALUES (?,?,NULL)', documents.items())
        real_roots(store)
        history = list(map(tuple, store.conn.execute('SELECT * FROM schema_migrations ORDER BY version')))
        second = Store(fixture_path)
        second.init_schema(defer_metadata=True)
        timings.watch(store.conn)
        timings.watch(second.conn)
        stage = ready()
        publish('controlled-actual-identities', stage)
        report['retained_root_links'] = owner.root_stage(store.conn, stage)['copied']
        for surface, raw in documents.items():
            assert second.rec_proposals._proposal_json(surface) == raw
        filename = store.conn.execute('SELECT filename FROM '+preparation.PARTS+
            " WHERE surface='home_prepared' ORDER BY position LIMIT 1").fetchone()[0]
        Path(filename).unlink()
        assert second.rec_proposals.get_proposals('home_prepared') is None
        _, report['regular_admission_missing_file'] = timed(lambda: second.init_schema(defer_metadata=True))
        publication.repair(store.conn, 'home_prepared')
        publish('repaired-files', stage)
        _, report['compatible_writer'] = timed(lambda: second.rec_proposals.put_proposals(
            'home_prepared', {'tracks': [{'key': keys[-1], 'title': 'Continuing writer'}]}, 2000))
        assert owner.selected(store.conn) is None
        def failed(*args):
            raise OSError('controlled Home file failure')
        retain = files.publish
        files.publish = failed
        try:
            try:
                preparation.JOB.step(store.conn, batch_size=8)
            except OSError:
                pass
            else:
                raise AssertionError('Home file failure ignored')
        finally:
            files.publish = retain
        checkpoint = preparation.JOB.progress(store.conn)
        assert checkpoint.status == 'failed'
        _, report['regular_admission_failed_preparation'] = timed(lambda: second.init_schema(defer_metadata=True))
        regular.migrate(second.conn)
        assert preparation.JOB.progress(store.conn) == checkpoint
        preparation.JOB.step(second.conn, batch_size=8, retry=True)
        stage = ready()
        publish('continuing-writer', stage)
        assert second.rec_proposals.get_proposals('home_prepared')['tracks'][0]['key'] == keys[-1]
        assert history == list(map(tuple, store.conn.execute('SELECT * FROM schema_migrations ORDER BY version')))
        report.update(status='passed', exact_original_files_and_roots=True, continuing_writes_reconciled=True,
            failed_preparation_preserved_across_regular_admission=True, history_exact=True)
    except BaseException as exc:
        report['error'] = repr(exc)
        raise
    finally:
        timings.close()
        report['actual_sources_exact'] = before == {owner: fingerprint(actual, owner) for owner in protected}
        actual.close()
        if second is not None:
            second.conn.close()
        store.conn.close()
        report_path.write_text(json.dumps(report, indent=2)+'\n')
    assert report['actual_sources_exact']
    return report


if __name__ == '__main__':
    result = check(*(Path(argument).resolve() for argument in sys.argv[1:]))
    print(json.dumps({key: result[key] for key in ('status', 'actual_home_heads', 'actual_recording_keys', 'publications')}))
