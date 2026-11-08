"""Pair cutover retains complete ranked projections and comparison custody."""
from dataclasses import replace
from contextlib import closing, contextmanager
from functools import partial
import json
from pathlib import Path
import sqlite3

import pytest

from tests.core.test_predecessor_writes import consumer_installed as installed
from tests.independent_schema_support import independent_regular_ddl
from tests.core.test_ranked_state_preparation import seed
from tests.genre_catalog_support import bound_catalog
from tests.store_fixture_support import reuse_reporting_reader
from yt_playlist.core import group_evidence, recommendation_custody, schema_inspection
from yt_playlist.core.migrations import MigrationError, regular, runner
from yt_playlist.core.preparation import pair_publication as publication, ranked_state as preparation
from yt_playlist.core.store import Store


@pytest.fixture(autouse=True)
def disable_periodic_library_sync():
    """Publication owners do not construct an HTTP application."""


@pytest.fixture(autouse=True)
def publication_readers(request, monkeypatch):
    if 'installed' not in request.fixturenames:
        yield
        return
    store, _ = request.getfixturevalue('installed')
    # Reporting inside the input publishers can share a real file reader.
    # Explicit preopened readers and pinned publication snapshots stay private.
    with reuse_reporting_reader(store, monkeypatch), schema_inspection.reuse_tables(store.conn):
        yield


@pytest.mark.parametrize('installed', [79, 93, 94], indirect=True)
def test_pair_input_keeps_numbered_history_and_private_snapshot_guards(installed):
    store, version = installed
    template = store._fixture_installed_template
    assert template.source.conn.execute('PRAGMA query_only').fetchone()[0] == 1
    assert store.conn.execute('SELECT max(version) FROM schema_migrations').fetchone()[0] == version
    assert template.revision_guards
    assert store._snapshot_revisions.conn is store.conn
    assert not store.conn.execute('PRAGMA foreign_key_check').fetchone()


def roots(store, run_id, format='radio'):
    from tests.group_support import publish as publish_groups, recordings
    from tests.repos.test_typed_recommendation_publication import plan, publish
    tracks = recordings([('House', 12, .3)])
    with store.conn:
        store.conn.executemany('INSERT INTO recordings(identity_key) VALUES (?)', [(track.key,) for track in tracks])
    groups = publish_groups(store, tracks)
    generation = publish(store, plan(store)).generation_id
    context = {'group_generation':groups['generation'], 'musical_groups':[
        {'version':groups['groups'][0]['version'], 'group_id':groups['groups'][0]['group_id']}]}
    key = f'proposal:model_pair:{run_id}:incumbent:{format}:context'
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


def pair_inputs(store, *, format=None):
    surfaces = ({f'{arm}:{format}' for arm in ('incumbent', 'challenger')}
                if format else {'incumbent:radio', 'challenger:mixes'})
    with pytest.MonkeyPatch.context() as patch, reuse_reporting_reader(store, patch), schema_inspection.reuse_tables(store.conn):
        originals, run_id = seed(store, surfaces=surfaces)
        prepare(store.conn)
    return originals, run_id, None


def pair_root_sources(store, *, format=None):
    surfaces = ({f'{arm}:{format}' for arm in ('incumbent', 'challenger')}
                if format else {'incumbent:radio', 'challenger:mixes'})
    with pytest.MonkeyPatch.context() as patch, reuse_reporting_reader(store, patch), schema_inspection.reuse_tables(store.conn):
        originals, run_id = seed(store, surfaces=surfaces)
        return originals, run_id, roots(store, run_id)


def prepare_root_inputs(store):
    prepare(store.conn)


def pair_template(factory, completed, version, format=None, *, with_roots=False, sources_only=False):
    if with_roots:
        sources = factory(version, f'pair-root-sources:{format}',
            partial(pair_root_sources, format=format), admit=False, completed=completed)
        if sources_only:
            return sources
        return factory(version, f'pair-root-preparation:{format}',
                       prepare_root_inputs, admit=False, completed=sources)
    return factory(version, f'pairs:{format}:False',
                   partial(pair_inputs, format=format), admit=False, completed=completed)


@pytest.fixture
def pair_input_templates(installed, staged_custody_templates):
    store, version = installed
    return partial(pair_template, staged_custody_templates, store._fixture_installed_template, version)


@pytest.fixture
def pair_source_templates(version, _bounded_installed_schema, staged_custody_templates, monkeypatch):
    bound_catalog(monkeypatch)
    return partial(pair_template, staged_custody_templates, _bounded_installed_schema(version), version)


def restore_pair_inputs(installed, staged_custody, pair_input_templates, *, format=None, with_roots=False):
    if with_roots:
        sources = pair_input_templates(format, with_roots=True, sources_only=True)
        staged_custody(installed, f'pair-root-preparation:{format}', prepare_root_inputs,
                       admit=False, completed=sources)
        return sources.result
    return staged_custody(installed, f'pairs:{format}:{with_roots}',
                          partial(pair_inputs, format=format), admit=False)


@contextmanager
def private_pair_input(template, path, version, monkeypatch):
    with template.open(path) as store:
        with reuse_reporting_reader(store, monkeypatch), schema_inspection.reuse_tables(store.conn):
            yield store
        with closing(sqlite3.connect(store._db_path.as_uri() + '?mode=ro', uri=True)) as reader:
            reader.row_factory = sqlite3.Row
            runner.validate_version(reader, version)


@pytest.fixture
def pair_projection(request, tmp_path, format, _bounded_installed_schema, staged_custody_templates, monkeypatch):
    version = request.param
    bound_catalog(monkeypatch)
    template = pair_template(staged_custody_templates, _bounded_installed_schema(version), version, format)
    with private_pair_input(template, tmp_path / 'pair-projection.db', version, monkeypatch) as store:
        yield store, version, template.result


@pytest.fixture
def pair_repair_projection(request, tmp_path, kind, _bounded_installed_schema, staged_custody_templates, monkeypatch):
    version = request.param
    bound_catalog(monkeypatch)
    get = partial(pair_template, staged_custody_templates, _bounded_installed_schema(version), version, 'radio')
    template = get(with_roots=kind == 'roots')
    values = get(with_roots=True, sources_only=True).result if kind == 'roots' else template.result
    with private_pair_input(template, tmp_path / 'pair-repair.db', version, monkeypatch) as store:
        yield store, version, values


@pytest.fixture
def rooted_pair_projection(request, tmp_path, _bounded_installed_schema, staged_custody_templates, monkeypatch):
    version = request.param
    bound_catalog(monkeypatch)
    get = partial(pair_template, staged_custody_templates, _bounded_installed_schema(version), version)
    template = get(with_roots=True)
    with private_pair_input(template, tmp_path / 'rooted-pairs.db', version, monkeypatch) as store:
        yield store, version, get(with_roots=True, sources_only=True).result


@pytest.mark.parametrize('version', [79, 93, 94])
@pytest.mark.parametrize('format', ['mixes', 'albums', 'radio'])
def test_pair_projection_input_keeps_originals_and_unselected_preparation(pair_source_templates, format):
    template = pair_source_templates(format)
    store = template.source
    originals, run_id, key = template.result
    assert run_id and key is None
    assert len(originals) == 2
    assert publication.OWNER.selected(store.conn) is None
    for surface, raw in originals.items():
        head = preparation.head(store.conn, surface)
        assert head['built_at'] is None
        assert preparation.read_original(store.conn, head) == raw
    assert not store.conn.execute('PRAGMA foreign_key_check').fetchone()


@pytest.mark.parametrize('version', [93])
@pytest.mark.parametrize('format', [None, 'radio'])
def test_pair_root_source_input_keeps_real_groups_and_recommendation_custody(pair_source_templates, format):
    template = pair_source_templates(format, with_roots=True, sources_only=True)
    originals, run_id, key = template.result
    source = template.source
    assert len(originals) == 2 and run_id and key
    assert source.conn.execute('PRAGMA query_only').fetchone()[0] == 1
    assert source.conn.execute("SELECT 1 FROM group_evidence_generations WHERE kind='comparison' AND external_key=?", (key,)).fetchone()
    assert source.conn.execute("SELECT 1 FROM recommendation_generation_refs WHERE kind='comparison' AND external_key=?", (key,)).fetchone()
    assert not source.conn.execute('PRAGMA foreign_key_check').fetchone()


@pytest.mark.parametrize('version', [93])
@pytest.mark.parametrize('format', [None, 'radio'])
def test_pair_root_input_keeps_real_custody_and_unselected_preparation(pair_source_templates, format):
    sources = pair_source_templates(format, with_roots=True, sources_only=True)
    template = pair_source_templates(format, with_roots=True)
    store = template.source
    originals, run_id, key = sources.result
    assert len(originals) == 2 and run_id and key
    assert publication.OWNER.selected(store.conn) is None
    assert preparation.ROOT_JOB.progress(store.conn).status == 'prepared'
    assert store.conn.execute("SELECT 1 FROM group_evidence_generations WHERE kind='comparison' AND external_key=?", (key,)).fetchone()
    assert store.conn.execute("SELECT 1 FROM recommendation_generation_refs WHERE kind='comparison' AND external_key=?", (key,)).fetchone()
    assert not store.conn.execute('PRAGMA foreign_key_check').fetchone()


def ready(conn, size=8, *, prepared=False):
    if not prepared:
        prepare(conn)
    for _ in range(1000):
        stage = publication.OWNER.step(conn, batch_size=size)
        if stage.complete:
            return stage.id
    pytest.fail('Pair publication map did not finish bounded fixture')


@pytest.mark.parametrize('pair_projection', [79, 93, 94], indirect=True)
@pytest.mark.parametrize('format', ['mixes', 'albums', 'radio'])
@pytest.mark.parametrize('reader_timing', ['preopened', 'reopened'])
def test_preopened_readers_keep_compressed_originals_ranked_members_modes_and_roots(
        pair_projection, format, reader_timing):
    store, _, (originals, run_id, _) = pair_projection
    owner = publication.OWNER
    other = Store(store._db_path)
    try:
        if reader_timing == 'preopened':
            other.init_schema(defer_metadata=True)
        stage = ready(store.conn, prepared=True)
        owner.publish(store.conn, stage=stage)
        if reader_timing == 'reopened':
            other.init_schema(defer_metadata=True)
        assert owner.selected(other.conn) == stage
        assert owner.stage(store.conn, stage).copied == 2
        for surface, raw in originals.items():
            if not surface.startswith('model_pair:'):
                continue
            assert owner.value(other.conn, stage, surface) == raw
            assert other.rec_proposals.get_proposals(surface) == json.loads(raw)
            assert owner.capture(other.conn, stage, surface)['built_at'] is None
        regular.validate(other.conn)
    finally:
        other.conn.close()


@pytest.mark.parametrize('installed', [93], indirect=True)
@pytest.mark.parametrize('failure', ['vector', 'display', 'run', 'context', 'root', 'root_original', 'missing_root', 'missing_output'])
def test_unverified_projection_or_retention_state_cannot_publish(installed, staged_custody, pair_input_templates, failure):
    store, _ = installed
    with_roots = failure in {'root', 'root_original', 'missing_root'}
    if with_roots:
        _, run_id, _ = restore_pair_inputs(installed, staged_custody, pair_input_templates, with_roots=True)
    else:
        _, run_id = seed(store, surfaces={'incumbent:radio', 'challenger:mixes'})
    owner = publication.OWNER
    if not with_roots:
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


@pytest.mark.parametrize('rooted_pair_projection', [93], indirect=True)
def test_released_roots_withdraw_selection_keep_old_pins_and_reconcile_unknown_context(rooted_pair_projection):
    store, _, (_, run_id, key) = rooted_pair_projection
    owner = publication.OWNER
    first = ready(store.conn, prepared=True)
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


@pytest.mark.parametrize('pair_repair_projection', [93], indirect=True)
@pytest.mark.parametrize('kind', ['display', 'output', 'roots'])
def test_missing_display_or_output_files_and_failed_roots_do_not_block_ddl(
        pair_repair_projection, independent_regular_ddl, kind):
    store, _, (_, run_id, key) = pair_repair_projection
    owner, surface = publication.OWNER, f'model_pair:{run_id}:challenger:radio'
    stage = ready(store.conn, prepared=True)
    owner.publish(store.conn, stage=stage)
    if kind != 'roots':
        table, predicate = ('parts', " AND kind='display'") if kind == 'display' else ('outputs', '')
        filename = store.conn.execute('SELECT filename FROM prepared_model_pairs_'+table+' WHERE surface=?'+predicate+' LIMIT 1', (surface,)).fetchone()[0]
        Path(filename).unlink()
        with pytest.raises(OSError):
            owner.publish(store.conn, stage=stage)
        store.init_schema(defer_metadata=True)
        regular.migrate(store.conn)
        publication.repair(store.conn, surface)
        owner.publish(store.conn, stage=stage)
        return
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
    owner.publish(store.conn, stage=ready(store.conn, prepared=True))
