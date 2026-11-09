"""Completed track spaces can serve through a refresh; retired artist vocabularies cannot."""
from contextlib import closing

import pytest
from tests.derived_rating_support import seed_dislike, seed_like
from tests.core.test_early_schema_upgrade import rename_owner, restore_programs
from tests.genre_catalog_support import bound_catalog
from tests.prepared_fixture_support import capture_template

from yt_playlist.core import embeddings
from yt_playlist.core.migrations import registry, regular, runner
from yt_playlist.core.store import Store
from yt_playlist.rec import invalidation


TRACK_SPACES = ('rec_vectors', 'rec_content_vectors', 'rec_discovered_content_vectors')
ARTIST_SPACES = ('rec_artist_vectors', 'rec_artist_content_vectors')
MODEL = '{"ncat":1,"cont":[]}'


@pytest.fixture(autouse=True)
def disable_periodic_library_sync():
    """Invalidation owns SQL and retained files, without an HTTP lifespan."""


@pytest.fixture(scope='module')
def identity_journey(_pristine_installed_schema, tmp_path_factory):
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv('YT_PLAYLIST_HOME', str(tmp_path_factory.mktemp('identity-upgrade-home')))
        bound_catalog(patch)
        source = Store(tmp_path_factory.mktemp('identity-upgrade') / 'installed.db')
        with closing(source.conn):
            _pristine_installed_schema(33).backup(source.conn)
            source.conn.execute('PRAGMA journal_mode=WAL')
            source.conn.execute('PRAGMA synchronous=NORMAL')
            original = _identity_inputs(source, current=False)
            yield dict(conn=source.conn, source=source, version=33, original=original)


def assert_identity_history(journey):
    conn = journey['conn']
    assert list(map(tuple, conn.execute('SELECT version,name FROM schema_migrations ORDER BY version'))) == [
        (entry.version, entry.name) for entry in registry.MIGRATIONS[:journey['version']]]
    assert list(map(tuple, conn.execute('SELECT id,identity_key FROM recordings'))) == journey['original']['subjects']
    assert list(map(tuple, conn.execute('SELECT id,started_at,result_json FROM rec_model_selection_runs'))) == journey['original']['evidence']
    assert not conn.execute('PRAGMA foreign_key_check').fetchone()


def test_identity_upgrade_input_keeps_populated_models_vectors_and_evidence(identity_journey):
    assert_identity_history(identity_journey)
    for table in (*TRACK_SPACES, *ARTIST_SPACES):
        assert identity_journey['conn'].execute('SELECT count(*) FROM ' + table).fetchone()[0] == 1


@pytest.mark.parametrize('version', range(34, 103))
def test_actual_identity_owner_preserves_catalog_and_rollback_evidence(identity_journey, version):
    runner.migrate(identity_journey['conn'], registry=registry.MIGRATIONS[:version])
    identity_journey['version'] = version
    assert_identity_history(identity_journey)


def test_actual_identity_account_rename_preserves_rollback_evidence(identity_journey):
    rename_owner(identity_journey)
    assert_identity_history(identity_journey)


def test_actual_identity_program_restoration_preserves_rollback_evidence(identity_journey):
    restore_programs(identity_journey)
    assert_identity_history(identity_journey)


@pytest.mark.parametrize('version', range(104, len(registry.MIGRATIONS) + 1))
def test_actual_final_identity_owner_preserves_rollback_evidence(identity_journey, version):
    runner.migrate(identity_journey['conn'], registry=registry.MIGRATIONS[:version])
    identity_journey['version'] = version
    assert_identity_history(identity_journey)


@pytest.mark.parametrize('version', range(1, len(regular.CHANGES) + 1))
def test_actual_regular_identity_owner_preserves_rollback_evidence(identity_journey, version):
    regular.migrate(identity_journey['conn'], changes=regular.CHANGES[:version])
    assert regular.validate(identity_journey['conn']) == version
    assert_identity_history(identity_journey)


def test_invalidation_input_keeps_shipped_history_and_read_only_guards(_pristine_current_schema):
    from yt_playlist.core.migrations import registry, regular

    source = _pristine_current_schema
    assert source.execute('PRAGMA query_only').fetchone()[0] == 1
    assert list(source.execute('SELECT version,name FROM schema_migrations ORDER BY version')) == [
        (entry.version, entry.name) for entry in registry.MIGRATIONS]
    assert list(source.execute('SELECT version,name FROM schema_changes ORDER BY version')) == [
        (entry.version, entry.name) for entry in regular.CHANGES]
    assert source.execute("SELECT 1 FROM sqlite_temp_schema WHERE name='snapshot_revisions'").fetchone()
    assert not source.execute('PRAGMA foreign_key_check').fetchone()


def _identity_inputs(store, *, current):
    with store.conn:
        store.conn.execute("INSERT INTO recordings(identity_key) VALUES ('key')")
        store.conn.execute("INSERT INTO artists(id,name,provisional) VALUES (1,'Artist',1)")
        for table in (*TRACK_SPACES, *ARTIST_SPACES):
            key = '@artist:1' if table in ARTIST_SPACES else 'key'
            if current:
                embeddings.publish(store.conn, table, [(key, bytes.fromhex('0000803F'))], replace=True)
            else:
                store.conn.execute(f'INSERT INTO {table} VALUES (?,?)', (key, bytes.fromhex('0000803F')))
        if not current:
            store.conn.execute("INSERT INTO rec_proposals VALUES ('home_choice','{\"marker\":\"artist-choice\"}',1)")
            store.conn.executemany('INSERT INTO settings(key,value) VALUES (?,?)', [
                ('rec_content_model', MODEL), ('rec_artist_content_model', MODEL)])
        store.conn.execute("INSERT INTO rec_model_selection_runs(started_at,result_json,identity_version) VALUES (1,'evidence',2)")
    if current:
        store.settings.set_setting('rec_content_model', MODEL)
        store.settings.set_setting('rec_artist_content_model', MODEL)
    return {
        'history': list(map(tuple, store.conn.execute('SELECT * FROM schema_migrations ORDER BY version'))),
        'subjects': list(map(tuple, store.conn.execute('SELECT id,identity_key FROM recordings'))),
        'evidence': list(map(tuple, store.conn.execute('SELECT id,started_at,result_json FROM rec_model_selection_runs'))),
    }


@pytest.fixture(scope='module')
def upgraded_identity_template(_pristine_installed_schema, tmp_path_factory):
    # A consumer selected alone must not depend on the journey tests having
    # advanced their shared v33 input first. Keep this conversion private so
    # selecting consumers before edge owners cannot advance those owners either.
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv('YT_PLAYLIST_HOME', str(tmp_path_factory.mktemp('completed-identity-home')))
        bound_catalog(patch)
        source = Store(tmp_path_factory.mktemp('completed-identity') / 'installed.db')
        with closing(source.conn):
            _pristine_installed_schema(33).backup(source.conn)
            source.conn.execute('PRAGMA journal_mode=WAL')
            source.conn.execute('PRAGMA synchronous=OFF')
            original = _identity_inputs(source, current=False)
            runner.migrate(source.conn, registry=registry.MIGRATIONS)
            source.init_schema(defer_metadata=True)
            yield capture_template(source, original)


def test_completed_identity_upgrade_keeps_private_read_only_admission(upgraded_identity_template):
    conn = upgraded_identity_template.source.conn
    assert conn.execute('PRAGMA query_only').fetchone()[0] == 1
    assert conn.execute('SELECT max(version) FROM schema_migrations').fetchone()[0] == len(registry.MIGRATIONS)
    assert upgraded_identity_template.revision_guards


@pytest.mark.parametrize('change', ['model', 'artist'])
def test_refresh_retains_playable_menus_usage_and_tv_until_replacement(store, change):
    from tests.onboarding_support import publish_personal_mix
    from tests.test_tv_stream import video
    from yt_playlist.rec import home_prepared, onboarding, tv

    # Invalidation consumes completed menus and TV output. The actual resolver
    # and bounded producer/recovery loop have their owners in test_tv.
    seeds = publish_personal_mix(store, now=1000)
    for seed in seeds:
        seed_like(store, seed['key'], 1000)
    store.rec_proposals.put_proposals(tv.SURFACE, {
        'schema': tv.SCHEMA, 'key': 'invalidation-input', 'at': 1000,
        'attempted': [seed['key'] for seed in seeds],
        'items': [{**video(index), 'source_key': seed['key'],
                   'source_artist': seed['artist']} for index, seed in enumerate(seeds)],
        'offset': 0, 'state': 'exhausted', 'retry_at': 1000 + tv.SEARCH_RETRY_S,
    }, 1000)
    previous_tv = tv.cached(store)
    assert previous_tv and tv.queue(store, 1000)
    previous_mix = home_prepared.peek(store, 1000)
    serving = {surface: store.rec_proposals.get_proposals(surface)
               for surface in ('home_prepared', 'home_tuned')}
    usage = {'home_format': {'episodes': {'mixes': 2}}}
    store.rec_proposals.put_proposals('listening_choices', usage, 1000)
    assert onboarding.prepared_mixes_ready(store, 1000)
    with store.conn.atomic():
        if change == 'artist':
            invalidation.artist_reconciled(store.conn)
        else:
            invalidation.clear_model_display(store.conn)
    assert {surface: store.rec_proposals.get_proposals(surface) for surface in serving} == serving
    assert store.rec_proposals.get_proposals('home_choice') is None
    assert store.rec_proposals.choice_usage()['episodes'] == {'mixes': 2}
    assert tv.cached(store) == previous_tv
    assert onboarding.prepared_mixes_ready(store, 1001)
    key = previous_mix['menus'][0]['protos'][0]['tracks'][0]['key']
    seed_dislike(store, key, until=None, now=1002)
    assert onboarding.prepared_mixes_ready(store, 1002)
    # Removing evidence explicitly still clears the completed outputs.
    with store.conn.atomic():
        invalidation.clear_display(store.conn)
        invalidation.clear_findings(store.conn)
    assert store.rec_proposals.get_proposals(home_prepared.SURFACE) is None
    assert not onboarding.prepared_mixes_ready(store, 1002)
    assert not store.rec_proposals.choice_usage()
    assert tv.cached(store) is None


@pytest.mark.parametrize('current', [False, True])
@pytest.mark.parametrize('change', ['catalog', 'artist'])
def test_identity_invalidation_policy_preserves_eligible_spaces_and_rollback_evidence(
        current, change, consumer_store, request):
    store = consumer_store()
    try:
        if current:
            original = _identity_inputs(store, current=True)
        else:
            original = request.getfixturevalue('upgraded_identity_template').restore(store)
        original_history, original_subjects, original_evidence = (
            original[name] for name in ('history', 'subjects', 'evidence'))
        track_tables, artist_tables, model = TRACK_SPACES, ARTIST_SPACES, MODEL
        if not current:
            assert original_history[-1][0] == 33
        else:
            store.rec_proposals.put_proposals('home_choice', {'marker': 'artist-choice'}, 1)
        history = list(map(tuple, store.conn.execute('SELECT * FROM schema_migrations ORDER BY version')))
        assert history[:len(original_history)] == original_history
        assert list(map(tuple, store.conn.execute('SELECT id,identity_key FROM recordings'))) == original_subjects
        assert list(map(tuple, store.conn.execute(
            'SELECT id,started_at,result_json FROM rec_model_selection_runs'))) == original_evidence
        assert store.rec_proposals.get_proposals('home_choice') == {'marker': 'artist-choice'}
        vectors = {table: embeddings.rows(store.conn, table) for table in (*track_tables, *artist_tables)}
        assert all(vectors[table] == [('key', bytes.fromhex('0000803F'))] for table in track_tables)
        assert all(vectors[table] == [('@artist:1', bytes.fromhex('0000803F'))] for table in artist_tables)
        store.settings.set_setting('rec_artist_fit_inputs', 'artist inputs')
        with store.conn:
            if change=='artist':
                invalidation.artist_reconciled(store.conn)
            else:
                invalidation.catalog_changed(store.conn)
        count = lambda table: embeddings.count(store.conn, table)
        if change=='artist':
            assert all(count(table)==1 for table in track_tables)
            assert all(embeddings.rows(store.conn, table) == vectors[table] for table in track_tables)
            assert all(count(table)==0 for table in artist_tables)
            assert store.rec_proposals.get_proposals('home_choice') is None
            assert store.settings.get_setting('rec_content_model') == model
            assert store.settings.get_setting('rec_artist_content_model') is None
            assert store.settings.get_setting('rec_artist_fit_inputs') is None
        else:
            assert all(count(table)==1 for table in (*track_tables,*artist_tables))
            assert all(embeddings.rows(store.conn, table) == vectors[table] for table in vectors)
            assert store.rec_proposals.get_proposals('home_choice') == {'marker': 'artist-choice'}
        assert store.conn.execute("SELECT 1 FROM rec_change_queue WHERE kind='refresh' AND item=''").fetchone()
        evidence = store.conn.execute('SELECT result_json,identity_version FROM rec_model_selection_runs').fetchone()
        assert tuple(evidence)==('evidence',0)
        assert list(map(tuple, store.conn.execute(
            'SELECT id,started_at,result_json FROM rec_model_selection_runs'))) == original_evidence
        assert list(map(tuple, store.conn.execute(
            'SELECT * FROM schema_migrations ORDER BY version'))) == history
    finally:
        store.conn.close()
