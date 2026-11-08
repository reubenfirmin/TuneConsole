"""Retained publications cross one real populated historical upgrade journey."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3

import pytest

from tests.catalog_migration_support import catalog_reader
from tests.core import test_calendar_index_preparation as calendar
from tests.core import test_discovery_artist_publication as artists
from tests.core import test_home_publication as home
from tests.core import test_metadata_destinations as metadata
from tests.core.test_early_schema_upgrade import rename_owner
from tests.genre_catalog_support import bound_catalog
from tests.metadata_provenance_support import metadata_provenance
from tests.store_fixture_support import reuse_reporting_reader
from yt_playlist.core.migrations import registry, regular, runner, v103_entity_names
from yt_playlist.core.store import Store


@pytest.fixture(autouse=True)
def disable_periodic_library_sync():
    """These SQL and file owners do not construct an HTTP lifespan."""


@pytest.fixture(scope='module', autouse=True)
def isolated_home(tmp_path_factory):
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv('YT_PLAYLIST_HOME', str(tmp_path_factory.mktemp('native-custody-home')))
        patch.setenv('YT_PLAYLIST_AUDIO_VIBES', '0')
        patch.setenv('YT_PLAYLIST_AUDIO_VIBE_MODEL', '')
        bound_catalog(patch, entries=(('house', 'House'), ('rock', 'Rock'),
                                     ('jazz', 'Jazz'), ('blues', 'Blues')))
        yield


@pytest.fixture(scope='module')
def custody(_bounded_installed_schema, tmp_path_factory):
    template = _bounded_installed_schema(79)
    path = tmp_path_factory.mktemp('native-custody') / 'installed.db'
    with closing(sqlite3.connect(path)) as conn:
        template.source.conn.backup(conn)
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA foreign_keys=ON')
        conn.execute('PRAGMA journal_mode=WAL')
        yield dict(conn=conn, path=path, version=79, artists={})


def assert_history(state):
    assert list(map(tuple, state['conn'].execute(
        'SELECT version,name FROM schema_migrations ORDER BY version'))) == [
        (item.version, item.name) for item in registry.MIGRATIONS[:state['version']]]
    assert not state['conn'].execute('PRAGMA foreign_key_check').fetchone()


def test_native_custody_input_is_a_private_real_installed_79_database(custody):
    assert_history(custody)
    assert not custody['conn'].execute('SELECT 1 FROM metadata_findings').fetchone()


def test_metadata_input_keeps_explicit_null_blank_and_native_observations(custody):
    with catalog_reader(custody['path'], 79, snapshot_guards=True) as store:
        with pytest.MonkeyPatch.context() as patch, reuse_reporting_reader(store, patch):
            custody['metadata'] = metadata.seed(store, title='Metadata song')
        entry, _, key = custody['metadata']
        assert store.tracks.get_track_audio(entry)[0] is None
        assert store.tracks.get_track_enrichment(entry)[0] == ''
        assert store.enrichment_state.metadata_observations(key)
    assert_history(custody)


def test_real_metadata_destination_stage_keeps_each_selected_delivery_file(custody):
    conn = custody['conn']
    metadata.preparation.JOB.install(conn)
    custody['metadata_stage'] = metadata.stage(conn)
    custody['metadata_files'] = [Path(row[0]) for row in conn.execute(
        'SELECT DISTINCT filename FROM '+metadata.publication.table('deliveries'))]
    assert custody['metadata_files'] and all(path.is_file() for path in custody['metadata_files'])


def test_actual_metadata_selection_publishes_the_prepared_stage(custody):
    metadata.publication.publish(custody['conn'], identity=custody['metadata_stage'])
    assert metadata.publication.selected(custody['conn']) == custody['metadata_stage']
    assert_history(custody)


def test_artist_input_keeps_repeated_explanation_and_legacy_counters(custody):
    with catalog_reader(custody['path'], 79, snapshot_guards=True) as store:
        artists.seed(store)
        value = store.discovery.get_discovered_artists()[0]
        assert value['because'] == ['Anchor', 'Anchor'] and value['offered_count'] == 9
    assert_history(custody)


def select_artist(state, version):
    conn = state['conn']
    owner, stage = state['artist_prepared']
    owner.publish(conn, stage=stage)
    source = conn.execute('SELECT source FROM '+owner.header).fetchone()[0]
    state['artists'][version] = dict(owner=owner, stage=stage, source=source,
        original=owner.value(conn, stage, source),
        mapping=list(map(tuple, conn.execute('SELECT * FROM '+owner.table('map')))))
    assert owner.selected(conn) == stage


def test_real_legacy_artist_preparation_keeps_durable_file_stage(custody):
    custody['artist_prepared'] = artists.ready(custody['conn'])
    owner, stage = custody['artist_prepared']
    assert stage and owner.name == 'artists_legacy'


def test_real_legacy_artist_selection_keeps_the_original_explanation(custody):
    select_artist(custody, 79)
    assert custody['artists'][79]['original']


@pytest.fixture(scope='module', params=range(80, 91))
def middle_version(request):
    return request.param


def test_actual_middle_owner_preserves_published_metadata_and_artist_custody(custody, middle_version):
    conn = custody['conn']
    runner.migrate(conn, registry=registry.MIGRATIONS[:middle_version])
    custody['version'] = middle_version
    assert_history(custody)
    for captured in custody['artists'].values():
        owner = captured['owner']
        assert owner.value(conn, captured['stage'], captured['source']) == captured['original']
        assert list(map(tuple, conn.execute('SELECT * FROM '+owner.table('map')))) == captured['mapping']
    assert all(path.is_file() for path in custody['metadata_files'])


def test_home_input_keeps_exact_multibyte_documents_and_real_retention_roots(custody):
    with catalog_reader(custody['path'], 90, snapshot_guards=True) as store:
        custody['home'] = home.seed(store)
        home.real_roots(store)
        assert len(custody['home']['adaptive_group_preview'].encode()) > 128 * 1024
        assert sum(map(len, home.root_rows(store.conn).values())) > 4


def test_real_home_preparation_keeps_complete_document_and_root_stages(custody):
    custody['home_stage'] = home.ready(custody['conn'])
    assert custody['home_stage']


def test_real_home_selection_keeps_the_exact_original_documents(custody):
    owner, conn = home.publication.OWNER, custody['conn']
    owner.publish(conn, stage=custody['home_stage'])
    assert owner.selected(conn) == custody['home_stage']
    for surface, raw in custody['home'].items():
        assert owner.value(conn, custody['home_stage'], surface) == raw


@pytest.mark.parametrize('version', range(91, 101))
def test_actual_classification_owners_preserve_home_and_artist_archives(custody, version):
    runner.migrate(custody['conn'], registry=registry.MIGRATIONS[:version])
    custody['version'] = version
    assert_history(custody)
    for surface, raw in custody['home'].items():
        assert home.publication.OWNER.value(custody['conn'], custody['home_stage'], surface) == raw


def test_real_classified_artist_preparation_keeps_its_distinct_native_owner(custody):
    custody['artist_prepared'] = artists.ready(custody['conn'])
    owner, stage = custody['artist_prepared']
    assert stage and owner.name == 'artists_classified'


def test_real_classified_artist_selection_keeps_the_original_explanation(custody):
    select_artist(custody, 100)
    assert custody['artists'][100]['original']


@pytest.mark.parametrize('version', [101, 102])
def test_actual_pre_rename_owners_preserve_every_publication(custody, version):
    runner.migrate(custody['conn'], registry=registry.MIGRATIONS[:version])
    custody['version'] = version
    assert_history(custody)
    assert all(path.is_file() for path in custody['metadata_files'])


def test_calendar_input_keeps_each_account_and_its_exact_foreign_day_rows(custody):
    with catalog_reader(custody['path'], 102, snapshot_guards=True) as store:
        with store.conn:
            store.conn.execute('UPDATE identities SET is_master=0 WHERE is_master=1')
        accounts, _ = calendar.seed(store)
        custody['calendar'] = {account: calendar.observed(store.conn, account) for account in accounts}
        assert len(custody['calendar']) == 2 and all(custody['calendar'].values())


def test_real_calendar_publication_selects_the_complete_source_projection(custody):
    conn = custody['conn']
    calendar.indexes.job(conn).install(conn)
    calendar.indexes.publish(conn, revision=calendar.finish(conn).input_revision)
    assert calendar.indexes.selected(conn)
    calendar.assert_lookup_rows(conn)


def test_real_entity_rename_preserves_all_rows_before_restoring_programs(custody):
    rename_owner(custody)
    custody['version'] = 103
    assert_history(custody)


def test_real_entity_program_restoration_keeps_selected_calendar_indexes(custody):
    with custody['conn']:
        custody['conn'].execute('BEGIN')
        v103_entity_names.restore_programs(*custody['programs'].args, **custody['programs'].kwargs)
        regular.after_owner_upgrade(custody['conn'])
    calendar.indexes.validate_installed(custody['conn'])
    calendar.assert_lookup_rows(custody['conn'])
    assert_history(custody)


@pytest.mark.parametrize('version', [104, 105, 106, 107])
def test_actual_later_owners_keep_all_original_files_and_selected_calendar(custody, version):
    runner.migrate(custody['conn'], registry=registry.MIGRATIONS[:version])
    custody['version'] = version
    assert_history(custody)
    assert calendar.indexes.selected(custody['conn'])
    calendar.assert_lookup_rows(custody['conn'])
    assert all(path.is_file() for path in custody['metadata_files'])


@pytest.mark.parametrize('version', range(1, len(regular.CHANGES) + 1))
def test_actual_regular_owners_keep_each_selected_custody_archive(custody, version):
    regular.migrate(custody['conn'], changes=regular.CHANGES[:version])
    assert regular.validate(custody['conn']) == version
    assert_history(custody)
    calendar.assert_lookup_rows(custody['conn'])
    assert all(path.is_file() for path in custody['metadata_files'])


@pytest.fixture(scope='module')
def reopened(custody):
    store = Store(custody['path'])
    try:
        store.init_schema(defer_metadata=True)
        yield store
    finally:
        store.conn.close()


def test_actual_current_admission_preserves_the_full_numbered_history(reopened, custody):
    assert_history(custody)
    assert regular.validate(reopened.conn) == len(regular.CHANGES)
    assert not reopened._schema_compatibility


def test_metadata_native_restart_keeps_choice_lineage_observations_and_selected_files(reopened, custody):
    entry, _, key = custody['metadata']
    assert metadata.publication.selected(reopened.conn) is None
    assert metadata_provenance(reopened, entry)['lineage']['genre']['choice']['delivery']
    assert reopened.enrichment_state.metadata_observations(key)
    assert all(path.is_file() for path in custody['metadata_files'])


@pytest.mark.parametrize('version', [79, 100])
def test_artist_native_restart_keeps_each_retired_map_and_original_explanation(reopened, custody, version):
    captured = custody['artists'][version]
    owner = captured['owner']
    assert owner.selected(reopened.conn) is None
    assert artists.publication.relation(reopened.conn) == 'main.discovered_artists'
    assert list(map(tuple, reopened.conn.execute('SELECT * FROM '+owner.table('map')))) == captured['mapping']
    assert owner.value(reopened.conn, captured['stage'], captured['source']) == captured['original']
    assert reopened.discovery.get_discovered_artists()[0]['because'] == ['Anchor', 'Anchor']


def test_home_native_restart_keeps_exact_archives_and_prefers_new_native_writes(reopened, custody):
    owner = home.publication.OWNER
    assert owner.selected(reopened.conn) is None
    for surface, raw in custody['home'].items():
        assert owner.value(reopened.conn, custody['home_stage'], surface) == raw
        assert reopened.rec_proposals.get_proposals(surface) == json.loads(raw)
    reopened.rec_proposals.put_proposals('home_prepared', {'native': True}, 3000)
    assert reopened.rec_proposals.get_proposals('home_prepared') == {'native': True}


def test_calendar_native_restart_keeps_both_accounts_and_follows_continuing_writes(reopened, custody):
    calendar.indexes.validate_installed(reopened.conn)
    assert calendar.indexes.selected(reopened.conn)
    for account, expected in custody['calendar'].items():
        assert calendar.observed(reopened.conn, account) == expected
    account = next(iter(custody['calendar']))
    reopened.history.record_play_event(account, 'later', None, calendar.stamp('2026-10-22T12:00:00'))
    calendar.assert_lookup_rows(reopened.conn)
    assert not reopened.conn.execute('PRAGMA foreign_key_check').fetchone()


def test_repeated_regular_admission_preserves_native_writes_and_retained_files(reopened, custody):
    before = list(reopened.conn.iterdump())
    regular.migrate(reopened.conn)
    assert list(reopened.conn.iterdump()) == before
    assert reopened.rec_proposals.get_proposals('home_prepared') == {'native': True}
    assert all(path.is_file() for path in custody['metadata_files'])
