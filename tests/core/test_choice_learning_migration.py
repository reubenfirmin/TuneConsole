"""Compiled serving inputs have typed owners and atomic, retained publications."""
import sqlite3
from types import SimpleNamespace

import pytest
from tests.genre_catalog_support import bound_catalog
from tests.core.test_early_schema_upgrade import rename_owner, restore_programs
from tests.prepared_fixture_support import assert_upgrade_phase, capture_template, custody_predecessor, custody_upgrade_cases
from tests.store_fixture_support import install_regular_input

from yt_playlist.core import choice_learning_state as owner
from yt_playlist.core.migrations import migrate, registry, regular, v093_choice_learning
from yt_playlist.core.migrations.runner import MigrationError
from yt_playlist.core.store import Store


@pytest.fixture
def predecessor(tmp_path, _pristine_installed_schema):
    store = Store(tmp_path / 'learning.db')
    store.conn.execute('PRAGMA journal_mode=WAL')
    _pristine_installed_schema(92).backup(store.conn)
    store._bind_repositories()
    from yt_playlist.core.db_revisions import Revisions
    store._snapshot_revisions = Revisions(store.conn)
    yield store
    store.conn.close()


@pytest.fixture(scope='module', autouse=True)
def isolated_learning_home(tmp_path_factory):
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv('YT_PLAYLIST_HOME', str(tmp_path_factory.mktemp('choice-learning-home')))
        bound_catalog(patch)
        yield


@pytest.fixture(autouse=True)
def disable_periodic_library_sync():
    """These SQL owners do not construct an HTTP application."""


def interests():
    return {'schema': 1, 'at': 10, 'watermark': 'original', 'contexts_total': 1,
            'contexts': [{'format': 'albums', 'option_id': 'release', 'mode_id': None,
                          'keys': ['song|artist', 'unknown|artist'], 'feature': [0., .25],
                          'at': 8, 'days': 2, 'episodes': 3, 'episode_support': .75}],
            'album_track_days': {'release': {'song|artist': 2}}, 'interest_version': 'effective'}


def seed_learning_input(store):
    from tests.group_support import publish, recordings

    # Completed private fixture writes need commit visibility; crash durability
    # belongs to the real upgrade and recovery owners.
    store.conn.execute('PRAGMA synchronous=NORMAL')
    store.tracks.upsert_track('video', 'Song', 'Artist', 'Album', 100, album_browse_id='release')
    store.rec_proposals.put_proposals('choice_interests', interests(), None)
    store.rec_proposals.put_proposals('choice_bandit', {'rows': [{'mode_id': 99, 'estimate': .5}], 'expires_at': None}, None)
    # Four members suffice for a real retained group estimate. Numerical graph
    # discovery at corpus scale is below this compiled-input upgrade boundary.
    head = publish(store, recordings([('Jazz', 4, .2)]), now=10)
    group = head['groups'][0]
    value = {'policy': 'compiled-statistics', 'generation': head['generation'], 'at': 11,
             'rows': [{'group_id': group['group_id'], 'version': group['version'], 'estimate': .6,
                       'shared_from': None, 'menus': ['opportunity']}], 'excluded': {'unseen': 0}}
    store.rec_proposals.put_proposals('group_choice_bandit', value, 11)
    return {'head': head, 'group': group, 'group_estimates': value}


@pytest.fixture(scope='module')
def learning_native_schema(_bounded_installed_schema, tmp_path_factory):
    with _bounded_installed_schema(92).open(
            tmp_path_factory.mktemp('choice-learning-regular-input') / 'source.db') as source:
        source.conn.execute('PRAGMA synchronous=NORMAL')
        with source.conn:
            install_regular_input(source.conn, regular.CHANGES)
        yield capture_template(source, None)


@pytest.fixture(scope='module')
def learning_native_inputs(upgraded_custody_inputs, learning_native_schema):
    renamed = None
    restored = False

    def original(target, **options):
        return upgraded_custody_inputs(92, target, 'choice-learning-native', seed_learning_input,
                                       prepared=learning_native_schema, **options)

    def get(target, **options):
        nonlocal renamed, restored
        phase = options.get('phase', 'captured')
        if target == 103 and phase != 'input':
            if renamed is None:
                predecessor = original(103, phase='input')
                conn = predecessor.source.conn
                conn.execute('PRAGMA query_only=OFF')
                try:
                    renamed = rename_owner(dict(conn=conn, source=predecessor.source,
                                                result=predecessor.result))
                finally:
                    conn.execute('PRAGMA query_only=ON')
            if phase != 'converted' and not restored:
                conn = renamed['conn']
                conn.execute('PRAGMA query_only=OFF')
                try:
                    restore_programs(renamed)
                    restored = True
                finally:
                    conn.execute('PRAGMA query_only=ON')
            if phase in ('converted', 'programs'):
                return SimpleNamespace(source=renamed['source'], result=renamed['result'])
        return original(target, **options)
    return get


def test_choice_learning_schema_input_keeps_actual_92_history_and_private_guards(_bounded_installed_schema):
    assert_upgrade_phase(_bounded_installed_schema(92), 92)


def test_choice_learning_regular_input_has_complete_real_guards_without_advancing_numbered_history(
        learning_native_schema):
    assert_upgrade_phase(learning_native_schema, 92)
    assert regular.validate(learning_native_schema.source.conn) == len(regular.CHANGES)


def test_choice_learning_input_keeps_original_null_clocks_unknowns_and_group_estimates(learning_native_inputs):
    template = learning_native_inputs(92)
    assert_upgrade_phase(template, 92)
    source = template.source
    assert source.rec_proposals.get_proposals('choice_interests') == interests()
    assert source.rec_proposals.get_proposals('group_choice_bandit') == template.result['group_estimates']
    assert source.conn.execute("SELECT built_at FROM rec_proposals WHERE surface='choice_interests'").fetchone()[0] is None


CHOICE_UPGRADES = [step for case in custody_upgrade_cases((92,))
                  for step in ([case, (92, 103, 'programs')] if case == (92, 103, 'converted') else [case])]


@pytest.mark.parametrize('version,target,phase', CHOICE_UPGRADES)
def test_actual_choice_learning_upgrade_keeps_values_and_original_group_evidence(
        learning_native_inputs, version, target, phase):
    template = learning_native_inputs(target, phase=phase)
    actual = custody_predecessor(version, target) if phase == 'input' else target
    assert_upgrade_phase(template, actual)
    if actual >= 93:
        assert owner.read(template.source.conn, 'choice_interests') == interests()


def test_choice_learning_serving_input_keeps_current_history_and_real_snapshot_guards(learning_native_inputs):
    template = learning_native_inputs(107)
    assert_upgrade_phase(template, 107)
    assert template.source._snapshot_revisions.conn is template.source.conn


@pytest.fixture(scope='module')
def upgraded_learning_template(learning_native_inputs):
    return learning_native_inputs(107)


@pytest.fixture
def upgraded_learning(tmp_path, upgraded_learning_template):
    with upgraded_learning_template.open(tmp_path / 'learning.db') as store:
        yield store


def test_registered_cutover_preserves_values_unknowns_subjects_and_restart(upgraded_learning):
    store = upgraded_learning
    original = interests()
    assert store.rec_proposals.get_proposals('choice_interests') == original
    assert store.rec_proposals.get_proposals('choice_bandit') == {'rows': [{'mode_id': 99, 'estimate': .5}], 'expires_at': None}
    assert [tuple(row) for row in store.conn.execute('SELECT raw_key,recording_id IS NOT NULL,resolution_reason '
            'FROM choice_interest_tracks ORDER BY position')] == [
                ('song|artist', 1, None), ('unknown|artist', 0, 'missing_recording')]
    assert store.conn.execute('SELECT album_id FROM choice_interest_contexts').fetchone()[0] is not None
    assert store.conn.execute('SELECT album_id FROM choice_album_interests').fetchone()[0] is not None
    assert tuple(store.conn.execute('SELECT resolved_mode_id,resolution_reason FROM choice_mode_estimates').fetchone()) == (None, 'missing_mode')
    assert all(row[0] is None for row in store.conn.execute(
        "SELECT built_at FROM choice_learning_publications WHERE surface IN ('choice_interests','choice_bandit')"))
    assert not store.conn.execute("SELECT 1 FROM rec_proposals WHERE surface IN ('choice_bandit','group_choice_bandit','choice_interests')").fetchone()
    reopened = Store(store._db_path)
    try:
        reopened.init_schema()
        assert reopened.rec_proposals.get_proposals('choice_interests') == original
        assert not reopened.conn.execute('PRAGMA foreign_key_check').fetchall()
    finally:
        reopened.conn.close()


def test_historical_group_estimates_keep_values_without_pinning_snapshots(upgraded_learning, upgraded_learning_template):
    store = upgraded_learning
    head, group, value = (upgraded_learning_template.result[name] for name in ('head', 'group', 'group_estimates'))
    assert store.rec_proposals.get_proposals('group_choice_bandit') == value
    assert not store.conn.execute('SELECT 1 FROM choice_learning_group_roots').fetchone()
    # #688 retains the historical numerical result without requiring its group
    # tree. It cannot become a new snapshot owner when the projection changes.
    replacement = {**value, 'generation': 'missing'}
    store.rec_proposals.put_proposals('group_choice_bandit', replacement, 12)
    assert store.rec_proposals.get_proposals('group_choice_bandit') == replacement
    store.rec_proposals.put_proposals('group_choice_bandit', {**value, 'rows': []}, 13)
    assert not store.conn.execute('SELECT 1 FROM choice_learning_group_roots').fetchone()
    assert store.taste_groups.head_generation() == head['generation']
    assert store.taste_groups.definition(group['version'], generation=head['generation']) is not None


def test_interrupted_cutover_leaves_original_rows_and_schema(predecessor):
    predecessor.rec_proposals.put_proposals('choice_interests', interests(), 10)
    before = list(predecessor.conn.iterdump())
    with pytest.raises(InterruptedError), predecessor.conn.atomic():
        v093_choice_learning.apply(predecessor.conn, None)
        raise InterruptedError('Before migration commit')
    assert list(predecessor.conn.iterdump()) == before


def test_publication_rollback_and_snapshot_invalidation_follow_typed_inputs(upgraded_learning):
    predecessor = upgraded_learning
    predecessor.rec_proposals.put_proposals('choice_interests', interests(), 10)
    with predecessor.read_snapshot() as snapshot:
        assert snapshot.store.rec_proposals.get_proposals('choice_interests') == interests()
        snapshot.release_reader()
    replacement = {**interests(), 'interest_version': 'new'}
    with pytest.raises(InterruptedError), predecessor.conn.atomic():
        owner.write(predecessor.conn, 'choice_interests', replacement, 11)
        raise InterruptedError('Before publication commit')
    assert predecessor.rec_proposals.get_proposals('choice_interests') == interests()
    predecessor.rec_proposals.put_proposals('choice_interests', replacement, 12)
    assert not snapshot.publish(lambda: pytest.fail('Published from stale learning inputs'))
    assert predecessor.conn.execute("SELECT count(*) FROM choice_learning_publications WHERE surface='choice_interests'").fetchone()[0] == 1


def test_sql_enforces_surface_subject_and_immutable_published_inputs(upgraded_learning):
    predecessor = upgraded_learning
    predecessor.rec_proposals.put_proposals('choice_interests', interests(), 10)
    identity = predecessor.conn.execute("SELECT publication_id FROM choice_learning_heads WHERE surface='choice_interests'").fetchone()[0]
    commands = [
        ("INSERT INTO rec_proposals VALUES ('choice_interests','{}',1)", ()),
        ('INSERT INTO choice_learning_heads VALUES (?,?)', ('choice_bandit', identity)),
        ('INSERT INTO choice_mode_estimates(publication_id,position,field_mask,resolution_reason) VALUES (?,0,0,?)', (identity, 'missing_mode')),
        ('UPDATE choice_interest_tracks SET recording_id=999999,resolution_reason=NULL', ()),
        ('UPDATE choice_interest_contexts SET days=99', ()),
        ('DELETE FROM choice_interest_features', ()),
        ('UPDATE choice_learning_publications SET at=99', ()),
        ('DELETE FROM choice_learning_publications', ()),
    ]
    for sql, parameters in commands:
        with pytest.raises(sqlite3.IntegrityError), predecessor.conn.atomic():
            predecessor.conn.execute(sql, parameters)
    assert predecessor.rec_proposals.get_proposals('choice_interests') == interests()


@pytest.mark.parametrize('raw', ['{', '{"contexts":[{"feature":[NaN]}]}',
                                 '{"contexts":[{"feature":[1e999]}]}', '{"unexpected":1}',
                                 '{"parameters":{"open":null}}'])
def test_invalid_old_state_keeps_the_original_for_recovery(predecessor, raw):
    with predecessor.conn:
        predecessor.conn.execute('INSERT INTO rec_proposals VALUES (?,?,?)', ('choice_interests', raw, 10))
    before = list(predecessor.conn.iterdump())
    with pytest.raises(MigrationError):
        migrate(predecessor.conn, registry=registry.MIGRATIONS)
    assert list(predecessor.conn.iterdump()) == before
