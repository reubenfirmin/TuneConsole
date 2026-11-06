"""Artist identity policy is exercised without catalog fixtures."""
from contextlib import contextmanager
from unittest.mock import create_autospec

import pytest

from tests.repository_mock_support import no_database
from yt_playlist.controllers.artists import Artists
from yt_playlist.repos.artists import ArtistRepo

pytestmark = pytest.mark.usefixtures('no_database')


@pytest.fixture
def artists():
    repository = create_autospec(ArtistRepo, instance=True, spec_set=True)
    repository.provider_id.return_value = None
    repository.provisional_id.return_value = None
    repository.create.return_value = 42
    writing = False

    @contextmanager
    def transaction():
        nonlocal writing
        writing = True
        try:
            yield
        finally:
            writing = False

    def create(name, *, provisional):
        assert writing
        return 42

    repository.transaction.side_effect = transaction
    repository.create.side_effect = create
    return Artists(repository)


def test_known_provider_identity_is_reused_even_when_the_display_name_changes(artists):
    artists.repository.provider_id.return_value = 7
    assert artists.ensure_artist('New name', provider='youtube', provider_id='channel') == 7
    artists.repository.provider_id.assert_called_once_with('youtube', 'channel')
    artists.repository.add_alias.assert_called_once_with(7, 'New name', 'youtube')
    artists.repository.provisional_id.assert_not_called()
    artists.repository.create.assert_not_called()
    artists.repository.add_provider.assert_not_called()


def test_new_provider_identity_does_not_adopt_an_unproven_same_name_artist(artists):
    artists.repository.provisional_id.return_value = 7
    assert artists.ensure_artist('Same name', provider='youtube', provider_id='channel') == 42
    artists.repository.provisional_id.assert_not_called()
    artists.repository.create.assert_called_once_with('Same name', provisional=False)
    artists.repository.add_provider.assert_called_once_with(42, 'youtube', 'channel')
    artists.repository.add_alias.assert_called_once_with(42, 'Same name', 'youtube')


def test_unidentified_name_reuses_its_provisional_identity(artists):
    artists.repository.provisional_id.return_value = 7
    assert artists.ensure_artist('Unidentified') == 7
    artists.repository.provisional_id.assert_called_once_with('Unidentified')
    artists.repository.add_alias.assert_called_once_with(7, 'Unidentified', 'unidentified_credit')
    artists.repository.provider_id.assert_not_called()
    artists.repository.create.assert_not_called()


@pytest.mark.parametrize('provider,provider_id,source', [
    (None, None, 'unidentified_credit'), ('youtube', None, 'youtube'),
    (None, 'channel', 'unidentified_credit'), ('', 'channel', 'unidentified_credit'),
    ('youtube', '', 'youtube'),
])
def test_incomplete_provider_evidence_creates_a_provisional_identity(artists, provider, provider_id, source):
    assert artists.ensure_artist('Unidentified', provider=provider, provider_id=provider_id) == 42
    artists.repository.create.assert_called_once_with('Unidentified', provisional=True)
    artists.repository.add_alias.assert_called_once_with(42, 'Unidentified', source)
    artists.repository.provider_id.assert_not_called()
    artists.repository.add_provider.assert_not_called()


def test_blank_display_name_does_not_create_an_alias(artists):
    artists.repository.provider_id.return_value = 7
    assert artists.ensure_artist('', provider='youtube', provider_id='channel') == 7
    artists.repository.add_alias.assert_not_called()
