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


def test_credit_correction_adds_an_alias_without_replacing_a_verified_identity(artists):
    artists.repository.credit_inputs.return_value = {'entry_id': 7, 'artists': [12], 'typed': True}
    artists.record_credit_choice(99, 'Correct spelling', now=10)
    artists.repository.add_alias.assert_called_once_with(12, 'Correct spelling', 'user_correction')
    artists.repository.create.assert_not_called()
    artists.repository.store_credit.assert_not_called()


def test_credit_correction_does_not_assign_one_alias_to_multiple_artists(artists):
    artists.repository.credit_inputs.return_value = {'entry_id': 7, 'artists': [12, 13], 'typed': True}
    artists.record_credit_choice(7, 'Display credit', now=10)
    artists.repository.add_alias.assert_not_called()
    artists.repository.store_credit.assert_not_called()


@pytest.mark.parametrize('typed', [True, False])
def test_uncredited_correction_prepares_exact_credit_before_persistence(artists, typed):
    artists.repository.credit_inputs.return_value = {'entry_id': 7, 'artists': [], 'typed': typed}
    artists.record_credit_choice(99, 'New credit', now=10)
    args, kwargs = artists.repository.store_credit.call_args
    assert args[0] == 7
    credit = args[1]
    assert credit.display_credit == 'New credit'
    assert [(m.artist_id, m.credit_text, m.presence) for m in credit.members] == [(42, 'New credit', 1)]
    assert args[2:] == ([{'name': 'New credit', 'id': None}], 'user_correction', 10)
    assert kwargs == {'typed': typed}
    artists.repository.add_alias.assert_called_once_with(42, 'New credit', 'user_correction')


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


def test_similarity_state_reuses_an_exact_artist_reference(artists):
    artists.repository.resolve.return_value = 7
    assert artists.state_id('@artist:7') == 7
    artists.repository.resolve.assert_called_once_with('@artist:7', exact=True)
    artists.repository.transaction.assert_not_called()


@pytest.mark.parametrize('value', [999, '@artist:999', '@artist:invalid'])
def test_similarity_state_rejects_unknown_explicit_identity(artists, value):
    artists.repository.resolve.return_value = None
    with pytest.raises(ValueError, match='Unknown artist reference'):
        artists.state_id(value)
    artists.repository.transaction.assert_not_called()


def test_similarity_state_admits_an_unknown_name_as_provisional(artists):
    artists.repository.resolve.return_value = None
    assert artists.state_id('Unknown') == 42
    artists.repository.resolve.assert_called_once_with('Unknown', exact=True)
    artists.repository.create.assert_called_once_with('Unknown', provisional=True)
