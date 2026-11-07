"""Artist identity policy is exercised without catalog fixtures."""
from contextlib import contextmanager
from unittest.mock import Mock, create_autospec

import pytest

from tests.repository_mock_support import no_database
from yt_playlist.controllers.artists import Artists
from yt_playlist.controllers.artist_state import ArtistState
from yt_playlist.controllers.retained_originals import RetainedOriginals
from yt_playlist.repos.artists import ArtistRepo

pytestmark = pytest.mark.usefixtures('no_database')


@pytest.fixture
def artists():
    repository = create_autospec(ArtistRepo, instance=True, spec_set=True)
    repository.provider_id.return_value = None
    repository.provisional_id.return_value = None
    repository.create.return_value = 42
    repository.reference_rows.side_effect = lambda identities, names: ({}, {name: set() for name in names})
    repository.alias_rows.return_value = []
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
    def independent(*args, **kwargs):
        assert not writing, 'Feature writes must run after the identity transaction closes'
    state = create_autospec(ArtistState, instance=True, spec_set=True)
    state.reconcile_artist.side_effect = independent
    return Artists(repository, state, Mock(side_effect=independent),
                   create_autospec(RetainedOriginals, instance=True, spec_set=True))


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
    artists.repository.reference_rows.side_effect = None
    artists.repository.reference_rows.return_value = ({7: 7}, {})
    assert artists.state_id('@artist:7') == 7
    artists.repository.reference_rows.assert_called_once_with([7], [])
    artists.repository.transaction.assert_not_called()


@pytest.mark.parametrize('value', [999, '@artist:999', '@artist:invalid'])
def test_similarity_state_rejects_unknown_explicit_identity(artists, value):
    with pytest.raises(ValueError, match='Unknown artist reference'):
        artists.state_id(value)
    artists.repository.transaction.assert_not_called()


def test_similarity_state_admits_an_unknown_name_as_provisional(artists):
    assert artists.state_id('Unknown') == 42
    artists.repository.reference_rows.assert_called_once_with([], ['Unknown'])
    artists.repository.alias_rows.assert_not_called()
    artists.repository.create.assert_called_once_with('Unknown', provisional=True)


def identity(aid, *, provisional=False, identified=True, ambiguous=False):
    return dict(id=aid, exists=True, provisional=provisional, identified=identified, ambiguous=ambiguous)


def test_first_verified_context_promotes_its_provisional_identity(artists):
    artists.repository.identity_inputs.return_value = identity(7, provisional=True, identified=False)
    assert artists.ensure_artist('Name', provider='youtube', provider_id='channel', previous=7) == 7
    artists.repository.promote.assert_called_once_with(7)
    artists.repository.add_provider.assert_called_once_with(7, 'youtube', 'channel')
    artists.repository.create.assert_not_called()
    artists.reconcile_discovery.assert_not_called()


def test_weaker_refresh_keeps_the_exact_credit_identity(artists):
    artists.repository.identity_inputs.return_value = identity(7)
    assert artists.ensure_artist('Alias', previous=7) == 7
    artists.repository.provisional_id.assert_not_called()
    artists.repository.create.assert_not_called()


def test_ambiguous_context_cannot_promote_or_redirect_a_same_name_identity(artists):
    artists.repository.identity_inputs.return_value = identity(7, provisional=True, identified=False, ambiguous=True)
    assert artists.ensure_artist('Name', provider='youtube', provider_id='channel', previous=7) == 42
    artists.repository.create.assert_called_once_with('Name', provisional=False)
    artists.repository.promote.assert_not_called()
    artists.reconcile_discovery.assert_not_called()


def test_known_provider_moves_features_before_publishing_the_redirect(artists):
    before, after = identity(7, provisional=True, identified=False), identity(12)
    artists.repository.identity_inputs.side_effect = lambda aid: before if aid == 7 else after
    artists.repository.provider_id.return_value = 12
    events = Mock()
    events.attach_mock(artists.reconcile_discovery, 'discovery')
    events.attach_mock(artists.state.reconcile_artist, 'state')
    events.attach_mock(artists.repository.reconcile_identity, 'redirect')
    assert artists.ensure_artist('Name', provider='youtube', provider_id='channel', previous=7) == 12
    assert [call[0] for call in events.mock_calls] == ['discovery', 'state', 'redirect']
    artists.repository.add_alias.assert_called_once_with(12, 'Name', 'youtube')


def test_failed_feature_update_leaves_redirect_unpublished_and_can_retry(artists):
    artists.repository.identity_inputs.side_effect = lambda aid: identity(7, provisional=True, identified=False) if aid == 7 else identity(12)
    artists.state.reconcile_artist.side_effect = RuntimeError('interrupted feature')
    with pytest.raises(RuntimeError, match='interrupted feature'):
        artists.reconcile_provisional(7, 12, source='youtube', now=10)
    artists.repository.reconcile_identity.assert_not_called()
    artists.state.reconcile_artist.side_effect = None
    assert artists.reconcile_provisional(7, 12, source='youtube', now=10) == 12
    artists.repository.reconcile_identity.assert_called_once_with(7, 12, 'youtube', 10)


@pytest.mark.parametrize('before,after,expected', [
    (identity(7), identity(12), 12),
    (identity(7, provisional=True), identity(12), 12),
    (identity(7, provisional=True, identified=False, ambiguous=True), identity(12), 12),
    (identity(7, provisional=True, identified=False), identity(12, provisional=True, identified=False), 7),
    (identity(12), identity(12), 12),
])
def test_reconciliation_requires_an_unambiguous_provisional_and_verified_target(artists, before, after, expected):
    artists.repository.identity_inputs.side_effect = [before, after]
    assert artists.reconcile_provisional(7, 12, source='youtube') == expected
    artists.reconcile_discovery.assert_not_called()
    artists.state.reconcile_artist.assert_not_called()
    artists.repository.reconcile_identity.assert_not_called()


@pytest.mark.parametrize('other_albums,other_entries', [([2], []), ([], [3])])
def test_shared_unresolved_credit_retains_ambiguity_without_identifying_other_releases(artists, other_albums, other_entries):
    artists.repository.release_context.return_value = dict(identity=identity(7, provisional=True, identified=False),
        other_albums=other_albums, other_entries=other_entries)
    assert artists.release_subject(12, 7) is None
    artists.originals.retain.assert_called_once()
    artists.repository.mark_ambiguity.assert_called_once_with(7)
    artists.repository.reconcile_identity.assert_not_called()


def test_ambiguous_credit_cannot_be_reused_after_other_memberships_are_removed(artists):
    artists.repository.release_context.return_value = dict(
        identity=identity(7, provisional=True, identified=False, ambiguous=True), other_albums=[], other_entries=[])
    assert artists.release_subject(12, 7) is None
    artists.originals.retain.assert_not_called()


def test_credit_observation_validates_the_whole_structure_before_identity_changes(artists):
    artists.repository.credit_observation_inputs.return_value = dict(entry_id=7, typed=True, credits=[])
    with pytest.raises(ValueError, match='structured artist credit'):
        artists.observe(7, 'Artist', [{'name': 'Artist', 'id': 'channel', 'unexpected': True}])
    artists.repository.create.assert_not_called()
    artists.repository.add_alias.assert_not_called()
    artists.repository.store_credit.assert_not_called()


def test_exact_credit_context_and_delivery_are_preserved_in_observation(artists):
    existing = [{'artist_id': 7, 'credit': 'Artist'}]
    artists.repository.credit_observation_inputs.return_value = dict(entry_id=3, typed=True, credits=existing)
    artists.repository.credit_subject_candidates.return_value = {7}
    artists.repository.identity_inputs.return_value = identity(7, provisional=True, identified=False)
    artists.observe(99, 'Artist', [{'name': 'Artist', 'id': 'channel'}], metadata_delivery_id=10, now=20)
    args, kwargs = artists.repository.store_credit.call_args
    assert args[0] == 3 and args[1].members[0].artist_id == 7
    assert args[2:] == ([{'name': 'Artist', 'id': 'channel'}], 'youtube', 20)
    assert kwargs == dict(typed=True, delivery_key=None, metadata_delivery_id=10)


@pytest.mark.parametrize('browse_id,candidates', [(None, {7}), ('channel', set()), ('channel', {7, 8})])
def test_unstructured_refresh_cannot_replace_an_unproven_existing_credit(artists, browse_id, candidates):
    artists.repository.credit_observation_inputs.return_value = dict(
        entry_id=7, typed=True, credits=[{'artist_id': 7, 'credit': 'Artist'}])
    artists.repository.credit_subject_candidates.return_value = candidates
    artists.observe(7, 'Other', browse_id=browse_id)
    artists.repository.create.assert_not_called()
    artists.repository.store_credit.assert_not_called()


def test_exact_aliases_take_precedence_over_normalized_homonyms(artists):
    artists.repository.reference_rows.side_effect = None
    artists.repository.reference_rows.return_value = ({}, {'Artist': {7}})
    assert artists.resolve('Artist') == 7
    artists.repository.alias_rows.assert_not_called()


def test_ambiguous_exact_alias_does_not_fall_back_to_a_guess(artists):
    artists.repository.reference_rows.side_effect = None
    artists.repository.reference_rows.return_value = ({}, {'Artist': {7, 8}})
    assert artists.resolve('Artist') is None
    artists.repository.alias_rows.assert_not_called()


def test_normalized_lookup_scans_aliases_once_for_the_entire_batch(artists):
    artists.repository.alias_rows.return_value = [
        {'alias': 'ARTIST', 'artist_id': 7}, {'alias': 'artist', 'artist_id': 7},
        {'alias': 'Echo', 'artist_id': 8}, {'alias': 'ECHO', 'artist_id': 9}]
    assert artists.resolve_many(['Artist', 'echo', 'Missing']) == {'Artist': 7, 'echo': None, 'Missing': None}
    artists.repository.alias_rows.assert_called_once_with()


def test_explicit_missing_reference_never_uses_a_same_text_alias(artists):
    assert artists.resolve('@artist:999') is None
    artists.repository.reference_rows.assert_called_once_with([999], [])
    artists.repository.alias_rows.assert_not_called()


def test_public_keys_preserve_retired_handles_homonyms_and_unknown_values(artists):
    artists.repository.reference_rows.side_effect = None
    artists.repository.reference_rows.return_value = ({7: 12, 8: 8, 9: 9}, {'Missing': set()})
    artists.repository.public_key_inputs.return_value = ({12: 'Unique', 8: 'Echo', 9: 'Echo'}, [
        {'alias': 'Unique', 'artist_id': 12}, {'alias': 'UNIQUE', 'artist_id': 12},
        {'alias': 'Echo', 'artist_id': 8}, {'alias': 'ECHO', 'artist_id': 9}])
    assert artists.public_keys([7, '@artist:8', 9, 'Missing']) == {
        7: 'unique', '@artist:8': '@artist:8', 9: '@artist:9', 'Missing': 'Missing'}
    artists.repository.public_key_inputs.assert_called_once_with({12, 8, 9})


def test_public_artist_details_are_composed_from_resolved_storage_facts(artists):
    artists.repository.reference_rows.side_effect = None
    artists.repository.reference_rows.return_value = ({}, {'Alias': {7}})
    artists.repository.artist_row.return_value = {'id': 7, 'name': 'Artist', 'provider_ids': {'youtube': 'channel'}}
    assert artists.get_artist('Alias') == {
        'id': 7, 'name': 'Artist', 'provider_ids': {'youtube': 'channel'}, 'key': '@artist:7'}
    artists.repository.artist_row.assert_called_once_with(7)
