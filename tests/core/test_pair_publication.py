"""Pair cutover retains complete ranked projections and comparison custody."""
from dataclasses import replace
import json
from pathlib import Path
import sqlite3

import pytest

from tests.core.test_ranked_state_preparation import seed
from tests.store_fixture_support import (
    guarded_numbered_store as installed,
    publication_consumer_reads,
)
from yt_playlist.core import group_evidence, recommendation_custody
from yt_playlist.core.migrations import MigrationError, regular
from yt_playlist.core.preparation import pair_publication as publication, ranked_state as preparation
from yt_playlist.core.store import Store


pytestmark = pytest.mark.usefixtures('publication_consumer_reads')


@pytest.fixture(autouse=True)
def disable_periodic_library_sync():
    """Publication owners do not construct an HTTP application."""


def roots(store, run_id):
    from tests.group_support import publish as publish_groups, recordings
    from tests.repos.test_typed_recommendation_publication import plan, publish
    tracks = recordings([('House', 12, .3)])
    with store.conn:
        store.conn.executemany('INSERT INTO recordings(identity_key) VALUES (?)', [(track.key,) for track in tracks])
    groups = publish_groups(store, tracks)
    generation = publish(store, plan(store)).generation_id
    context = {'group_generation':groups['generation'], 'musical_groups':[
        {'version':groups['groups'][0]['version'], 'group_id':groups['groups'][0]['group_id']}]}
    key = f'proposal:model_pair:{run_id}:incumbent:radio:context'
    with store.conn.atomic():
        group_evidence.retain(store.conn, 'comparison', key, context, 1000)
        recommendation_custody.retain(store.conn, 'comparison', key, generation)
        group_evidence.retain_unknown_context(store.conn, 'comparison',
            f'proposal:model_pair:{run_id}:challenger:mixes:context', 'original:unavailable', 'missing_context', 1000)
    return key


def prepare(conn):
    preparation.JOBS['pairs'].install(conn)
    preparation.ROOT_JOB.install(conn)
    for job in (preparation.JOBS['pairs'], preparation.ROOT_JOB):
        for _ in range(2000):
            if job.step(conn, batch_size=8).status == 'prepared':
                break
        else:
            pytest.fail('Pair preparation did not finish bounded fixture')


def ready(conn, size=8):
    prepare(conn)
    for _ in range(1000):
        stage = publication.OWNER.step(conn, batch_size=size)
        if stage.complete:
            return stage.id
    pytest.fail('Pair publication map did not finish bounded fixture')


@pytest.mark.parametrize('installed', [79, 93, 94], indirect=True)
def test_preopened_readers_keep_compressed_originals_ranked_members_modes_and_roots(
        installed):
    store, _ = installed
    originals, run_id = seed(store)
    roots(store, run_id)
    owner = publication.OWNER
    other = Store(store._db_path)
    try:
        other.init_schema(defer_metadata=True)
        stage = ready(store.conn)
        owner.publish(store.conn, stage=stage)
        assert owner.selected(other.conn) == stage
        assert owner.stage(store.conn, stage).copied == 6
        assert owner.root_stage(store.conn, stage)['copied'] >= 5
        for surface, raw in originals.items():
            if not surface.startswith('model_pair:'):
                continue
            assert owner.value(other.conn, stage, surface) == raw
            assert other.rec_proposals.get_proposals(surface) == json.loads(raw)
            assert owner.capture(other.conn, stage, surface)['built_at'] is None
        contexts = store.conn.execute('SELECT o.source_snapshot FROM '+owner.table('root_map')+
            ' m JOIN '+preparation.ORIGINALS+' o ON o.owner=m.owner AND o.source_key=m.source_key AND o.digest=m.source_digest '
            "WHERE m.stage=? AND m.owner='unresolved_group_contexts'", (stage,)).fetchall()
        assert any(json.loads(row[0])['source_context'] == 'original:unavailable' for row in contexts)
        regular.migrate(other.conn)
    finally:
        other.conn.close()
    reopened = Store(store._db_path)
    try:
        reopened.init_schema(defer_metadata=True)
        assert reopened.rec_proposals.get_proposals(f'model_pair:{run_id}:challenger:radio') == json.loads(originals[f'model_pair:{run_id}:challenger:radio'])
    finally:
        reopened.conn.close()


@pytest.mark.parametrize('installed', [93], indirect=True)
@pytest.mark.parametrize('failure', ['vector', 'display', 'run', 'context', 'root', 'root_original', 'missing_root', 'missing_output'])
def test_unverified_projection_or_retention_state_cannot_publish(installed, failure):
    store, _ = installed
    _, run_id = seed(store)
    roots(store, run_id)
    owner = publication.OWNER
    prepare(store.conn)
    with store.conn:
        if failure == 'vector':
            store.conn.execute("UPDATE prepared_model_pairs_modes SET centroid=X'0000000000000000'")
        elif failure == 'display':
            store.conn.execute("UPDATE prepared_model_pairs_parts SET filename='/missing' WHERE kind='display'")
        elif failure == 'run':
            store.conn.execute('UPDATE prepared_model_pairs_generations SET run_ref=NULL,run_resolution=\'missing_run\'')
        elif failure == 'context':
            # The source has no such context; the typed relation must be empty.
            row = preparation.head(store.conn, f'model_pair:{run_id}:challenger:mixes')
            store.conn.execute("INSERT INTO prepared_model_pairs_contexts VALUES (?,?, 'context','invented')",
                (row['surface'], row['preparation_revision']))
        elif failure == 'root':
            store.conn.execute('UPDATE '+preparation.ROOTS+" SET source_context='invented'")
        elif failure == 'root_original':
            store.conn.execute('UPDATE '+preparation.ORIGINALS+" SET source_snapshot='{}'")
        elif failure == 'missing_root':
            store.conn.execute('DELETE FROM '+preparation.ROOTS)
        elif failure == 'missing_output':
            store.conn.execute('DELETE FROM prepared_model_pairs_heads')
    for _ in range(100):
        stage = owner.step(store.conn, batch_size=8)
        if stage.complete:
            break
    assert stage.complete
    with pytest.raises((MigrationError, OSError, ValueError)):
        owner.publish(store.conn, stage=stage.id)
    assert owner.selected(store.conn) is None
    regular.migrate(store.conn)


@pytest.mark.parametrize('installed', [93], indirect=True)
def test_released_roots_withdraw_selection_keep_old_pins_and_reconcile_unknown_context(installed):
    store, _ = installed
    _, run_id = seed(store)
    key = roots(store, run_id)
    owner = publication.OWNER
    first = ready(store.conn)
    owner.publish(store.conn, stage=first)
    assert owner.root_stage(store.conn, first)['copied'] >= 5
    contexts = store.conn.execute('SELECT o.source_snapshot FROM '+owner.table('root_map')+
        ' m JOIN '+preparation.ORIGINALS+' o ON o.owner=m.owner AND o.source_key=m.source_key AND o.digest=m.source_digest '
        "WHERE m.stage=? AND m.owner='unresolved_group_contexts'", (first,)).fetchall()
    assert any(json.loads(row[0])['source_context'] == 'original:unavailable' for row in contexts)
    with store.conn.atomic():
        group_evidence.release(store.conn, 'comparison', key)
        recommendation_custody.release(store.conn, 'comparison', key)
        group_evidence.retain_unknown_context(store.conn, 'comparison', key, 'changed:unavailable', 'missing_context', 2000)
    assert owner.selected(store.conn) is None
    with pytest.raises(MigrationError, match='retention roots'):
        owner.publish(store.conn, stage=first)
    second = ready(store.conn)
    owner.publish(store.conn, stage=second)
    assert owner.root_stage(store.conn, second)['copied'] < owner.root_stage(store.conn, first)['copied']
    assert store.conn.execute('SELECT count(*) FROM '+owner.table('root_map')+' WHERE stage=?', (first,)).fetchone()[0] >= 5
    retained_key = owner.pin_key(first, key)
    assert store.conn.execute('SELECT 1 FROM group_evidence_generations WHERE kind=\'comparison\' AND external_key=?', (retained_key,)).fetchone()
    assert store.conn.execute('SELECT 1 FROM recommendation_generation_refs WHERE kind=\'comparison\' AND external_key=?', (retained_key,)).fetchone()
    version = store.conn.execute('SELECT external_version FROM group_evidence_versions WHERE kind=\'comparison\' AND external_key=?', (retained_key,)).fetchone()[0]
    with pytest.raises(sqlite3.IntegrityError, match='FOREIGN KEY'):
        with store.conn:
            store.conn.execute('DELETE FROM group_version_aliases WHERE external_version=?', (version,))
    with pytest.raises(sqlite3.IntegrityError, match='future cleanup'):
        with store.conn:
            store.conn.execute('DELETE FROM '+preparation.ORIGINALS)
    assert owner.verify_roots(store.conn, second)


@pytest.mark.parametrize('installed', [93], indirect=True)
def test_source_snapshot_fence_and_real_prepared_writer_remain_compatible(installed):
    store, _ = installed
    originals, run_id = seed(store)
    owner, surface = publication.OWNER, f'model_pair:{run_id}:challenger:mixes'
    first = ready(store.conn)
    owner.publish(store.conn, stage=first)
    value = {'run_id':run_id, 'data':{'items':[], 'caption':'Changed choice'}}
    with store.read_snapshot() as snapshot:
        assert snapshot.store.rec_proposals.get_proposals(surface) == json.loads(originals[surface])
        with store.conn.atomic():
            store.rec_proposals.publish_prepared_home(json.dumps(value), 17, surface=surface)
        assert not snapshot.publish(lambda:None)
    assert owner.selected(store.conn) is None
    owner.publish(store.conn, stage=ready(store.conn))
    assert store.rec_proposals.get_proposals(surface) == value
    assert owner.value(store.conn, first, surface) == originals[surface]


@pytest.mark.parametrize('installed', [93], indirect=True)
def test_interrupted_publication_and_root_race_cannot_select_partial_custody(installed, monkeypatch):
    store, _ = installed
    _, run_id = seed(store)
    key = roots(store, run_id)
    owner = publication.OWNER
    stage = ready(store.conn)
    with store.conn:
        store.conn.execute('CREATE TRIGGER controlled_pair_interruption BEFORE INSERT ON '+owner.table('head')+
            " BEGIN SELECT RAISE(ABORT,'controlled pair interruption'); END")
    with pytest.raises(sqlite3.IntegrityError, match='controlled pair interruption'):
        owner.publish(store.conn, stage=stage)
    for suffix, field in (('generations', 'revision'), ('root_publications', 'stage')):
        assert not store.conn.execute('SELECT 1 FROM '+owner.table(suffix)+' WHERE '+field+'=?', (stage,)).fetchone()
    with store.conn:
        store.conn.execute('DROP TRIGGER controlled_pair_interruption')
    verify = type(owner).verify_roots
    def race(self, conn, identity):
        proof = verify(self, conn, identity)
        with store.conn.atomic():
            group_evidence.release(conn, 'comparison', key)
        return proof
    monkeypatch.setattr(type(owner), 'verify_roots', race)
    with pytest.raises(MigrationError):
        owner.publish(store.conn, stage=stage)
    assert owner.selected(store.conn) is None
    monkeypatch.setattr(type(owner), 'verify_roots', verify)
    owner.publish(store.conn, stage=ready(store.conn))


@pytest.mark.parametrize('installed', [93], indirect=True)
def test_missing_display_or_output_files_and_failed_roots_do_not_block_ddl(
        installed):
    store, _ = installed
    _, run_id = seed(store)
    key = roots(store, run_id)
    owner, surface = publication.OWNER, f'model_pair:{run_id}:challenger:radio'
    stage = ready(store.conn)
    owner.publish(store.conn, stage=stage)
    for table, predicate in (('parts', " AND kind='display'"), ('outputs', '')):
        filename = store.conn.execute('SELECT filename FROM prepared_model_pairs_'+table+' WHERE surface=?'+predicate+' LIMIT 1', (surface,)).fetchone()[0]
        Path(filename).unlink()
        with pytest.raises(OSError):
            owner.publish(store.conn, stage=stage)
        store.init_schema(defer_metadata=True)
        regular.migrate(store.conn)
        publication.repair(store.conn, surface)
        owner.publish(store.conn, stage=stage)
    with store.conn.atomic():
        group_evidence.release(store.conn, 'comparison', key)
        group_evidence.retain_unknown_context(store.conn, 'comparison', key, 'next:unavailable', 'missing_context', 2000)
    def fail(*args, **kwargs):
        raise OSError('controlled root preparation failure')
    with pytest.raises(OSError, match='controlled root preparation failure'):
        replace(preparation.ROOT_JOB, copy=fail).step(store.conn)
    failed = preparation.ROOT_JOB.progress(store.conn)
    assert failed.status == 'failed'
    store.init_schema(defer_metadata=True)
    regular.migrate(store.conn)
    assert preparation.ROOT_JOB.progress(store.conn) == failed
    assert preparation.ROOT_JOB.step(store.conn, retry=True).status == 'prepared'
    owner.publish(store.conn, stage=ready(store.conn))
