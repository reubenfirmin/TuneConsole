"""Playlist policy and ingestion ordering use mocked repository boundaries."""
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import Mock, create_autospec

import pytest

from tests.repository_mock_support import no_database
from yt_playlist.controllers.playlists import Playlists
from yt_playlist.controllers.radio_checkpoints import RadioCheckpoints
from yt_playlist.library.playlist_metadata import GENERATED_DESCRIPTION, PROMOTED_DESCRIPTION, content_hash
from yt_playlist.repos.playlists import PlaylistRepo
from yt_playlist.repos.tracks import TrackRepo

pytestmark = pytest.mark.usefixtures('no_database')


@pytest.fixture
def playlists():
    repo = create_autospec(PlaylistRepo, instance=True, spec_set=True)
    tracks = create_autospec(TrackRepo, instance=True, spec_set=True)
    radio = create_autospec(RadioCheckpoints, instance=True, spec_set=True)
    repo.confirmed_facts.return_value = {'id': 4, 'videos': [], 'unresolved': False}
    return Playlists(repo, tracks, radio, nullcontext)


@pytest.mark.parametrize('types,kind', [([], ''), ([None], 'mix'),
    (['MUSIC_VIDEO_TYPE_ATV', None], 'audio'), (['MUSIC_VIDEO_TYPE_OMV', 'OTHER'], 'video'),
    (['MUSIC_VIDEO_TYPE_ATV', 'MUSIC_VIDEO_TYPE_OMV'], 'mixed')])
def test_media_classification_is_product_policy(playlists, types, kind):
    playlists.repository.playlist_video_types.return_value = types
    assert playlists.playlist_kind(4) == kind


def test_generated_description_and_explicit_adoption_are_supplied_to_sql(playlists):
    playlists.mark_generated_playlist('scope')
    playlists.repository.set_playlist_provenance.assert_called_once_with(
        'scope', origin='tuneconsole', adopted=False, description=GENERATED_DESCRIPTION)
    playlists.adopt_generated_playlist('scope')
    playlists.repository.adopt_generated_playlist.assert_called_once_with('scope', PROMOTED_DESCRIPTION)


@pytest.mark.parametrize('description', [None, GENERATED_DESCRIPTION, 'Ordinary remote description'])
def test_stale_or_missing_remote_marker_cannot_reverse_explicit_adoption(playlists, description):
    playlists.repository.playlist_reference.return_value = 12
    playlists.repository.playlist_provenance.return_value = {
        'origin': 'tuneconsole', 'adopted': 1, 'description': PROMOTED_DESCRIPTION}
    playlists.reconcile_playlist_provenance('remote', description, account_id=3)
    playlists.repository.set_playlist_provenance.assert_called_once_with(12, origin='tuneconsole', adopted=1,
        description=description if description is not None else PROMOTED_DESCRIPTION)


def test_ordinary_playlist_does_not_acquire_generated_provenance(playlists):
    playlists.repository.playlist_provenance.return_value = {'origin': 'ordinary', 'adopted': 1, 'description': None}
    playlists.reconcile_playlist_provenance('remote', 'My playlist')
    playlists.repository.set_playlist_provenance.assert_not_called()


def test_cleanup_classifies_provider_types_and_retains_unique_recording_keys(playlists):
    playlists.repository.cleanup_membership_facts.return_value = {
        'playlists': [SimpleNamespace(id=4, playlist_scope_id=12), SimpleNamespace(id=5, playlist_scope_id=13)],
        'keys': {4: {'one'}, 5: {'two'}}, 'types': {4: {'MUSIC_VIDEO_TYPE_OMV', 'OTHER'}, 5: set()}, 'excluded': {4}}
    result = playlists.cleanup_memberships()
    assert result.kinds == {12: 'video', 13: 'mix'}
    assert result.keys == {4: frozenset({'one'}), 5: frozenset({'two'})}
    assert result.excluded == {4}


def test_snapshot_hashes_canonical_distinct_entries_before_sql_publication(playlists):
    playlists.repository.playlist_revision.return_value = 7
    playlists.repository.snapshot_track_facts.return_value = [
        {'id': 8, 'identity_key': 'same'}, {'id': 8, 'identity_key': 'same'}, {'id': 9, 'identity_key': 'same'}]
    playlists.publish_playlist_snapshot(3, 'remote', 'Title', [1, 8, 9], 10, expected_revision=7)
    playlists.repository.publish_playlist_snapshot.assert_called_once_with(3, 'remote', 'Title', [8, 9], 10,
        expected_revision=7, digest=content_hash(['same', 'same']), thumbnail=None, created_at=None)


def test_stale_snapshot_does_not_resolve_or_publish_membership(playlists):
    playlists.repository.playlist_revision.return_value = 8
    assert playlists.publish_playlist_snapshot(3, 'remote', 'Old', [1], 10, expected_revision=7) is None
    playlists.repository.snapshot_track_facts.assert_not_called()
    playlists.repository.publish_playlist_snapshot.assert_not_called()


def test_confirmed_tracks_commit_catalog_observations_then_hash_final_identities_then_publish(playlists):
    playlists.tracks.upsert_track.side_effect = [8, 9]
    playlists.tracks.identity_keys_for_videos.return_value = {'one': 'reconciled', 'two': 'reconciled'}
    order = Mock()
    order.attach_mock(playlists.tracks.upsert_track, 'observe')
    order.attach_mock(playlists.tracks.identity_keys_for_videos, 'identities')
    order.attach_mock(playlists.repository.publish_confirmed, 'publish')
    playlists.publish_confirmed_tracks(4, 'Title', [{'video_id': 'one'}, {'video_id': 'two'}], 10)
    assert [call[0] for call in order.mock_calls] == ['observe', 'observe', 'identities', 'publish']
    assert all(call.kwargs['library'] is False for call in playlists.tracks.upsert_track.call_args_list)
    args = playlists.repository.publish_confirmed.call_args.args
    assert args == (4, 'Title', ['one', 'two'], content_hash({'reconciled'}), [8, 9], 10)


@pytest.mark.parametrize('saved,unresolved,observed', [(['one'], False, ['two']),
    (['different'], False, ['one', 'two']), (['one'], True, ['one', 'two'])])
def test_incremental_ingestion_requires_exact_prefix_and_no_unresolved_members(playlists, saved, unresolved, observed):
    playlists.repository.confirmed_facts.return_value = {'id': 4, 'videos': saved, 'unresolved': unresolved}
    playlists.tracks.identity_keys_for_videos.return_value = {'one': 'a', 'two': 'b'}
    playlists.publish_confirmed_tracks(4, 'Title', [{'video_id': 'one'}, {'video_id': 'two'}], 10, expected_prefix=['one'])
    assert [call.args[0] for call in playlists.tracks.upsert_track.call_args_list] == observed


@pytest.mark.parametrize('tracks', [[{'video_id': ''}], [{'video_id': 'one'}, {'video_id': 'one'}]])
def test_invalid_remote_sequence_does_not_partially_ingest_catalog(playlists, tracks):
    with pytest.raises(ValueError, match='distinct playable uploads'):
        playlists.publish_confirmed_tracks(4, 'Title', tracks, 10)
    playlists.tracks.upsert_track.assert_not_called()
    playlists.repository.publish_confirmed.assert_not_called()


def test_failed_catalog_observation_does_not_publish_partial_playlist(playlists):
    playlists.tracks.upsert_track.side_effect = [8, OSError('file failed')]
    with pytest.raises(OSError):
        playlists.publish_confirmed_tracks(4, 'Title', [{'video_id': 'one'}, {'video_id': 'two'}], 10)
    assert playlists.tracks.upsert_track.call_count == 2
    playlists.repository.publish_confirmed.assert_not_called()


def test_unknown_playlist_is_rejected_before_catalog_ingestion(playlists):
    playlists.repository.confirmed_facts.return_value = None
    with pytest.raises(ValueError, match='Unknown playlist'):
        playlists.publish_confirmed_tracks(4, 'Title', [{'video_id': 'one'}], 10)
    playlists.tracks.upsert_track.assert_not_called()


def test_song_like_projects_only_into_existing_liked_music_membership(playlists):
    playlists.repository.liked_membership_target.return_value = None
    playlists.set_song_liked(3, 'video', True)
    playlists.repository.set_liked_membership.assert_not_called()
    playlists.repository.liked_membership_target.return_value = 4
    playlists.set_song_liked(3, 'video', True)
    playlists.repository.liked_membership_target.assert_called_with(3, 'LM', 'video')
    playlists.repository.set_liked_membership.assert_called_once_with(4, 'video', True)
