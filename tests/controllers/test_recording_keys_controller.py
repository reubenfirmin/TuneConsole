"""Alias workflows retain originals and rewrite documents outside database units."""
import json
from unittest.mock import Mock, create_autospec

import pytest

from tests.repository_mock_support import no_database
from yt_playlist.controllers.playlist_recipes import PlaylistRecipes
from yt_playlist.controllers.recording_keys import RecordingKeys
from yt_playlist.controllers.retained_originals import RetainedOriginals
from yt_playlist.controllers.road_trip import RoadTrips
from yt_playlist.controllers.listening_calendar import ListeningCalendar
from yt_playlist.repos.recording_keys import RecordingKeysRepo

pytestmark = pytest.mark.usefixtures('no_database')


@pytest.fixture
def keys():
    repository = create_autospec(RecordingKeysRepo, instance=True, spec_set=True)
    repository.add_alias.return_value = [
        {'id': 7, 'identity_key': 'song|artist', 'artist': 'Artist'},
        {'id': 8, 'identity_key': '@recording:8|artist', 'artist': 'Artist'}]
    repository.key_candidates.return_value = {'direct': None, 'aliases': {7, 8}}
    repository.rekey_inputs.return_value = {'feedback': [{'rowid': 9, 'kind': 'hide'}],
        'modes': [{'mode_id': 'mode', 'rep_keys': '["song|artist"]'}]}
    return RecordingKeys(repository,
        create_autospec(RetainedOriginals, instance=True, spec_set=True),
        create_autospec(PlaylistRecipes, instance=True, spec_set=True),
        create_autospec(RoadTrips, instance=True, spec_set=True),
        create_autospec(ListeningCalendar, instance=True, spec_set=True))


def test_ambiguous_alias_has_no_privileged_owner_and_retains_exact_feedback_before_rekey(keys):
    order = Mock()
    for mock, name in ((keys.originals.retain, 'original'), (keys.road_trips.rekey_recordings, 'draft'),
                       (keys.recipes.rekey_recordings, 'recipe'), (keys.repository.apply_rekey, 'sql')):
        order.attach_mock(mock, name)
    keys.add_alias('song|artist', 8, 'youtube')
    assert [call[0] for call in order.mock_calls] == ['original', 'draft', 'recipe', 'sql']
    args = keys.originals.retain.call_args.args
    assert args[:3] == ('recommendation', 'feedback_reassociation', '7:9:song|artist')
    assert json.loads(args[3]) == {'rowid': 9, 'kind': 'hide'}
    keys.repository.apply_rekey.assert_called_once_with('song|artist', '@recording:7|artist', 7,
        [{'rowid': 9, 'kind': 'hide'}], [('["@recording:7|artist"]', 'mode')])
    keys.repository.resolve_mood_seeds.assert_called_once_with('song|artist', None, 'ambiguous_alias')
    keys.repository.invalidate_catalog.assert_called_once_with()


def test_document_failure_leaves_catalog_key_unpublished_for_retry(keys):
    keys.recipes.rekey_recordings.side_effect = OSError('full')
    with pytest.raises(OSError, match='full'):
        keys.add_alias('song|artist', 8, 'youtube')
    keys.repository.apply_rekey.assert_not_called()
    keys.repository.invalidate_catalog.assert_not_called()
    keys.recipes.rekey_recordings.side_effect = None
    keys.add_alias('song|artist', 8, 'youtube')
    keys.repository.apply_rekey.assert_called_once()


def test_unique_alias_resolves_mood_without_rewriting_unrelated_documents(keys):
    keys.repository.add_alias.return_value = [{'id': 7, 'identity_key': 'song|artist', 'artist': 'Artist'}]
    keys.repository.key_candidates.return_value = {'direct': 7, 'aliases': {7}}
    keys.add_alias('song|artist', 7, 'youtube')
    keys.repository.resolve_mood_seeds.assert_called_once_with('song|artist', 7, None)
    keys.recipes.rekey_recordings.assert_not_called()
    keys.repository.apply_rekey.assert_not_called()


def test_listening_resolution_passes_only_unambiguous_keys_for_this_recording(keys):
    keys.repository.listening_keys.return_value = ['exact', 'alias', 'ambiguous', 'other']
    candidates = {'exact': (7, {7, 8}), 'alias': (None, {7}), 'ambiguous': (None, {7, 8}), 'other': (8, {7})}
    keys.repository.key_candidates.side_effect = lambda key: dict(zip(('direct', 'aliases'), candidates[key]))
    keys.repository.resolve_listening_keys.return_value = True
    keys.resolve_listening(7)
    keys.repository.resolve_listening_keys.assert_called_once_with(7, ['exact', 'alias'])
    keys.calendar.reconcile_recordings.assert_called_once_with({7})
