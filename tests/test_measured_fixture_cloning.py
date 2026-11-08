"""Private SQL/file clones consume complete measured lower-layer outputs."""
from copy import deepcopy
import sqlite3

import pytest

from tests import measured_sounds_support as measured
from tests.home_fixture_support import returning_listener
from tests.prepared_fixture_support import capture_template, prepared_template, roots
from tests.recommendation_support import seed_publication_subjects
from tests.store_fixture_support import bounded_consumer_schema
from yt_playlist.core import profile_files
from yt_playlist.rec import home_cache, home_cards, home_prepared, home_vibe, mix_tuning, mode_surfaces


@pytest.fixture(autouse=True)
def disable_periodic_library_sync():
    """Fixture-copy contracts do not create a web application or workers."""


@pytest.fixture(scope='module', autouse=True)
def isolated_app_home(tmp_path_factory):
    # Module-owned Stores and their reader locks outlive individual consumers.
    # Keep their evidence namespace alive for the same bounded fixture lifetime.
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv('YT_PLAYLIST_HOME', str(tmp_path_factory.mktemp('measured-app-home')))
        patch.setenv('YT_PLAYLIST_AUDIO_VIBES', '0')
        patch.setenv('YT_PLAYLIST_AUDIO_VIBE_MODEL', '')
        patch.delenv('DEEPSEEK_API_KEY', raising=False)
        yield


@pytest.fixture(scope='module')
def measured_schema():
    entries = tuple((genre.lower().replace(' ', '-'), genre) for genre in
                    ('Jazz', 'Techno', 'Trance', 'Indie Rock', 'Ambient', 'Classical'))
    with bounded_consumer_schema(entries) as schema:
        yield schema


def test_measured_clone_schema_has_real_empty_owners_and_private_revision_guards(measured_schema):
    assert measured_schema.execute('SELECT COUNT(*) FROM recordings').fetchone()[0] == 0
    assert measured_schema.execute('SELECT COUNT(*) FROM genre_family').fetchone()[0] == 6
    assert measured_schema._revision_script


@pytest.fixture(scope='module')
def measured_catalog(measured_schema):
    with prepared_template(measured_schema, measured.catalog) as template:
        yield template


def test_measured_clone_catalog_keeps_all_six_modes_and_480_distinct_subjects(measured_catalog):
    source = measured_catalog.source
    assert len(source.modes.list_modes()) == 6
    assert source.conn.execute('SELECT COUNT(*) FROM recordings').fetchone()[0] == 480
    assert len(source.rec_query.library_keys()) == 360
    keys = {item['key'] for items in measured_catalog.bundles['all'].values() for item in items}
    assert len(keys) == 480
    assert len(source.discovery.discovered_tracks_by_keys(keys)) == 120
    assert not source.conn.execute('PRAGMA foreign_key_check').fetchall()


@pytest.fixture(scope='module')
def measured_profiles(measured_catalog):
    with measured_catalog.open(':memory:') as source:
        measured.profiles(source, source._fixture_bundle_payload)
        yield capture_template(source, None)


def test_measured_clone_profiles_retain_the_complete_soft_tail_in_real_file_heads(measured_profiles):
    source = measured_profiles.source
    keys = [item['key'] for items in measured_profiles.bundles['all'].values() for item in items]
    profiles = source.musical_profiles.by_keys(keys)
    assert len(profiles) == 480
    assert source.conn.execute('SELECT COUNT(*) FROM recording_profile_files').fetchone()[0] == 480
    for mid in range(6):
        for lane in mode_surfaces.CARD_SURFACES:
            items = measured_profiles.bundles[str(mid)][lane]
            assert len(items) == 20
            assert [profiles[item['key']]['dimensions']['intensity']['value'] < .5 for item in items] == (
                [False] * 12 + [True] * 8)


@pytest.fixture(scope='module')
def measured_publications(measured_profiles):
    with measured_profiles.open(':memory:') as source:
        measured.publish(source, source._fixture_bundle_payload)
        yield capture_template(source, None)


def test_measured_clone_publications_keep_every_preset_and_complete_input(measured_publications):
    bundle = measured_publications.bundles
    assert set(bundle['_vibes']['pools']) == set(home_vibe.PRESETS)
    assert {head['surface'] for head in measured_publications.source.recommendations.heads()} >= {
        'vibe:' + preset for preset in home_vibe.PRESETS}
    for mid in range(6):
        for items in bundle[str(mid)].values():
            assert len(items) == 20
            assert sum(item['musical']['dimensions']['intensity']['value'] < .5 for item in items) == 8


@pytest.fixture(scope='module')
def measured_browser(measured_publications):
    with measured_publications.open(':memory:') as source:
        account = source.accounts.upsert_account('main', 'cred', None, True)
        source.playlists.upsert_playlist(account, 'PLZ', 'Old Mix', 3, 'h', 0.)
        returning_listener(source, 'vibes', now=1)
        source.settings.set_setting('last_sync_at', '1')
        source.settings.set_setting('onboard_dismissed', '1')
        genres = ('Jazz', 'Techno', 'Trance', 'Indie Rock', 'Ambient', 'Classical')
        favorites = [{'key': f'favorite {index}|favorite artist {index}', 'video_id': f'favorite-{index}',
                      'title': f'Favorite {index}', 'artist': f'Favorite artist {index}', 'genre': genre,
                      'year': str(1960 + index * 10)} for index, genre in enumerate(genres)]
        seed_publication_subjects(source, favorites)
        for item in favorites:
            for day in range(10):
                source.history.record_history_plays(account, 1 - day * 86400, [item['key']], source_timezone='UTC')
        # The clone consumer receives a completed four-card calculation. The
        # actual Home writer retains its typed display files and SQL coordinates.
        with source.read_view() as reader:
            protos = []
            for lane in mode_surfaces.CARD_SURFACES:
                proto = home_cards._proto(reader, lane, lane.title(),
                    deepcopy(source._fixture_bundle_payload['all'][lane][:4]), 1)
                proto['recipe'] = {'lane': lane}
                protos.append(proto)
        result = home_cards.CardsResult(protos, [{'proto': proto, 'pool': None} for proto in protos],
                                       None, None, [], [], 0, None)
        home_prepared.remember(source, None, result, 1)
        home = home_cache.read(source, 'home_tuned')
        assert home_prepared._valid(home)
        home_cache.put(source, home_prepared.SURFACE, home, 1)
        home_cache.put(source, 'home_tuned', {}, 1)
        home_cache.put(source, 'home_choice', {}, 1)
        yield capture_template(source, None)


def assert_browser(owner, template):
    assert owner.conn.execute('SELECT COUNT(*) FROM recordings').fetchone()[0] == 486
    assert len(owner.rec_query.library_keys()) == 366
    assert len(owner.accounts.get_accounts()) == 1
    playlists = owner.playlists.get_playlists()
    assert len(playlists) == 1
    assert (playlists[0].provider_playlist_id, playlists[0].title, playlists[0].track_count) == ('PLZ', 'Old Mix', 3)
    home = owner.rec_proposals.get_proposals(home_prepared.SURFACE)
    assert home == template.source.rec_proposals.get_proposals(home_prepared.SURFACE)
    assert home_prepared._valid(home) and len(home['menus'][0]['protos']) == 4
    assert owner.settings.get_setting(home_vibe.SETTING) is None
    assert owner.settings.get_setting(mix_tuning.UPDATED_AT_SETTING) is None
    assert owner.rec_model.get_leans() == {}
    assert not owner.conn.execute('PRAGMA foreign_key_check').fetchall()


def test_measured_browser_input_keeps_playlists_home_and_untuned_defaults(measured_browser):
    assert_browser(measured_browser.source, measured_browser)
    assert measured_browser.source.conn.execute('PRAGMA query_only').fetchone()[0] == 1
    with pytest.raises(sqlite3.OperationalError, match='readonly'), measured_browser.source.conn.atomic():
        measured_browser.source.settings.set_setting('private_sound_fixture', 'forbidden')


@pytest.fixture(scope='module')
def measured_memory_clone(measured_browser):
    with measured_browser.open(':memory:') as consumer:
        yield consumer


def test_measured_memory_clone_keeps_sql_home_and_full_bundle(measured_memory_clone, measured_browser):
    assert_browser(measured_memory_clone, measured_browser)
    assert measured_memory_clone._fixture_bundle_payload == measured_browser.bundles


@pytest.fixture(scope='module')
def measured_disk_clone(measured_browser, tmp_path_factory):
    with measured_browser.open(tmp_path_factory.mktemp('measured-consumer') / 'consumer.db') as consumer:
        yield consumer


def test_measured_disk_clone_keeps_sql_home_and_full_bundle(measured_disk_clone, measured_browser):
    assert_browser(measured_disk_clone, measured_browser)
    assert measured_disk_clone._fixture_bundle_payload == measured_browser.bundles
    assert measured_disk_clone.conn.execute('PRAGMA journal_mode').fetchone()[0] == 'wal'


@pytest.mark.parametrize('consumer', ['source', 'memory', 'disk'])
def test_measured_clone_decodes_all_480_profiles_and_keeps_typed_heads(
        consumer, measured_browser, measured_memory_clone, measured_disk_clone):
    owner = {'source': measured_browser.source, 'memory': measured_memory_clone, 'disk': measured_disk_clone}[consumer]
    keys = {item['key'] for items in measured_browser.bundles['all'].values() for item in items}
    assert len(keys) == 480
    actual = owner.musical_profiles.by_keys(keys)
    expected = measured_browser.source.musical_profiles.by_keys(keys)
    assert len(actual) == 480 and actual == expected
    assert owner.recommendations.heads() == measured_browser.source.recommendations.heads()


def test_measured_clones_copy_every_retained_file_with_distinct_inodes(
        measured_browser, measured_memory_clone, measured_disk_clone):
    destinations = [roots(owner.conn) for owner in (measured_memory_clone, measured_disk_clone)]
    copied = 0
    for name, directory in measured_browser.files.items():
        for source in directory.rglob('*'):
            if not source.is_file():
                continue
            paths = [source, *(owned[name] / source.relative_to(directory) for owned in destinations)]
            assert all(path.read_bytes() == source.read_bytes() for path in paths)
            assert len({(path.stat().st_dev, path.stat().st_ino) for path in paths}) == 3
            copied += 1
    assert copied


def test_measured_clones_keep_profile_corruption_and_sql_nested_mutations_private(
        measured_browser, measured_memory_clone, measured_disk_clone):
    first, second = measured_memory_clone, measured_disk_clone
    publication = first.conn.execute('SELECT publication FROM recording_profile_files LIMIT 1').fetchone()[0]
    relative = profile_files.relative('recording', publication)
    original = (profile_files.directory(second.conn) / relative).read_bytes()
    (profile_files.directory(first.conn) / relative).write_bytes(b'Private corrupted profile')
    first.settings.set_setting('private_sound_fixture', 'first consumer')
    first._fixture_bundle_payload['_vibes']['pools']['relax']['fresh'][0]['musical']['dimensions']['intensity']['value'] = -1
    for target in (measured_browser.source, second):
        assert (profile_files.directory(target.conn) / relative).read_bytes() == original
        assert target.settings.get_setting('private_sound_fixture') is None
        assert target._fixture_bundle_payload == measured_browser.bundles
