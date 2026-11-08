"""Home cutover must select exact files and retain their real evidence roots."""
from dataclasses import replace
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import json
import sqlite3
import threading
import time

import pytest

from tests.core.test_home_output_preparation import seed, root_rows
from tests.core.test_predecessor_writes import consumer_installed as installed
from tests.independent_schema_support import independent_regular_ddl
from tests.store_fixture_support import publication_consumer_reads
from yt_playlist.core import group_evidence, recommendation_custody
from yt_playlist.core.migrations import MigrationError, registry, regular, runner
from yt_playlist.core.preparation import home_outputs as preparation, home_publication as publication
from yt_playlist.core.store import Store


@pytest.fixture(autouse=True)
def disable_periodic_library_sync():
    pass


def test_home_publication_input_keeps_installed_history_and_guards(_bounded_installed_schema):
    source = _bounded_installed_schema(90).source
    assert source.conn.execute('PRAGMA query_only').fetchone()[0] == 1
    assert list(map(tuple, source.conn.execute('SELECT version,name FROM schema_migrations ORDER BY version'))) == [
        (entry.version, entry.name) for entry in registry.MIGRATIONS[:90]]
    assert source._snapshot_revisions.conn is source.conn
    assert not source.conn.execute('PRAGMA foreign_key_check').fetchone()


def prepare(conn):
    for job in (preparation.JOB, preparation.ROOT_JOB):
        job.install(conn)
        for _ in range(2000):
            if job.step(conn, batch_size=8).status == 'prepared':
                break
        else:
            pytest.fail('Home preparation did not finish bounded fixture')


def ready(conn, size=8):
    prepare(conn)
    for _ in range(2000):
        stage = publication.OWNER.step(conn, batch_size=size)
        if stage.complete:
            return stage.id
    pytest.fail('Home publication did not finish bounded fixture')


def real_roots(store):
    from tests.group_support import publish as publish_groups, recordings
    from tests.repos.test_typed_recommendation_publication import plan, publish
    tracks = recordings([('House', 12, .3)])
    with store.conn:
        store.conn.executemany('INSERT INTO recordings(identity_key) VALUES (?)', [(track.key,) for track in tracks])
    groups = publish_groups(store, tracks)
    generation = publish(store, plan(store)).generation_id
    context = {'group_generation': groups['generation'], 'musical_groups': [
        {'version': groups['groups'][0]['version'], 'group_id': groups['groups'][0]['group_id']}]}
    with store.conn.atomic():
        group_evidence.retain(store.conn, 'prepared', 'proposal:home_prepared:context', context, 1000)
        recommendation_custody.retain(store.conn, 'prepared', 'proposal:home_prepared:recommendations', generation)


@pytest.mark.parametrize('installed', [79, 90], indirect=True)
def test_preopened_and_pinned_readers_select_exact_files_without_legacy_payload(installed):
    store, _ = installed
    originals = seed(store)
    real_roots(store)
    roots = root_rows(store.conn)
    other = Store(store._db_path)
    try:
        other.init_schema(defer_metadata=True)
        with other.read_snapshot() as snapshot:
            pinned = snapshot.store
            assert pinned.rec_proposals.get_proposals('home_prepared') == json.loads(originals['home_prepared'])
            stage = ready(store.conn, size=1)
            publication.OWNER.publish(store.conn, stage=stage)
            assert publication.OWNER.selected(pinned.conn) is None
        def deny_source(action, owner, column, *_):
            return sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_READ and owner == 'rec_proposals' and column == 'payload' else sqlite3.SQLITE_OK
        with other.read_view() as selected:
            selected.conn.set_authorizer(deny_source)
            try:
                assert publication.OWNER.selected(selected.conn) == stage
                for surface, raw in originals.items():
                    assert selected.rec_proposals._proposal_json(surface) == raw
                    assert selected.rec_proposals.has_proposals(surface)
                assert selected.rec_proposals.get_proposals('home_tuned') is None
                assert not selected.rec_proposals.has_proposals('home_tuned')
                assert selected.rec_proposals.group_checkpoint() == {
                    'schema': 2, 'generation': 'original-generation', 'mix_regions': 1}
            finally:
                selected.conn.set_authorizer(None)
        assert root_rows(store.conn) == roots
        assert publication.OWNER.root_stage(store.conn, stage)['copied'] == sum(map(len, roots.values()))
        assert not store.conn.execute('PRAGMA foreign_key_check').fetchone()
        regular.validate(other.conn)
    finally:
        other.conn.close()


@pytest.mark.parametrize('installed', [90], indirect=True)
@pytest.mark.parametrize('failure', ['metadata', 'file', 'policy', 'extra_policy', 'root', 'root_original', 'missing_root', 'missing_output'])
def test_missing_or_incorrect_documents_and_custody_cannot_publish(installed, failure):
    store, _ = installed
    seed(store)
    prepare(store.conn)
    with store.conn:
        if failure == 'metadata':
            store.conn.execute('UPDATE '+preparation.HEAD+' SET mix_regions=99')
        elif failure == 'file':
            store.conn.execute('UPDATE '+preparation.PARTS+" SET filename='/missing'")
        elif failure == 'policy':
            store.conn.execute('DELETE FROM '+preparation.POLICIES)
        elif failure == 'extra_policy':
            store.conn.execute('INSERT INTO '+preparation.POLICIES+
                " SELECT surface,generation,'invented',NULL,'missing_policy' FROM "+preparation.HEAD+' LIMIT 1')
        elif failure == 'root':
            store.conn.execute('UPDATE '+preparation.ROOTS+" SET source_context='invented'")
        elif failure == 'root_original':
            store.conn.execute('UPDATE '+preparation.ORIGINALS+" SET source_snapshot='{}'")
        elif failure == 'missing_root':
            store.conn.execute('DELETE FROM '+preparation.ROOTS)
        elif failure == 'missing_output':
            store.conn.execute('DELETE FROM '+preparation.CURRENT)
    for _ in range(100):
        stage = publication.OWNER.step(store.conn)
        if stage.complete:
            break
    with pytest.raises((MigrationError, OSError, ValueError)):
        publication.OWNER.publish(store.conn, stage=stage.id)
    assert publication.OWNER.selected(store.conn) is None


@pytest.mark.parametrize('installed', [90], indirect=True)
def test_continuing_writes_withdraw_selection_and_keep_real_retention_pins(installed):
    store, _ = installed
    originals = seed(store)
    real_roots(store)
    owner = publication.OWNER
    first = ready(store.conn)
    owner.publish(store.conn, stage=first)
    key = 'proposal:home_prepared:context'
    retained = owner.pin_key(first, key)
    with store.conn.atomic():
        group_evidence.release(store.conn, 'prepared', key)
        recommendation_custody.release(store.conn, 'prepared', 'proposal:home_prepared:recommendations')
    assert owner.selected(store.conn) is None
    assert store.conn.execute('SELECT 1 FROM group_evidence_generations WHERE external_key=?', (retained,)).fetchone()
    assert store.conn.execute('SELECT 1 FROM group_evidence_versions WHERE external_key=?', (retained,)).fetchone()
    assert store.conn.execute('SELECT 1 FROM recommendation_generation_refs WHERE external_key=?',
        (owner.pin_key(first, 'proposal:home_prepared:recommendations'),)).fetchone()
    with pytest.raises(sqlite3.IntegrityError, match='future cleanup'):
        with store.conn.atomic():
            group_evidence.release(store.conn, 'prepared', retained)
    store.rec_proposals.put_proposals('home_prepared', {'tracks': [{'title': 'continuing update'}]}, 2000)
    assert store.rec_proposals.get_proposals('home_prepared')['tracks'][0]['title'] == 'continuing update'
    owner.publish(store.conn, stage=ready(store.conn))
    assert store.rec_proposals.get_proposals('home_prepared')['tracks'][0]['title'] == 'continuing update'
    assert owner.value(store.conn, first, 'home_prepared') == originals['home_prepared']


@pytest.mark.parametrize('installed', [90], indirect=True)
def test_interrupted_publication_and_source_race_cannot_select_partial_state(installed, monkeypatch):
    store, _ = installed
    seed(store)
    owner = publication.OWNER
    stage = ready(store.conn)
    with store.conn:
        store.conn.execute('CREATE TRIGGER controlled_home_interruption BEFORE INSERT ON '+owner.table('head')+
            " BEGIN SELECT RAISE(ABORT,'controlled Home interruption'); END")
    with pytest.raises(sqlite3.IntegrityError, match='controlled Home interruption'):
        owner.publish(store.conn, stage=stage)
    for suffix, field in (('generations', 'revision'), ('root_publications', 'stage')):
        assert not store.conn.execute('SELECT 1 FROM '+owner.table(suffix)+' WHERE '+field+'=?', (stage,)).fetchone()
    with store.conn:
        store.conn.execute('DROP TRIGGER controlled_home_interruption')
    verify = type(owner).verify_roots
    def race(self, conn, identity):
        proof = verify(self, conn, identity)
        with conn.atomic():
            group_evidence.release(conn, 'prepared', 'proposal:home_prepared:choice_context')
        return proof
    monkeypatch.setattr(type(owner), 'verify_roots', race)
    with pytest.raises(MigrationError, match='reconciled retention roots|changed'):
        owner.publish(store.conn, stage=stage)
    assert owner.selected(store.conn) is None
    monkeypatch.setattr(type(owner), 'verify_roots', verify)
    owner.publish(store.conn, stage=ready(store.conn))


@pytest.mark.parametrize('installed', [90], indirect=True)
def test_missing_file_repair_leaves_regular_schema_independent(
        installed, independent_regular_ddl, publication_consumer_reads):
    store, _ = installed
    originals = seed(store)
    owner = publication.OWNER
    stage = ready(store.conn)
    owner.publish(store.conn, stage=stage)
    filename = store.conn.execute('SELECT filename FROM '+preparation.PARTS+" WHERE surface='home_prepared'").fetchone()[0]
    Path(filename).unlink()
    assert store.rec_proposals.get_proposals('home_prepared') is None
    with pytest.raises(OSError):
        owner.publish(store.conn, stage=stage)
    regular.migrate(store.conn)
    publication.repair(store.conn, 'home_prepared')
    assert store.rec_proposals._proposal_json('home_prepared') == originals['home_prepared']
    owner.publish(store.conn, stage=stage)


@pytest.mark.parametrize('installed', [90], indirect=True)
def test_failed_roots_keep_independent_ddl_and_restart_retry_usable(
        installed, independent_regular_ddl, publication_consumer_reads):
    store, _ = installed
    seed(store)
    owner = publication.OWNER
    owner.publish(store.conn, stage=ready(store.conn))
    with store.conn.atomic():
        group_evidence.release(store.conn, 'prepared', 'proposal:home_prepared:choice_context')
    def failed(*args):
        raise OSError('controlled Home roots failure')
    with pytest.raises(OSError, match='controlled Home roots failure'):
        replace(preparation.ROOT_JOB, copy=failed, delete=failed).step(store.conn)
    checkpoint = preparation.ROOT_JOB.progress(store.conn)
    assert checkpoint.status == 'failed'
    regular.migrate(store.conn)
    assert preparation.ROOT_JOB.progress(store.conn) == checkpoint
    reopened = Store(store._db_path)
    try:
        reopened.init_schema(defer_metadata=True)
        preparation.ROOT_JOB.step(reopened.conn, retry=True)
        owner.publish(reopened.conn, stage=ready(reopened.conn))
    finally:
        reopened.conn.close()


@pytest.mark.parametrize('installed', [90], indirect=True)
def test_interrupted_root_copy_resumes_from_another_connection(installed):
    store, _ = installed
    seed(store)
    real_roots(store)
    prepare(store.conn)
    owner = publication.OWNER
    for _ in range(20):
        checkpoint = owner.step(store.conn, batch_size=1)
        if checkpoint.outputs_complete and checkpoint.roots_copied == 1:
            break
    assert not checkpoint.complete and checkpoint.roots_copied == 1
    captured = tuple(owner.root_stage(store.conn, checkpoint.id))
    other = Store(store._db_path)
    try:
        other.init_schema(defer_metadata=True)
        with store.conn:
            store.conn.execute('CREATE TRIGGER controlled_home_root_copy BEFORE INSERT ON '+owner.table('root_map')+
                " BEGIN SELECT RAISE(ABORT,'controlled root copy'); END")
        with pytest.raises(sqlite3.IntegrityError, match='controlled root copy'):
            owner.step(other.conn, batch_size=1)
        assert tuple(owner.root_stage(store.conn, checkpoint.id)) == captured
        with store.conn:
            store.conn.execute('DROP TRIGGER controlled_home_root_copy')
        for _ in range(100):
            resumed = owner.step(other.conn, batch_size=1)
            assert resumed.id == checkpoint.id
            if resumed.complete:
                break
        assert resumed.complete
        owner.publish(other.conn, stage=resumed.id)
        assert owner.selected(store.conn) == resumed.id
    finally:
        other.conn.close()


@pytest.mark.parametrize('installed', [90], indirect=True)
def test_slow_file_verification_leaves_writer_and_regular_schema_usable(installed, monkeypatch, independent_regular_ddl):
    store, _ = installed
    seed(store)
    stage = ready(store.conn)
    blocked, release = threading.Event(), threading.Event()
    read = publication.files._read
    def slow(*args, **kwargs):
        if not blocked.is_set():
            assert not store.conn.in_transaction
            blocked.set()
            assert release.wait(10)
        return read(*args, **kwargs)
    monkeypatch.setattr(publication.files, '_read', slow)
    other = Store(store._db_path)
    try:
        other.init_schema(defer_metadata=True)
        with ThreadPoolExecutor(max_workers=1) as pool:
            publisher = pool.submit(publication.OWNER.publish, store.conn, stage=stage)
            assert blocked.wait(2)
            phases = [('foreground_write', time.perf_counter())]
            try:
                other.rec_proposals.put_proposals('home_prepared', {'new': 'while files are slow'}, 3000)
                phases.append(('regular_schema', time.perf_counter()))
                regular.migrate(other.conn)
                phases.append(('foreground_readback', time.perf_counter()))
                assert other.rec_proposals.get_proposals('home_prepared') == {'new': 'while files are slow'}
            finally:
                finished = time.perf_counter()
                release.set()
                timings = {name: ended - started for (name, started), (_, ended)
                           in zip(phases, phases[1:] + [('released', finished)])}
                timings['blocked_reader_progress'] = finished - phases[0][1]
                print(json.dumps({'home_publication_concurrency_seconds': timings}, sort_keys=True))
            with pytest.raises(MigrationError):
                publisher.result(2)
        assert publication.OWNER.selected(other.conn) is None
        publication.OWNER.publish(other.conn, stage=ready(other.conn))
    finally:
        other.conn.close()


@pytest.mark.parametrize('installed', [90], indirect=True)
def test_native_upgrade_preserves_archives_and_prefers_native_writes(installed, tmp_path):
    store, _ = installed
    originals = seed(store)
    real_roots(store)
    owner = publication.OWNER
    stage = ready(store.conn)
    owner.publish(store.conn, stage=stage)
    upgraded = Store(tmp_path/'home-publication-upgrade.db')
    try:
        store.conn.backup(upgraded.conn)
        runner.migrate(upgraded.conn, registry=registry.MIGRATIONS)
        upgraded.init_schema(defer_metadata=True)
        assert owner.selected(upgraded.conn) is None
        for surface, raw in originals.items():
            assert owner.value(upgraded.conn, stage, surface) == raw
            assert upgraded.rec_proposals.get_proposals(surface) == json.loads(raw)
        upgraded.rec_proposals.put_proposals('home_prepared', {'native': True}, 3000)
        assert upgraded.rec_proposals.get_proposals('home_prepared') == {'native': True}
        regular.migrate(upgraded.conn)
    finally:
        upgraded.conn.close()
