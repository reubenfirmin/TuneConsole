from contextlib import nullcontext
from copy import deepcopy
from unittest.mock import Mock, create_autospec

import pytest

from tests.repository_mock_support import no_database
from yt_playlist.controllers.album_aliases import AlbumAliases
from yt_playlist.controllers.albums import Albums
from yt_playlist.controllers.discovery import Discovery
from yt_playlist.controllers.retained_originals import RetainedOriginals
from yt_playlist.core.album_aliases import compatible
from yt_playlist.repos.album_aliases import AlbumAliasesRepo

pytestmark = pytest.mark.usefixtures('no_database')


@pytest.fixture
def aliases():
    repository = create_autospec(AlbumAliasesRepo, instance=True, spec_set=True)
    albums = create_autospec(Albums, instance=True, spec_set=True)
    originals = create_autospec(RetainedOriginals, instance=True, spec_set=True)
    discovery = create_autospec(Discovery, instance=True, spec_set=True)
    controller = AlbumAliases(repository, albums, originals, discovery, Mock(), nullcontext)
    repository.fingerprint_inputs.return_value = {'shared': False, 'digest': None}
    repository.remember_fingerprint.return_value = [1]
    albums.observe.return_value = 2
    repository.merge_inputs.return_value = {'conflict': False, 'original': {'albums': [{'id': 2}]},
        'target': {'tracks': [], 'revisions': [], 'state': None},
        'retired': {'tracks': [], 'revisions': [], 'state': None}}
    return controller


def release():
    return {'title': 'LP', 'artists': [{'name': 'Artist'}], 'year': '2020', 'trackCount': 1,
            'tracks': [{'videoId': 'video', 'title': 'Song', 'duration': '3:00', 'isExplicit': False}]}


def test_alias_identity_publishes_after_originals_and_independent_owners(aliases):
    calls = Mock()
    for name, collaborator in [('originals', aliases.originals), ('discovery', aliases.discovery),
                               ('feedback', aliases.merge_feedback), ('repository', aliases.repository)]:
        calls.attach_mock(collaborator, name)
    assert aliases.observe('B', release(), now=100)
    names = [call[0] for call in calls.mock_calls]
    assert names.index('originals.retain') < names.index('discovery.merge_albums') < names.index('feedback')
    assert names.index('feedback') < names.index('repository.apply_merge')
    aliases.discovery.merge_albums.assert_called_once_with(2, 1)
    aliases.merge_feedback.assert_called_once_with(1, 2, 100)
    target, retired, digest, now = aliases.repository.apply_merge.call_args.args
    assert (target, retired, now) == (1, 2, 100)
    assert aliases.originals.retain.call_args.args[:4] == (
        'album', 'album_alias_reconciliation', '2', '{"albums": [{"id": 2}]}')
    assert digest in aliases.originals.retain.call_args.args[4]


@pytest.mark.parametrize('stage', ['originals', 'discovery', 'feedback'])
def test_owner_failure_cannot_publish_alias_identity(aliases, stage):
    operation = {'originals': aliases.originals.retain, 'discovery': aliases.discovery.merge_albums,
                 'feedback': aliases.merge_feedback}[stage]
    operation.side_effect = OSError('interrupted')
    with pytest.raises(OSError, match='interrupted'):
        aliases.observe('B', release(), now=100)
    aliases.repository.apply_merge.assert_not_called()


@pytest.mark.parametrize('shared', [False, True])
def test_incomplete_comparison_does_not_change_existing_proof(aliases, shared):
    aliases.repository.fingerprint_inputs.return_value = {'shared': shared, 'digest': 'original'}
    album = release()
    album['tracks'][0].pop('isExplicit')
    assert aliases.observe('B', album) is not shared
    aliases.albums.observe.assert_not_called()
    aliases.repository.remember_fingerprint.assert_not_called()


def test_changed_known_alias_cannot_replace_metadata_or_tracks(aliases):
    aliases.repository.fingerprint_inputs.return_value = {'shared': True, 'digest': 'original'}
    assert not aliases.observe('B', release())
    aliases.albums.observe.assert_not_called()
    aliases.repository.remember_fingerprint.assert_not_called()


@pytest.mark.parametrize('reason', ['provider', 'bonus', 'unknown-order', 'incomplete-state'])
def test_conflicting_retained_evidence_prevents_merge(aliases, reason):
    inputs = aliases.repository.merge_inputs.return_value
    if reason == 'provider':
        inputs['conflict'] = True
    elif reason == 'bonus':
        inputs['retired']['tracks'] = [{'video_id': 'bonus', 'is_full': False, 'position': None}]
    elif reason == 'unknown-order':
        inputs['retired']['revisions'] = [{'status': 'complete', 'order_status': 'unknown',
                                         'occurrences': [{'video_id': 'video', 'position': 0}]}]
    else:
        inputs['retired']['tracks'] = [{'video_id': 'video', 'is_full': True, 'position': 0}]
    assert aliases.observe('B', release())
    aliases.originals.retain.assert_not_called()
    aliases.repository.apply_merge.assert_not_called()


def test_repeated_occurrences_and_retained_partial_revisions_are_compatible():
    inputs = {'tracks': [], 'state': None, 'revisions': [
        {'status': 'complete', 'order_status': 'known', 'occurrences': [
            {'video_id': 'video', 'position': 0}, {'video_id': 'video', 'position': 1}]},
        {'status': 'partial', 'order_status': 'unknown', 'occurrences': [{'video_id': 'video', 'position': None}]}]}
    before = deepcopy(inputs)
    assert compatible(inputs, ['video', 'video'])
    assert inputs == before
