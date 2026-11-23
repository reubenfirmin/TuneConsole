"""Installed ranking snapshots track their current normalized input owners."""
from contextlib import ExitStack

import pytest

from tests.core.test_predecessor_writes import installed
from tests.genre_catalog_support import bound_catalog
from tests.prepared_fixture_support import assert_upgrade_phase, historical_template
from tests.rec.test_ranking_playback_publication import _seed_ranker_inputs, completed_policy
from tests.store_fixture_support import reuse_reporting_reader
from yt_playlist.core import genre_identity, radio_provenance
from yt_playlist.rec import album_feedback_state, candidate_refresh, measurement, mode_inputs, mode_surfaces, vibe_pools


@pytest.fixture(autouse=True)
def disable_periodic_library_sync():
    """These ranking and snapshot owners do not construct an HTTP app."""


@pytest.fixture(scope='module', autouse=True)
def isolated_ranking_home(tmp_path_factory):
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv('YT_PLAYLIST_HOME', str(tmp_path_factory.mktemp('installed-ranking-home')))
        bound_catalog(patch, entries=(('rock', 'Rock'), ('jazz', 'Jazz'), ('techno', 'Techno')))
        yield


@pytest.fixture(scope='module')
def installed_ranking_inputs(_bounded_installed_schema, tmp_path_factory):
    with ExitStack() as resources:
        templates = {}

        def seed(store):
            with store.conn:
                genre_identity.seed_catalog(store.conn)
            _seed_ranker_inputs(store)

        def get(version):
            if version not in templates:
                completed = _bounded_installed_schema(version)
                templates[version] = resources.enter_context(historical_template(
                    completed.source.conn, seed, completed=completed,
                    database=tmp_path_factory.mktemp('installed-ranking-input') / 'source.db'))
            return templates[version]

        yield get


@pytest.mark.parametrize('version', [107])
def test_installed_ranking_schema_input_keeps_history_and_private_snapshot_guards(
        _bounded_installed_schema, version):
    assert_upgrade_phase(_bounded_installed_schema(version), version)


@pytest.mark.parametrize('version', [107])
def test_installed_ranking_input_keeps_admitted_models_membership_and_discovery(
        installed_ranking_inputs, version):
    from yt_playlist.rec import taste_modes

    template = installed_ranking_inputs(version)
    assert_upgrade_phase(template, version)
    assert template.source.conn.execute('SELECT count(*) FROM playlist_tracks').fetchone()[0] == 12
    assert template.source.conn.execute('SELECT count(DISTINCT track_id) FROM playlist_tracks').fetchone()[0] == 9
    assert len(template.source.accounts.get_accounts()) == 1
    assert len(template.source.playlists.get_playlists()) == 2
    assert taste_modes.live_modes(template.source)


@pytest.fixture
def installed_ranker(request, installed_ranking_inputs, tmp_path, monkeypatch):
    with installed_ranking_inputs(request.param).open(tmp_path / 'rankings.db') as store:
        # Reporting-pool admission is below the ranking boundary. The ranking
        # still owns its separate real pinned snapshot during the live write.
        with reuse_reporting_reader(store, monkeypatch):
            yield store, request.param


@pytest.mark.parametrize('installed_ranker', [107], indirect=True)
def test_real_installed_rankings_publish_after_listening_index_invalidation(installed_ranker, monkeypatch):
    store, version = installed_ranker
    # Completed models are lower inputs. Actual ranking, concurrent listening,
    # settings deletion, file publication and selected cards remain real.
    monkeypatch.setattr(measurement, 'maintain_storage', lambda *args: True)
    monkeypatch.setattr(measurement, 'serving_policy', completed_policy)
    monkeypatch.setattr(candidate_refresh, 'prepared_settings',
                        lambda reader, now: {'rec_candidate_preferences': 'installed-ranker-inputs'})
    # Optional six-preset production has independent worker/serving owners.
    # This case verifies installed Home/Fresh rankings across a listening write.
    monkeypatch.setattr(vibe_pools, 'prepare', lambda reader, payload, now, policies: payload)
    # Real older libraries retain these cached index markers. An empty fixture
    # misses the settings deletion performed by the public listening write.
    store.settings.set_setting('trend_first_play_source', 'canonical_listening')
    store.settings.set_setting('trend_first_play_watermark', 'captured')
    calculate = mode_inputs._calculate_bundles
    ranked = []

    def during_playback(reader, now, policies):
        result = calculate(reader, now, policies)
        account = store.accounts.get_accounts()[0].id
        assert store.history.record_play_event(
            account, store.tracks.identity_key_for_video('v0'), 'v0', 150000)
        if version == 93:
            assert store.settings.get_setting('trend_first_play_source') is None
            assert store.settings.get_setting('trend_first_play_watermark') is None
        ranked.append(result[0])
        return result

    monkeypatch.setattr(mode_inputs, '_calculate_bundles', during_playback)
    result = mode_surfaces.prepare_bundles(store, 2000)
    assert result is ranked[0]
    assert result['all']['wheelhouse']
    assert store.recommendations.head('home')['id'] == store.recommendations.head('fresh')['id']
    with store.read_view() as reader:
        page = reader.recommendations.candidate_page('home', 'all', 'wheelhouse', limit=10)
        assert page['tracks']
    assert not store.conn.execute('PRAGMA foreign_key_check').fetchall()


def read_setting_owner(store, key):
    if key == 'album_feedback':
        return album_feedback_state.state(store)
    return store.playlists.radio_playlist_ids()


def change_owner(store, key, change, now):
    if key == 'album_feedback':
        store.album_feedback.replace('release', 'clear' if change == 'delete' else
                                     'known' if change == 'update' else 'more', now)
    else:
        with store.conn:
            if change != 'insert':
                store.conn.execute('DELETE FROM settings WHERE key=?', (radio_provenance.UNRESOLVED,))
            if change != 'delete':
                radio_provenance.remember(store.conn, 'radio-' + str(now))


@pytest.mark.parametrize('installed', [107], indirect=True)
@pytest.mark.parametrize('key', ['album_feedback', 'radio_origins'])
@pytest.mark.parametrize('change', ['insert', 'update', 'delete'])
def test_installed_feedback_and_radio_owners_reject_changed_inputs(installed, key, change):
    store, _ = installed
    store.accounts.upsert_account('Main', None, True)
    if change != 'insert':
        change_owner(store, key, 'insert', 1000)
    with store.read_snapshot() as snapshot:
        read_setting_owner(snapshot.store, key)
        change_owner(store, key, change, 1001)
        assert not snapshot.publish(lambda: pytest.fail('Published changed feedback or Radio origins'))


@pytest.mark.parametrize('installed', [107], indirect=True)
@pytest.mark.parametrize('key', ['album_feedback', 'radio_origins'])
def test_installed_owner_readers_ignore_unrelated_index_settings(installed, key):
    store, _ = installed
    store.accounts.upsert_account('Main', None, True)
    store.settings.set_setting('trend_first_play_source', 'canonical_listening')
    with store.read_snapshot() as snapshot:
        read_setting_owner(snapshot.store, key)
        with store.conn:
            store.conn.execute("DELETE FROM settings WHERE key='trend_first_play_source'")
        assert snapshot.publish(lambda: True)
