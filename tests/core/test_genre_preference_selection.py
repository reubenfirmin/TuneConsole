"""Genre selection and native cutover retain intent and original source rows."""
from contextlib import closing
import json
import sqlite3

import pytest

from tests.core.test_genre_preference_publication import ready, seed, sources
from tests.genre_catalog_support import bound_catalog
from tests.prepared_fixture_support import capture_template, prepared_template
from tests.store_fixture_support import install_numbered_input
from yt_playlist.core import schema_definition
from yt_playlist.core.migrations import registry, regular, runner
from yt_playlist.core.preparation import genre_preferences as preparation, genre_preference_publication as publication
from yt_playlist.core.store import Store


EXPECTED = [('drum bass', 1, 'legacy'), ('rock', 0, 'default')]


@pytest.fixture(autouse=True)
def disable_periodic_library_sync():
    """These SQL owners do not start an application or background worker."""


@pytest.fixture(scope='module', autouse=True)
def isolated_selection_home(tmp_path_factory):
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv('YT_PLAYLIST_HOME', str(tmp_path_factory.mktemp('genre-selection-home')))
        patch.setenv('YT_PLAYLIST_AUDIO_ENABLED', '0')
        patch.setenv('YT_PLAYLIST_ENRICH_MODEL', '')
        patch.delenv('ANTHROPIC_API_KEY', raising=False)
        bound_catalog(patch, entries=(('drum bass', 'Drum & Bass'), ('rock', 'Rock')))
        yield


@pytest.fixture(scope='module', params=[79, 84])
def selection_input(request, tmp_path_factory):
    version = request.param
    path = tmp_path_factory.mktemp('genre-selection-input') / f'{version}.db'
    with closing(sqlite3.connect(':memory:')) as schema:
        install_numbered_input(schema, version, regular_owners=regular.CHANGES)
        schema.execute("INSERT INTO genre_alias VALUES ('drum and bass','Drum And Bass','drum bass','curated')")
        schema.commit()
        with prepared_template(schema, lambda store: version, database=path, defer_metadata=True) as template:
            yield template


def test_selection_input_admits_history_regular_ddl_wal_and_revision_guards(selection_input):
    source = selection_input.source
    with closing(sqlite3.connect(source._db_path.as_uri() + '?mode=ro', uri=True)) as reader:
        reader.row_factory = sqlite3.Row
        runner.validate_version(reader, selection_input.result)
        regular.validate(reader)
    assert source.conn.execute('PRAGMA journal_mode').fetchone()[0] == 'wal'
    assert selection_input.revision_guards


@pytest.fixture(scope='module')
def selection_seeded(selection_input, tmp_path_factory):
    with selection_input.open(tmp_path_factory.mktemp('genre-selection-source') / 'source.db') as store:
        seed(store)
        yield capture_template(store, sources(store.conn))


def test_selection_source_keeps_legacy_spelling_duplicate_and_unknown_defaults(selection_seeded):
    store = selection_seeded.source
    assert store.genres.get_genre_whitelist() == ['Drum And Bass']
    assert dict(selection_seeded.result['settings'])['genre_defaults'] == (
        '[ "Drum And Bass", "Rock", "Rock", "Unknown default", "" ]')
    assert sources(store.conn) == selection_seeded.result


@pytest.fixture(scope='module')
def selection_connections(selection_seeded, tmp_path_factory):
    with selection_seeded.open(tmp_path_factory.mktemp('genre-selection-live') / 'live.db') as store:
        other = Store(store._db_path)
        try:
            other.init_schema(defer_metadata=True)
            yield store, other
        finally:
            other.conn.close()


def test_preopened_reader_starts_with_legacy_choice_and_no_selection(selection_connections):
    store, other = selection_connections
    assert publication.selected(store.conn) is None
    assert other.genres.get_genre_whitelist() == ['Drum And Bass']


@pytest.fixture(scope='module')
def selection_published(selection_connections):
    store, other = selection_connections
    with other.read_snapshot() as snapshot:
        pinned = snapshot.store
        assert pinned.genres.get_genre_whitelist() == ['Drum And Bass']
        stage = ready(store.conn, 1)
        publication.publish(store.conn, identity=stage)
        assert publication.selected(pinned.conn) is None
        assert pinned.genres.get_genre_whitelist() == ['Drum And Bass']
    return store, other, stage


def test_selection_publishes_enabled_legacy_and_disabled_default_without_changing_pinned_reads(selection_published):
    store, _, stage = selection_published
    assert publication.selected(store.conn) == stage
    assert list(map(tuple, store.conn.execute(
        'SELECT genre_key,enabled,origin FROM ' + publication.relation(store.conn) + ' ORDER BY genre_key'))) == EXPECTED


def test_selection_retains_exact_baseline_bytes_and_all_six_source_rows(selection_published, selection_seeded):
    store, _, stage = selection_published
    baseline = json.loads(publication.stage(store.conn, stage).baseline)
    assert preparation.read_baseline(baseline) == dict(selection_seeded.result['settings'])['genre_defaults']
    assert store.conn.execute('SELECT count(*) FROM ' + publication.table('rows')).fetchone()[0] == 6
    assert sources(store.conn) == selection_seeded.result


def test_preopened_reader_uses_selected_canonical_values_without_reading_legacy_whitelist(selection_published):
    _, other, _ = selection_published
    with other.read_view() as reader:
        reader.conn.set_authorizer(lambda action, owner, *_: sqlite3.SQLITE_DENY
            if action == sqlite3.SQLITE_READ and owner == 'genre_whitelist' else sqlite3.SQLITE_OK)
        try:
            assert reader.genres.get_genre_whitelist() == ['Drum & Bass']
            assert reader.genres.enabled_matcher()['drum and bass'] == 'Drum & Bass'
        finally:
            reader.conn.set_authorizer(None)


def test_regular_ddl_and_republication_keep_sources_and_one_generation(selection_published, selection_seeded):
    store, _, stage = selection_published
    regular.migrate(store.conn)
    assert sources(store.conn) == selection_seeded.result
    assert publication.step(store.conn).id == stage
    publication.publish(store.conn, identity=stage)
    assert store.conn.execute('SELECT count(*) FROM ' + publication.table('generations')).fetchone()[0] == 1


@pytest.fixture(scope='module')
def selection_retained(selection_published):
    store, _, stage = selection_published
    return capture_template(store, stage)


def test_selected_upgrade_input_is_read_only_and_keeps_complete_originals(selection_retained):
    conn = selection_retained.source.conn
    assert conn.execute('PRAGMA query_only').fetchone()[0] == 1
    assert publication.selected(conn) == selection_retained.result
    assert conn.execute('SELECT count(*) FROM ' + publication.table('rows')).fetchone()[0] == 6


@pytest.fixture(scope='module')
def selection_upgrade(selection_retained):
    # Historical SQL conversion has no file writes. Keep its retained baseline
    # in the immutable source; pinned serving reads above still use real WAL.
    with closing(sqlite3.connect(':memory:')) as conn:
        selection_retained.source.conn.backup(conn)
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA foreign_keys=ON')
        yield conn


def test_upgrade_copy_keeps_complete_baseline_bytes_and_retained_source_rows(selection_upgrade, selection_retained):
    stage = selection_retained.result
    original = json.loads(publication.stage(selection_retained.source.conn, stage).baseline)
    private = json.loads(publication.stage(selection_upgrade, stage).baseline)
    assert preparation.read_baseline(private) == preparation.read_baseline(original)
    assert selection_upgrade.execute('SELECT count(*) FROM ' + publication.table('rows')).fetchone()[0] == 6


def assert_native(conn):
    assert list(map(tuple, conn.execute('SELECT * FROM genre_preferences ORDER BY genre_key'))) == EXPECTED
    assert publication.selected(conn) is None
    assert conn.execute('SELECT count(*) FROM ' + publication.table('rows')).fetchone()[0] == 6
    assert not conn.execute('PRAGMA foreign_key_check').fetchall()


@pytest.fixture(scope='module')
def selection_native(selection_upgrade):
    runner.migrate(selection_upgrade, registry=registry.MIGRATIONS[:85])
    return selection_upgrade


def test_native_v85_cutover_matches_enabled_disabled_intent_and_retains_originals(selection_native):
    assert selection_native.execute('SELECT max(version) FROM schema_migrations').fetchone()[0] == 85
    assert_native(selection_native)


def test_native_v85_regular_admission_preserves_values_and_originals(selection_native):
    regular.migrate(selection_native)
    regular.validate(selection_native)
    assert_native(selection_native)


def test_account_cutover_reference_keeps_genre_preference_contract_independent():
    definitions = tuple(schema_definition.statements(version=103))
    assert 'CREATE TABLE genre_preferences' in '\n'.join(definitions)
    assert any(sql.startswith('CREATE TABLE accounts ') for sql in definitions)
    assert not any(sql.startswith('CREATE TABLE identities ') for sql in definitions)


@pytest.mark.parametrize('target', range(86, len(registry.MIGRATIONS) + 1))
def test_each_later_registered_upgrade_preserves_native_preferences_and_retained_rows(selection_native, target):
    current = selection_native.execute('SELECT max(version) FROM schema_migrations').fetchone()[0]
    if current < target - 1:
        runner.migrate(selection_native, registry=registry.MIGRATIONS[:target - 1])
    runner.migrate(selection_native, registry=registry.MIGRATIONS[:target])
    assert selection_native.execute('SELECT max(version) FROM schema_migrations').fetchone()[0] == target
    assert_native(selection_native)
    publication.validate_installed(selection_native)


def test_current_regular_admission_keeps_native_intent_and_all_originals(selection_native):
    runner.migrate(selection_native, registry=registry.MIGRATIONS)
    regular.migrate(selection_native)
    regular.validate(selection_native)
    assert_native(selection_native)
