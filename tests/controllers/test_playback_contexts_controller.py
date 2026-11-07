"""Playback attribution policy without a database or file fixture."""
from contextlib import nullcontext
import json
from unittest.mock import create_autospec

import pytest

from tests.repository_mock_support import no_database
from yt_playlist.controllers.playback_contexts import PlaybackContexts, BINDINGS_KEY
from yt_playlist.repos.rec_ledger import RecLedgerRepo
from yt_playlist.repos.settings import SettingsRepo
from yt_playlist.controllers.tracks import Tracks
from yt_playlist.repos.collection import CollectionRepo

pytestmark = pytest.mark.usefixtures('no_database')


@pytest.fixture
def playback():
    return PlaybackContexts(create_autospec(RecLedgerRepo, instance=True, spec_set=True),
        create_autospec(SettingsRepo, instance=True, spec_set=True),
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
                                                                  before=900, limit=64)


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


def legacy_row(video, at, *, account=2):
    return {'identity_id': account, 'playlist': 'LM', 'video': video, 'at': at,
            'opportunity_id': 'old', 'option_id': '1'}


def test_legacy_binding_filters_future_expired_and_same_key_then_caps_in_arrival_order(playback):
    playback.repository.playback_bindings_available.return_value = False
    rows = [legacy_row(str(i), 950 + i % 5) for i in range(70)]
    rows += [legacy_row('future', 1001), legacy_row('expired', 899), legacy_row('video', 950)]
    playback.settings.get_setting.return_value = json.dumps(rows)
    playback.bind_choice(2, 'LM', 'video', 'new', 3, 1000, 100)
    key, raw = playback.settings.set_setting.call_args.args
    saved = json.loads(raw)
    assert key == BINDINGS_KEY and len(saved) == 64
    assert saved[0]['video'] == '7'
    assert saved[-1] == {**legacy_row('video', 1000), 'opportunity_id': 'new', 'option_id': '3'}
    playback.repository.put_choice_binding.assert_not_called()
    playback.tracks.observe_track_entry.assert_not_called()


@pytest.mark.parametrize('now,video,expected', [(1000, None, 'late'), (1000, 'exact', 'exact'),
                                               (1100, 'exact', None), (899, None, None)])
def test_legacy_lookup_keeps_account_video_and_inclusive_time_bounds(playback, now, video, expected):
    playback.repository.playback_bindings_available.return_value = False
    playback.settings.get_setting.return_value = json.dumps([
        legacy_row('exact', 900), legacy_row('late', 950), legacy_row('other-account', 999, account=3),
        legacy_row('future', 1101)])
    row = playback.choice(2, 'LM', video, now, 100)
    assert (row['video'] if row else None) == expected


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


def test_legacy_album_uses_selected_document_and_preserves_external_account_field(playback):
    playback.repository.album_context_available.return_value = False
    value = {'opportunity_id': 'menu', 'browse': 'album', 'playlist': 'queue', 'at': 10, 'videos': ['v', 'v']}
    playback.settings.get_setting.return_value = json.dumps(value)
    assert playback.album() == value
    playback.bind_album(value, 2)
    key, raw = playback.settings.set_setting.call_args.args
    assert key == 'listening_album_pending'
    assert json.loads(raw) == {**value, 'identity_id': 2}
    playback.collection.ensure_album_identity.assert_not_called()
