"""Library policies and complete commands run with detached facts and explicit ports."""
from copy import deepcopy
import json
from types import SimpleNamespace as NS
from unittest.mock import Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.repository_mock_support import no_database
from yt_playlist.controllers.collection_commands import CollectionCommands, removal_owner
from yt_playlist.controllers.generated_playlists import GeneratedPlaylists, reconciled_additions
from yt_playlist.controllers.library_actions import LibraryActions
from yt_playlist.controllers.merge_drafts import MergeDrafts
from yt_playlist.controllers.transfer_commands import TransferCommands
from yt_playlist.core.action_errors import ActionInputError
from yt_playlist.library.playlist_policy import (copy_memberships, generated_memberships, tv_continuation, occurrence_order,
    missing_occurrences, excess_occurrences, placement_moves, collection_targets, permission_fallback)
from yt_playlist.library.playlist_writes import PlaylistAddUncertain

pytestmark = pytest.mark.usefixtures('no_database')


def test_membership_plans_keep_recording_policy_separate_from_exact_uploads():
    sources = [[('song', 'upload-a'), ('song', 'upload-b'), ('other', 'upload-c')]]
    assert copy_memberships(sources, [], policy='upload') == sources[0]
    assert copy_memberships(sources, [], policy='recording') == [('song', 'upload-a'), ('other', 'upload-c')]
    assert copy_memberships(sources, [('song', 'unrelated-upload')], policy='recording') == [('other', 'upload-c')]
    assert sources[0][1] == ('song', 'upload-b')
    tracks = [dict(key=key, video_id=video, duration=length) for key, video, length in
              [('song', 'upload-a', 120), ('song', 'upload-b', 130), ('other', 'long', 1000)]]
    assert generated_memberships(tracks, maximum=300) == tracks[:2]
    assert generated_memberships(tracks, [('song', 'upload-a')], maximum=300) == tracks[1:2]
    old, additions = tv_continuation(tracks[:2], [('song', 'upload-a')], recipe={'videos': tracks[:1]})
    assert old == tracks[:1] and additions == tracks[1:2]
    old, additions = tv_continuation(tracks[:2], sources[0], recipe={'videos': tracks[:1]})
    assert old == tracks[:1] and additions == [], 'Live membership outside the recipe must not be appended again'


def test_occurrence_and_placement_plans_require_exact_handles_and_preserve_repeats():
    rows = [dict(videoId='a', setVideoId='first'), dict(videoId='b', setVideoId='middle'),
            dict(videoId='a', setVideoId='last')]
    assert occurrence_order(rows, ['a', 'a', 'b']) == [0, 2, 1]
    assert missing_occurrences(rows[:1], ['a', 'a', 'b']) == (['a', 'b'], True)
    assert excess_occurrences(rows, ['a', 'b']) == [rows[2]]
    assert placement_moves(rows, dict(video_ids=['a'], after_video_id='b')) == []
    plan = dict(video_ids=['x', 'y'], after_video_id='a', set_video_ids={'x': 'new-x', 'y': 'new-y'})
    assert placement_moves(rows[:2], plan) == [('new-x', 'middle'), ('new-y', 'middle')]
    with pytest.raises(ValueError, match='handle'):
        excess_occurrences([dict(videoId='a'), dict(videoId='a')], ['a'])


def test_collection_retry_targets_keep_absent_account_scoped_intentions():
    one = NS(id=1, account_id=1, provider_playlist_id='same')
    two = NS(id=2, account_id=2, provider_playlist_id='same')
    targets = collection_targets([one, two], {1, 2}, [(1, 'same', 200), (3, 'gone', 10)], 100)
    assert [(p.account_id, p.provider_playlist_id) for p in targets] == [(2, 'same'), (3, 'gone')]
    assert permission_fallback(403, 'delete_failed')
    assert not permission_fallback(403, 'delete_pending')


def test_collection_save_confirms_before_membership_evidence_and_refresh():
    calls = []
    collection = Mock(spec_set=['add_saved_album', 'remove_saved_album', 'saved_album_owners'])
    client = Mock(spec_set=['get_album', 'rate_playlist'])
    client.get_album.return_value = dict(title='Album', audioPlaylistId='audio')
    client.rate_playlist.side_effect = lambda *args: calls.append('provider') or {'status': 'STATUS_SUCCEEDED'}
    collection.add_saved_album.side_effect = lambda *args, **kwargs: calls.append('membership')
    access = Mock(spec_set=['resolve'])
    access.resolve.return_value = 7, client
    command = CollectionCommands(collection, access, now=lambda: 20,
        respond=lambda *args, **kwargs: calls.append('evidence'), refresh=lambda: calls.append('refresh'))
    assert command.save('album', 'offer')['account_id'] == 7
    assert calls == ['provider', 'membership', 'evidence', 'refresh']
    calls.clear()
    client.rate_playlist.side_effect = None
    client.rate_playlist.return_value = False
    with pytest.raises(ActionInputError):
        command.save('album', 'offer')
    assert calls == []
    with pytest.raises(ActionInputError):
        removal_owner([{'account_id': 1}, {'account_id': 2}])


def test_partial_addition_publishes_only_confirmed_effects_without_retry():
    ports = [Mock() for _ in range(7)]
    playlists, accounts, charts, tracks, recipes, events, queue = ports
    playlists.get_playlist.return_value = NS(provider_playlist_id='remote', playlist_scope_id=5)
    playlists.is_quarantined_playlist.return_value = False
    charts.playlist_tracks_detail.side_effect = [[], [dict(video_id='yes', genre='Jazz', track_id=3)]]
    failure = PlaylistAddUncertain('remote', ['yes'], [], ['unknown'])
    ops, measure, refresh = Mock(), Mock(), Mock()
    ops.add_tracks.side_effect = failure
    command = LibraryActions(*ports, ops=ops, clients=lambda: {}, now=lambda: 20,
        measure=measure, respond=Mock(), refresh_player=refresh, refresh_cleanup=Mock(),
        refresh_content=Mock(), wake_enrichment=Mock(), identity=lambda: 7, logger=Mock())
    with pytest.raises(PlaylistAddUncertain) as caught:
        command.add_tracks(1, [{'videoId': 'yes'}, {'videoId': 'unknown'}], 'old')
    assert caught.value is failure
    ops.add_tracks.assert_called_once()
    measure.assert_called_once_with('save', 20, video_id='yes', generated=False)
    refresh.assert_called_once_with('remote')
    assert events.record_player_event.call_args.args[2] == 'yes'


def test_generated_command_owns_effects_and_read_only_reconciliation():
    access, jobs, modes = Mock(), Mock(), Mock()
    client = Mock()
    access.resolve.return_value = 4, client
    create, measure, respond, enrich, navigate, refresh = [Mock() for _ in range(6)]
    create.return_value = dict(pid=9, new_ytm='exact', added=2)
    recipes = Mock()
    command = GeneratedPlaylists(access, jobs, modes, create=create, now=lambda: 50,
        measure=measure, respond=respond, enrich=enrich, navigate=navigate, refresh=refresh,
        query=Mock(), recipes=recipes)
    recipe = dict(model='fresh', mode_id=3, opportunity_id='offer', option_id='option')
    result = command.create('Title', [{'video_id': 'one'}], recipe=recipe, navigate=True, intent=8)
    assert result['playlist_url'] == '/playlist/9'
    enrich.assert_called_once_with(9)
    navigate.assert_called_once_with(client, 'exact', 8)
    assert respond.call_count == 2 and measure.call_count == 1
    recovery = dict(confirmed=[], rejected=['no'], uncertain=['yes'], unattempted=['later'])
    before = deepcopy(recovery)
    reconciled = reconciled_additions(recovery, [{'video_id': v} for v in ['yes', 'no', 'later']], {'yes'})
    assert reconciled['confirmed'] == ['yes'] and reconciled['rejected'] == ['no']
    assert recovery == before
    for effect in (measure, respond, enrich, navigate, refresh):
        effect.reset_mock()
    create.return_value = dict(pid=10, new_ytm='rejected', added=0)
    drafts = Mock()
    result = command.road_trip(drafts, 1, {}, 'Title', [], recipe, 9)
    assert result['status'] == 'rejected'
    result = command.create('Title', [], recipe=recipe, navigate=True, canvas='{"nodes": []}')
    assert result['status'] == 'rejected'
    for effect in (measure, respond, enrich, navigate, refresh, drafts.saved, recipes.set_cluster_canvas):
        effect.assert_not_called()


def test_song_metadata_resource_maps_a_controller_stub(monkeypatch):
    from yt_playlist.web.context import Ctx
    from yt_playlist.web.routes import songs
    query = Mock(spec_set=['metadata'])
    query.metadata.return_value = dict(metadata={'title': 'Detached'}, genre_options=[], genres=['Jazz'])
    monkeypatch.setattr(Ctx, 'playlist_queries', lambda self: query)
    app = FastAPI()
    app.include_router(songs.build(NS(store=None)))
    with TestClient(app) as client:
        response = client.get('/songs/metadata', params={'video_id': 'exact-upload'})
    assert response.status_code == 200 and response.json()['metadata']['title'] == 'Detached'
    query.metadata.assert_called_once_with('exact-upload')


def test_transfer_rejects_a_stale_batch_before_moving_any_playlist():
    playlists = Mock(spec_set=['get_playlists', 'get_playlist'])
    rows = [NS(id=1, account_id=7, title='One', provider_playlist_id='remote-one'),
            NS(id=2, account_id=8, title='Changed owner', provider_playlist_id='remote-two')]
    playlists.get_playlists.return_value = rows
    playlists.get_playlist.side_effect = lambda pid: next(p for p in rows if p.id == pid)
    accounts = Mock(spec_set=['get_accounts'])
    accounts.get_accounts.return_value = [NS(id=7), NS(id=8)]
    move, refresh = Mock(), Mock()
    command = TransferCommands(playlists, accounts, move=move, refresh=refresh, logger=Mock())
    with pytest.raises(ValueError, match='selection has changed'):
        command.bulk('1,2', 7, 8, False)
    move.assert_not_called()
    refresh.assert_not_called()
    move.return_value = dict(added=3, deleted=True)
    assert command.bulk('1,1', 7, 8, False)[0]['state'] == 'moved'
    move.assert_called_once_with(1, 8, copy_only=False)
    refresh.assert_called_once_with('remote-one')


def test_merge_keeps_distinct_uploads_and_reviewed_draft_when_write_is_uncertain():
    playlists = Mock(spec_set=['get_playlist', 'get_playlist_tracks_with_meta'])
    playlists.get_playlist.side_effect = lambda pid: NS(id=pid, account_id=7,
        title=str(pid), provider_playlist_id='remote-' + str(pid))
    playlists.get_playlist_tracks_with_meta.side_effect = lambda pid: [
        ('same-recording', 'upload-' + str(pid), 'Song', 'Artist', 120, True)]
    accounts = Mock(spec_set=['get_accounts'])
    accounts.get_accounts.return_value = [NS(id=7, label='Owner')]
    actions = Mock(spec_set=['get_draft', 'delete_action'])
    actions.get_draft.return_value = NS(id=11, revision=4, params_json=json.dumps(dict(
        excluded=[], pick={}, sort='playlist', mode='interleaved', keep='1')))
    apply = Mock(side_effect=PlaylistAddUncertain('remote-1', ['upload-1'], [], ['upload-2']))
    command = MergeDrafts(playlists, accounts, actions, apply=apply, now=lambda: 20)
    with pytest.raises(PlaylistAddUncertain):
        command.apply([1, 2])
    apply.assert_called_once_with([1, 2], ['upload-1', 'upload-2'], '1', reviewed_draft=(11, 4))
    actions.delete_action.assert_not_called()
