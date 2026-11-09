"""Home output is replaceable without erasing its actual evidence lineage."""
from contextlib import closing
import json
import sqlite3

import pytest

from tests.genre_catalog_support import bound_catalog
from tests.store_fixture_support import install_numbered_input, without_query_timing
from yt_playlist.core import schema_inspection
from yt_playlist.core import home_outputs as owner, json_cache, recommendation_outputs as files
from yt_playlist.core.migrations import migrate, registry, v091_home_outputs
from yt_playlist.core.migrations.runner import MigrationError
from yt_playlist.core.store import Store
from yt_playlist.core.db_revisions import Revisions


pytestmark = pytest.mark.usefixtures('without_query_timing')


@pytest.fixture(autouse=True)
def disable_periodic_library_sync():
    """Display storage tests have no HTTP app or periodic worker."""


@pytest.fixture(scope='module')
def home_predecessor_schema():
    # v91 owns display conversion. Prior domain conversions and the independent
    # regular registry have separate owners. Supply the completed predecessor.
    with closing(sqlite3.connect(':memory:')) as schema, pytest.MonkeyPatch.context() as patch:
        bound_catalog(patch)
        install_numbered_input(schema, 90)
        schema.execute('PRAGMA query_only=ON')
        yield schema


@pytest.fixture
def predecessor(tmp_path, request, home_predecessor_schema, monkeypatch):
    bound_catalog(monkeypatch)
    store = Store(tmp_path / 'home.db')
    request.addfinalizer(store.conn.close)
    store.conn.execute('PRAGMA synchronous=OFF')
    store.conn.execute('PRAGMA journal_mode=WAL')
    home_predecessor_schema.backup(store.conn)
    store._bind_repositories()
    store._snapshot_revisions = Revisions(store.conn)
    with schema_inspection.reuse_tables(store.conn):
        yield store


def upgrade(store):
    migrate(store.conn, registry=registry.MIGRATIONS[:91])
    store._bind_repositories()
    store._snapshot_revisions = Revisions(store.conn)


@pytest.fixture(scope='module')
def large_display_input(home_predecessor_schema, tmp_path_factory):
    from yt_playlist.core import group_evidence
    store = Store(tmp_path_factory.mktemp('large-home-display') / 'installed.db')
    home_predecessor_schema.backup(store.conn)
    store.conn.execute('PRAGMA journal_mode=WAL')
    store._bind_repositories()
    store._snapshot_revisions = Revisions(store.conn)
    value = {'schema': 2, 'generation': 'original-generation', 'context': {'artifact': 'missing-artifact'},
             'menus': {'wheelhouse': [{'title': 'été' * 400000}], 'explore': []}}
    raw = json.dumps(value)
    with store.conn.atomic():
        store.conn.execute('INSERT INTO rec_proposals VALUES (?,?,?)', ('adaptive_group_preview', raw, None))
        group_evidence.retain_unknown_context(store.conn, 'prepared',
            'proposal:adaptive_group_preview:context', 'missing-artifact', 'missing_context', 123)
    try:
        yield store, value, raw
    finally:
        store.conn.close()


def test_large_display_source_keeps_original_bytes_and_unknown_context(large_display_input):
    store, value, raw = large_display_input
    assert store.conn.execute('SELECT payload FROM rec_proposals').fetchone()[0] == raw
    assert store.rec_proposals.get_proposals('adaptive_group_preview') == value
    assert store.conn.execute('SELECT max(version) FROM schema_migrations').fetchone()[0] == 90
    assert not store.conn.execute('PRAGMA foreign_key_check').fetchall()


@pytest.fixture(scope='module')
def large_display_cutover(large_display_input):
    predecessor, value, _raw = large_display_input
    upgrade(predecessor)
    return predecessor, value


def test_registered_cutover_preserves_large_display_unknown_time_and_explicit_roots(large_display_cutover):
    predecessor, value = large_display_cutover
    assert predecessor.rec_proposals.get_proposals('adaptive_group_preview') == value
    assert predecessor.rec_proposals.group_checkpoint() == {'schema': 2, 'generation': 'original-generation', 'mix_regions': 1}
    assert predecessor.conn.execute('SELECT built_at FROM home_display_publications').fetchone()[0] is None
    assert predecessor.conn.execute('SELECT count(*) FROM home_display_files').fetchone()[0] > 1
    assert predecessor.conn.execute('SELECT max(byte_size) FROM home_display_files').fetchone()[0] <= files.MAX_BYTES
    unknown = predecessor.conn.execute('SELECT external_key,source_context,reason FROM unresolved_group_contexts').fetchone()
    assert tuple(unknown) == ('home-output:1:context', 'missing-artifact', 'missing_context')
    assert not predecessor.conn.execute("SELECT 1 FROM group_evidence_owners WHERE external_key LIKE 'proposal:%'").fetchone()
    assert not predecessor.conn.execute('PRAGMA foreign_key_check').fetchall()


def test_fresh_home_display_reopen_retains_the_exact_large_value(large_display_cutover):
    predecessor, value = large_display_cutover
    reopened = Store(predecessor._db_path)
    try:
        reopened.init_schema(defer_metadata=True)
        assert reopened.rec_proposals.get_proposals('adaptive_group_preview') == value
    finally:
        reopened.conn.close()


def test_policy_references_use_only_declared_context_and_offer_fields(predecessor):
    predecessor.rec_ledger.record_serving_policy('real-policy', {'algorithm': 'saved'}, 1)
    predecessor.rec_proposals.put_proposals('home_prepared', {
        'unrelated': {'model_version': 'unrelated-name'}, 'title': 'abcdef0123456789',
        'menus': [{'protos': [{'tracks': [{'pool_policy': 'real-policy'}]}]}]}, 12)
    upgrade(predecessor)
    assert [tuple(row) for row in predecessor.conn.execute('SELECT raw_key,policy_version,resolution_reason FROM home_display_policies')] == [
        ('real-policy', 'real-policy', None)]
    with pytest.raises(sqlite3.IntegrityError), predecessor.conn.atomic():
        predecessor.conn.execute("DELETE FROM recommendation_policies WHERE version='real-policy'")
    with pytest.raises(ValueError, match='unknown policy'):
        predecessor.rec_proposals.put_proposals('home_prepared', {'protos': [{'tracks': [{'pool_policy': 'missing'}]}]}, 13)
    assert predecessor.rec_proposals.get_proposals('home_prepared')['title'] == 'abcdef0123456789'


@pytest.mark.parametrize('native', [False, True])
def test_compressed_display_preserves_actual_roots_policies_and_metadata(predecessor, native):
    from tests.core.test_home_publication import real_roots

    real_roots(predecessor)
    generation = predecessor.conn.execute('SELECT generation_id FROM recommendation_generation_refs LIMIT 1').fetchone()[0]
    group_id = predecessor.conn.execute('SELECT generation_id FROM group_evidence_generations LIMIT 1').fetchone()[0]
    group = predecessor.conn.execute('SELECT external_key FROM group_generations WHERE id=?', (group_id,)).fetchone()[0]
    predecessor.rec_ledger.record_serving_policy('compressed-policy', {'algorithm': 'saved'}, 1)
    if native:
        upgrade(predecessor)
    value = {'schema': 1, 'generation': 'compressed-home', 'context': {'group_generation': group},
        'tracks': [{'key': 'retained-track', 'serving_context': {'recommendation_generation_id': generation,
            'pool_policy': 'compressed-policy'}, 'musical': {'features': list(range(1000))}} for _ in range(100)]}
    raw = json_cache.encode(value, references=owner.policy_references(value))
    assert json.loads(raw)[json_cache.MARKER] == json_cache.FORMAT
    with predecessor.conn.atomic():
        predecessor.rec_proposals.publish_prepared_home(raw, 1000)
    assert predecessor.rec_proposals.get_proposals('home_prepared') == value
    assert predecessor.conn.execute('SELECT 1 FROM group_evidence_generations WHERE generation_id=?', (group_id,)).fetchone()
    assert predecessor.conn.execute('SELECT 1 FROM recommendation_generation_refs WHERE generation_id=?', (generation,)).fetchone()
    if native:
        assert tuple(predecessor.conn.execute('SELECT schema_version,generation_key FROM home_display_publications').fetchone()) == (1, 'compressed-home')
        assert predecessor.conn.execute("SELECT 1 FROM home_display_policies WHERE policy_version='compressed-policy'").fetchone()
        assert predecessor.conn.execute('SELECT 1 FROM home_display_group_roots').fetchone()
        assert predecessor.conn.execute('SELECT 1 FROM home_display_recommendation_roots').fetchone()
    assert not predecessor.conn.execute('PRAGMA foreign_key_check').fetchone()


@pytest.mark.parametrize('value', [float('nan'), float('inf'), -float('inf')])
def test_compressed_display_rejects_nonfinite_profile_inputs(value):
    raw = json_cache.encode({'tracks': [{'musical': {'feature': value, 'other': 'x' * 100000}}]})
    with pytest.raises(ValueError, match='Non-finite'):
        owner.prepare('home_prepared', raw, 1000)


def test_compressed_display_bounds_inflation(monkeypatch):
    raw = json_cache.encode({'title': 'x' * 100000})
    monkeypatch.setattr(owner, 'MAX_DECODED_BYTES', 10000)
    with pytest.raises(ValueError, match='compressed recommendation cache'):
        owner.prepare('home_prepared', raw, 1000)


def test_compressed_display_bounds_repeated_profiles_before_reconstruction(monkeypatch):
    profile = {'captured_features': {'original': 'x' * 64000}}
    value = {'tracks': [{'key': str(i), 'musical': profile} for i in range(300)]}
    assert len(json.dumps(value, separators=(',', ':')).encode()) > owner.MAX_DECODED_BYTES
    raw = json_cache.encode(value)
    assert len(raw.encode()) < 4_000_000
    monkeypatch.setattr(json_cache, '_restore_profiles', lambda *args, **kwargs:
                        pytest.fail('Oversized logical output reached profile reconstruction'))
    with pytest.raises(ValueError, match='logical byte budget'):
        owner.prepare('home_prepared', raw, 1000)


@pytest.mark.parametrize('value', [
    {'menu': {'protos': [{'tracks': [{'serving_context': {'pool_policy': 'held-policy'}}]}]}},
    {'items': [{'companion': [{'pool_policy': 'held-policy'}]}]},
])
def test_held_menu_and_album_companions_retain_their_actual_policy(predecessor, value):
    upgrade(predecessor)
    predecessor.rec_ledger.record_serving_policy('held-policy', {'algorithm': 'saved'}, 1)
    predecessor.rec_proposals.put_proposals('home_choice', value, 12)
    assert [row[0] for row in predecessor.conn.execute('SELECT policy_version FROM home_display_policies')] == ['held-policy']


@pytest.mark.parametrize('damage', ['missing', 'corrupt'])
def test_unavailable_display_file_is_a_cache_miss_and_can_be_rebuilt(predecessor, damage):
    from pathlib import Path
    upgrade(predecessor)
    predecessor.rec_proposals.put_proposals('home_prepared', {'title': 'one'}, 10)
    target = Path(predecessor.conn.execute('SELECT filename FROM home_display_files').fetchone()[0])
    if damage == 'missing':
        target.unlink()
    else:
        target.write_text('damaged')
    assert owner.encoded(predecessor.conn, 'home_prepared') is None
    predecessor.rec_proposals.put_proposals('home_prepared', {'title': 'two'}, 11)
    assert predecessor.rec_proposals.get_proposals('home_prepared') == {'title': 'two'}


def test_file_failure_or_interrupted_outer_publication_keeps_the_last_good(predecessor, monkeypatch):
    predecessor.rec_proposals.put_proposals('home_prepared', {'title': 'last good'}, 10)
    upgrade(predecessor)
    before = list(predecessor.conn.iterdump())
    prepared = owner.prepare('home_prepared', json.dumps({'title': 'new'}), 11)
    with pytest.raises(InterruptedError), predecessor.conn.atomic():
        owner.publish(predecessor.conn, prepared, store=predecessor)
        raise InterruptedError('Interrupted before commit')
    assert list(predecessor.conn.iterdump()) == before
    def unavailable(*args):
        raise OSError('Output volume unavailable')
    monkeypatch.setattr(files, 'publish', unavailable)
    with pytest.raises(OSError):
        predecessor.rec_proposals.put_proposals('home_prepared', {'title': 'new'}, 11)
    assert list(predecessor.conn.iterdump()) == before
    assert predecessor.rec_proposals.get_proposals('home_prepared') == {'title': 'last good'}


def test_replacement_keeps_pinned_reader_files_and_backup_restores_current_head(predecessor, tmp_path):
    predecessor.rec_proposals.put_proposals('home_prepared', {'title': 'one'}, 10)
    upgrade(predecessor)
    with predecessor.read_snapshot() as snapshot:
        reader = snapshot.store
        assert reader.rec_proposals.get_proposals('home_prepared') == {'title': 'one'}
        predecessor.rec_proposals.put_proposals('home_prepared', {'title': 'two'}, 11)
        assert reader.rec_proposals.get_proposals('home_prepared') == {'title': 'one'}
        assert files.collect_orphans(predecessor.conn) == 0
    assert files.collect_orphans(predecessor.conn) == 1
    with predecessor.conn.atomic():
        backup = files.export(predecessor.conn, tmp_path / 'backup')
    restored = Store(tmp_path / 'restored.db')
    try:
        predecessor.conn.backup(restored.conn)
        files.restore(restored.conn, backup)
        restored.init_schema(defer_metadata=True)
        assert restored.rec_proposals.get_proposals('home_prepared') == {'title': 'two'}
    finally:
        restored.conn.close()


def test_sql_rejects_incomplete_parts_wrong_heads_and_unowned_roots(predecessor):
    upgrade(predecessor)
    conn = predecessor.conn
    predecessor.rec_proposals.put_proposals('home_prepared', {'title': 'one'}, 10)
    with pytest.raises(sqlite3.IntegrityError), conn.atomic():
        conn.execute("INSERT INTO rec_proposals VALUES ('home_prepared','{}',1)")
    with pytest.raises(sqlite3.IntegrityError), conn.atomic():
        conn.execute('DELETE FROM home_display_files')
    with pytest.raises(sqlite3.IntegrityError), conn.atomic():
        conn.execute("INSERT INTO home_display_heads VALUES ('album_prepared',1)")
    with pytest.raises(sqlite3.IntegrityError), conn.atomic():
        conn.execute("INSERT INTO home_display_group_roots VALUES (999,'context','prepared','home-output:999:context')")


@pytest.mark.parametrize('raw', ['{', '{"schema":true}', '{"generation":12}', '{"value":NaN}',
                                 '{"value":1e999}', json.dumps({'text': 'x' * owner.MAX_BYTES})])
def test_malformed_legacy_display_retains_original_and_schema(predecessor, raw):
    with predecessor.conn:
        predecessor.conn.execute('INSERT INTO rec_proposals VALUES (?,?,1)', ('home_prepared', raw))
    before = list(predecessor.conn.iterdump())
    with pytest.raises(MigrationError):
        migrate(predecessor.conn, registry=registry.MIGRATIONS[:91])
    assert list(predecessor.conn.iterdump()) == before
