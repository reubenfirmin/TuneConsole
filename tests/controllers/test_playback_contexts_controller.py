"""Playback attribution policy without a database or file fixture."""
from contextlib import nullcontext
from unittest.mock import create_autospec

import pytest

from tests.repository_mock_support import no_database
from yt_playlist.controllers.playback_contexts import PlaybackContexts
from yt_playlist.repos.rec_ledger import RecLedgerRepo
from yt_playlist.controllers.tracks import Tracks
from yt_playlist.repos.collection import CollectionRepo

pytestmark = pytest.mark.usefixtures('no_database')


@pytest.fixture
def playback():
    return PlaybackContexts(create_autospec(RecLedgerRepo, instance=True, spec_set=True),
        create_autospec(Tracks, instance=True, spec_set=True),
        create_autospec(CollectionRepo, instance=True, spec_set=True), nullcontext)


def test_unknown_upload_is_observed_before_its_binding(playback):
    playback.repository.video_exists.return_value = False
    events = []
    playback.tracks.observe_track_entry.side_effect = lambda *args, **kwargs: events.append('catalog')
    playback.repository.put_choice_binding.side_effect = lambda *args, **kwargs: events.append('binding')
    playback.bind_choice(2, 'LM', 'video', 'menu', 3, 1000, 100)
    assert events == ['catalog', 'binding']
    playback.tracks.observe_track_entry.assert_called_once_with('video', '', '', preferred_key='@unresolved-video:video')
    playback.repository.put_choice_binding.assert_called_once_with(2, 'LM', 'video', 'menu', '3', 1000,
                                                                  before=900, limit=64, response_id=None)


def test_failed_catalog_observation_cannot_publish_a_binding(playback):
    playback.repository.video_exists.return_value = False
    playback.tracks.observe_track_entry.side_effect = OSError('full')
    with pytest.raises(OSError, match='full'):
        playback.bind_choice(2, 'LM', 'video', 'menu', 3, 1000, 100)
    playback.repository.put_choice_binding.assert_not_called()


def test_known_upload_is_not_reobserved_and_lookup_receives_explicit_window(playback):
    playback.repository.video_exists.return_value = True
    playback.bind_choice(2, 'LM', 'video', 'menu', 3, 1000, 100)
    playback.tracks.observe_track_entry.assert_not_called()
    playback.choice(2, 'LM', None, 1000, 100)
    playback.repository.choice_binding.assert_called_once_with(2, 'LM', None, before=900, through=1000)


def test_album_validation_precedes_every_catalog_write(playback):
    value = {'opportunity_id': 'menu', 'browse': 'album', 'playlist': 'queue', 'at': 10,
             'videos': ['valid', '']}
    with pytest.raises(ValueError, match='exact video IDs'):
        playback.remember_album(value)
    playback.collection.ensure_album_identity.assert_not_called()
    playback.tracks.observe_track_entry.assert_not_called()
    playback.repository.put_album_context.assert_not_called()


def test_album_and_unique_uploads_commit_before_queue_with_repeated_occurrences(playback):
    value = {'opportunity_id': 'menu', 'browse': 'album', 'playlist': 'queue', 'at': 10,
             'videos': ['video', 'video', 'known']}
    events = []
    playback.collection.ensure_album_identity.side_effect = lambda browse: events.append('album') or 5
    playback.repository.video_exists.side_effect = lambda video: video == 'known'
    playback.tracks.observe_track_entry.side_effect = lambda *args, **kwargs: events.append('video')
    playback.repository.put_album_context.side_effect = lambda *args: events.append('queue')
    playback.remember_album(value)
    assert events == ['album', 'video', 'queue']
    playback.repository.put_album_context.assert_called_once_with(value, 5)


def test_catalog_failure_preserves_previous_album_queue(playback):
    playback.repository.video_exists.return_value = False
    playback.tracks.observe_track_entry.side_effect = OSError('full')
    with pytest.raises(OSError, match='full'):
        playback.remember_album({'opportunity_id': 'menu', 'browse': 'album', 'playlist': 'queue', 'at': 10,
                                 'videos': ['video']})
    playback.repository.put_album_context.assert_not_called()


def test_clearing_album_does_not_observe_catalog_and_binding_uses_conditional_sql(playback):
    playback.remember_album(None)
    playback.repository.put_album_context.assert_called_once_with(None, None)
    playback.collection.ensure_album_identity.assert_not_called()
    playback.bind_album({'opportunity_id': 'menu', 'at': 10}, 2)
    playback.repository.bind_album_context.assert_called_once_with({'opportunity_id': 'menu', 'at': 10}, 2)


def test_album_reads_the_current_queue(playback):
    value = {'opportunity_id': 'menu', 'browse': 'album', 'playlist': 'queue', 'at': 10, 'videos': ['v', 'v']}
    playback.repository.album_context_values.return_value = value
    assert playback.album() == value
    playback.collection.ensure_album_identity.assert_not_called()
