"""Credential and account commands with bounded collaborators and no SQLite."""
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import Mock, create_autospec

import pytest

from tests.repository_mock_support import no_database
from yt_playlist.controllers.access_configuration import AccessConfiguration, precedence
from yt_playlist.controllers.account_configuration import AccountConfiguration, LastIdentity
from yt_playlist.controllers.settings import Settings
from yt_playlist.core.accounts import AccountAccess
from yt_playlist.core.action_errors import ActionInputError
from yt_playlist.core.config_files import ConfigFiles
from yt_playlist.rec import access, account_growth, artist_start, home_preferences, onboarding

pytestmark = pytest.mark.usefixtures('no_database')


def test_credential_sources_short_circuit_failed_lower_sources():
    settings = create_autospec(Settings, instance=True, spec_set=True)
    files = create_autospec(ConfigFiles, instance=True, spec_set=True)
    settings.get_setting.side_effect = RuntimeError('settings unavailable')
    files.read.side_effect = OSError('file unavailable')
    config = AccessConfiguration(settings, environment={'KEY': ' env '}, files=files)
    assert config.credential('KEY', 'key') == 'env'
    settings.get_setting.assert_not_called()
    files.read.assert_not_called()
    settings.get_setting.side_effect = None
    settings.get_setting.return_value = ' saved '
    config.environment.clear()
    assert config.credential('KEY', 'key') == 'saved'
    files.read.assert_not_called()
    settings.get_setting.return_value = None
    files.read.side_effect = None
    files.read.return_value = b'key = "file"'
    assert config.credential('KEY', 'key') == 'file'
    assert precedence(' ', ' settings ', 'file') == 'settings'


def test_live_credential_source_rechecks_after_replacement():
    environment = {'KEY': 'first'}
    config = AccessConfiguration(environment=environment)
    credential = config.credential_source('KEY', 'key')
    assert credential() == 'first'
    environment['KEY'] = 'second'
    assert credential() == 'second'


def test_exact_playlist_owner_cannot_fall_back_to_connected_main():
    accounts = Mock(spec_set=['get_accounts'])
    playlists = Mock(spec_set=['get_playlist'])
    accounts.get_accounts.return_value = [SimpleNamespace(id=1, is_master=True)]
    playlists.get_playlist.return_value = SimpleNamespace(account_id=2)
    command = AccountAccess(accounts, playlists, {1: object()})
    with pytest.raises(ActionInputError) as error:
        command.resolve(playlist_id=7)
    assert error.value.code == 'account_disconnected'


def test_removal_reconciles_authentication_only_after_successful_publication():
    row = lambda identity, master: SimpleNamespace(id=identity, label=str(identity),
        credential_ref='bridge', brand_account_id=None, is_master=master)
    accounts = Mock(spec_set=['get_accounts'])
    accounts.get_accounts.side_effect = [[row(1, True), row(2, False)], [row(2, True)]]
    runtime = Mock(spec_set=['apply_setup'])
    authentication = {1: 'one', 2: 'two'}
    clear = Mock(side_effect=lambda identity: authentication.pop(identity))
    command = AccountConfiguration(accounts, runtime, clear, lambda: authentication)
    result = command.remove(1)
    assert result['main_identity_id'] == 2 and result['auth_labels'] == ['two']
    clear.assert_called_once_with(1)
    assert runtime.apply_setup.call_args.args[0][0]['is_master'] is True


def test_failed_removal_keeps_authentication_and_last_account():
    row = SimpleNamespace(id=1, label='main')
    accounts = Mock(spec_set=['get_accounts'])
    accounts.get_accounts.return_value = [row]
    runtime, clear = Mock(spec_set=['apply_setup']), Mock()
    command = AccountConfiguration(accounts, runtime, clear, lambda: {1: 'main'})
    with pytest.raises(LastIdentity):
        command.remove(1)
    runtime.apply_setup.assert_not_called()
    clear.assert_not_called()


def test_earned_vibes_skips_all_tv_mix_and_preparation_reads():
    proposals = Mock(spec_set=['choice_usage'])
    proposals.choice_usage.return_value = {'vibe_episodes': 1}
    reader = SimpleNamespace(rec_proposals=proposals)
    @contextmanager
    def read_view():
        yield reader
    store = SimpleNamespace(read_view=read_view)
    assert access.check(store, 'vibes', now=10)
    assert account_growth._vibe_access(reader, 10)


def test_detached_starter_completion_and_defaults():
    assert artist_start.requires_artists(imported=True, picks=3, played=False)
    assert not artist_start.requires_artists(imported=True, picks=3, played=True)
    assert not artist_start.requires_artists(owned=True, imported=True)
    assert onboarding.select_starter(preference='radio', bootstrap={
        'seeds': [], 'state': {}, 'seedMenu': None}) == {'kind': 'library', 'radio_fallback': True}
    assert onboarding.completion(artists_required=True, remembered=True, mixes_ready=True,
                                 albums_ready=True, imported=True) == (False, 'artists')
    assert not onboarding.presented_offer('choice_window:mixes', {'opportunity_id': 'menu'}, {'options': []})
    assert home_preferences.select_format(override='albums', usage={'scores': {'mixes': 10}}) == 'albums'


def test_album_starter_query_owns_selection_without_radio_or_familiarity_acquisition():
    settings = Mock(spec_set=['get_setting'])
    settings.get_setting.return_value = 'albums'
    readiness = Mock(spec_set=['playable_owned', 'familiar_albums'])
    readiness.playable_owned.return_value = True
    collection = Mock(spec_set=['get_saved_albums'])
    collection.get_saved_albums.return_value = [
        {'browse': 'MPRE_single', 'title': 'Single', 'type': 'Single'},
        {'browse': 'MPRE_album', 'title': 'Album', 'type': 'Album'}]
    reader = SimpleNamespace(_read_only=True, settings=settings, readiness=readiness, collection=collection)
    @contextmanager
    def read_view():
        yield reader
    reader.read_view = read_view
    radio = Mock(spec_set=['bootstrap', 'history_tracks'])
    result = onboarding.starter(reader, radio, 100)
    assert result['kind'] == 'albums' and result['album']['browse'] == 'MPRE_album'
    assert result['album']['saved'] is True
    radio.bootstrap.assert_not_called()
    radio.history_tracks.assert_not_called()
    readiness.familiar_albums.assert_not_called()


def test_artist_selection_owns_catalog_projection_persistence_refresh_and_state():
    selected, tracks = [], []
    discovery = Mock(spec_set=['starter_artists', 'starter_songs', 'starter_tracks', 'select_starter_artist',
                               'upsert_discovered_track'])
    discovery.starter_artists.side_effect = lambda: selected
    discovery.starter_songs.return_value = []
    discovery.starter_tracks.side_effect = lambda: tracks
    discovery.select_starter_artist.side_effect = lambda name, browse, thumb, enabled, now: selected.append(
        {'browse_id': browse, 'name': name, 'thumbnail': thumb})
    discovery.upsert_discovered_track.side_effect = lambda *args, **kwargs: tracks.append({'video_id': args[1]})
    readiness = Mock(spec_set=['playable_owned'])
    readiness.playable_owned.return_value = True
    store = SimpleNamespace(_read_only=True, discovery=discovery, readiness=readiness,
                            settings=SimpleNamespace(get_setting=lambda key: None))
    client = Mock(spec_set=['get_artist'])
    client.get_artist.return_value = {'name': 'Artist', 'songs': {'results': [
        {'videoId': 'video', 'title': 'Song', 'duration': '3:00'}]}}
    def after_write():
        assert selected and tracks
    refresh = Mock(side_effect=after_write)
    result = artist_start.ArtistCatalog(lambda: {1: client}, trigger_refresh=refresh).choose(
        store, 'UCartist1', True, 100)
    client.get_artist.assert_called_once_with('UCartist1')
    refresh.assert_called_once()
    assert result['songs'] == 1 and result['selected'][0]['name'] == 'Artist'
    assert result['ready'] is False and result['complete'] is True
