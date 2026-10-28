"""Only complete remote reads can replace ordered playlist membership."""
import pytest
from copy import deepcopy
import json

from tests.conftest import FakeClient, _track
from yt_playlist.library import executor, playlist_positions, ratings, sync
from yt_playlist.library.playlist_metadata import GENERATED_DESCRIPTION, content_hash
from yt_playlist.library.sync_outcomes import sync_due


class SnapshotClient(FakeClient):
    response = None

    def get_playlist(self, playlistId, limit=100):
        return self.response if self.response is not None else super().get_playlist(playlistId, limit)


@pytest.fixture
def cached(store, monkeypatch):
    monkeypatch.setattr(sync, "with_retry", lambda fn, **kwargs: fn())
    iid = store.identities.upsert_identity("Main", "bridge", None, True)
    tracks = [{**_track(video, video.upper(), "Artist", set_video_id="set-" + video),
               "likeStatus": "INDIFFERENT"} for video in ("b", "a", "c")]
    client = SnapshotClient(playlists=[{"playlistId": "PL", "title": "Mix"}], tracks={"PL": tracks})
    assert sync.sync_all(store, {iid: client}, 100)["status"] == "complete"
    playlist, = store.playlists.get_playlists()
    return iid, client, playlist, store.playlists.get_playlist_track_ids(playlist.id)


@pytest.mark.parametrize("partial", [
    {"continuation": "next"},
    {"continuations": [{"nextContinuationData": {"continuation": "next"}}]},
    {"trackCount": 3},
    {"trackCount": "3"},
    {"trackCount": True},
    {"tracks": [], "continuation": "next"},
    {"tracks": [], "trackCount": 3},
])
def test_partial_reads_preserve_complete_cache_and_success_timestamp(store, cached, partial):
    iid, client, before, ordered = cached
    client.response = {"tracks": [client._tracks["PL"][1]], **partial}
    events = []
    result = sync.sync_all(store, {iid: client}, 200, on_progress=events.append)
    assert store.playlists.get_playlist(before.id) == before
    assert store.playlists.get_playlist_track_ids(before.id) == ordered
    assert [t["video_id"] for t in store.charts.playlist_tracks_detail(before.id)] == ["b", "a", "c"]
    account = result["accounts"][str(iid)]
    resource = account["resources"]["playlists"]
    assert result["status"] == account["status"] == resource["status"] == "partial"
    assert resource["refreshed"] == resource["skipped"] == 0
    assert resource["incomplete"] == 1 and "1 incomplete" in resource["message"]
    assert resource["last_success_at"] == 100 and resource["last_attempt_at"] == 200
    assert store.settings.get_setting("last_sync_at") == "100"
    assert not sync_due(store, 201, 86400)
    assert sync_due(store, 500, 86400)
    assert events[-1]["outcome"] == "partial"


@pytest.mark.parametrize("unavailable", [
    None, {}, {"tracks": None}, {"tracks": [], "error": "unavailable"},
    {"tracks": [], "status": "STATUS_FAILED"}, TimeoutError("unavailable"),
])
def test_unavailable_reads_preserve_cached_snapshot(store, cached, monkeypatch, unavailable):
    iid, client, before, ordered = cached

    def read(*args, **kwargs):
        if isinstance(unavailable, Exception):
            raise unavailable
        return unavailable

    monkeypatch.setattr(client, "get_playlist", read)
    result = sync.sync_identity(store, iid, client, 200)
    assert result["resources"]["playlists"]["status"] == "failed"
    assert store.playlists.get_playlist(before.id) == before
    assert store.playlists.get_playlist_track_ids(before.id) == ordered


@pytest.mark.parametrize("empty", [False, True])
def test_complete_read_after_partial_replaces_order_and_allows_real_removals(store, cached, empty):
    iid, client, before, ordered = cached
    client.response = {"tracks": [], "continuation": "next"}
    sync.sync_all(store, {iid: client}, 200)
    assert store.playlists.get_playlist_track_ids(before.id) == ordered
    tracks = [] if empty else [client._tracks["PL"][2], client._tracks["PL"][0]]
    client.response = {"tracks": tracks, "trackCount": len(tracks), "continuations": []}
    assert sync.sync_all(store, {iid: client}, 300)["status"] == "complete"
    after = store.playlists.get_playlist(before.id)
    expected = [] if empty else [ordered[2], ordered[0]]
    assert store.playlists.get_playlist_track_ids(before.id) == expected
    assert after.track_count == len(expected)
    assert after.content_hash == content_hash([] if empty else ["c|artist", "b|artist"])
    assert after.last_changed == after.last_seen == 300
    assert store.settings.get_setting("last_sync_at") == "300"


@pytest.mark.parametrize("generated", [False, True])
@pytest.mark.parametrize("empty", [False, True])
def test_new_partial_playlist_retains_observations_without_inventing_membership(store, generated, empty):
    iid = store.identities.upsert_identity("Main", "bridge", None, True)
    track = {**_track("new", "New song", "Artist"), "likeStatus": "LIKE"}
    client = SnapshotClient(playlists=[{"playlistId": "NEW", "title": "New mix"}], tracks={"NEW": [track]})
    client.response = {"tracks": [] if empty else [track], "continuation": "next",
                       "description": GENERATED_DESCRIPTION if generated else ""}
    result = sync.sync_all(store, {iid: client}, 100)
    assert result["status"] == "partial"
    assert result["accounts"][str(iid)]["resources"]["playlists"]["incomplete"] == 1
    assert store.playlists.get_playlists() == []
    if not empty:
        assert store.tracks.identity_key_for_video("new") == "new song|artist"
        assert store.tracks.item_rating(iid, "new")["status"] == "LIKE"
    assert store.conn.execute("SELECT COUNT(*) FROM library_memberships").fetchone()[0] == 0
    if generated:
        assert store.playlists.is_quarantined_playlist("NEW")
    client.response = {"tracks": [track], "trackCount": 1,
                       "description": GENERATED_DESCRIPTION if generated else ""}
    assert sync.sync_all(store, {iid: client}, 200)["status"] == "complete"
    playlist, = store.playlists.get_playlists()
    assert playlist.track_count == 1
    assert store.playlists.get_playlist_track_keys(playlist.id) == {"new song|artist"}
    assert store.playlists.is_quarantined_playlist("NEW") == generated


def test_partial_playlist_and_unavailable_neighbor_report_partial_progress(store, cached, monkeypatch):
    iid, client, before, ordered = cached
    client._playlists.append({"playlistId": "UNAVAILABLE", "title": "Unread neighbor"})

    def read(playlistId, limit=None):
        if playlistId == "UNAVAILABLE":
            raise TimeoutError("unavailable")
        return {"tracks": [client._tracks["PL"][1]], "continuation": "next"}

    monkeypatch.setattr(client, "get_playlist", read)
    result = sync.sync_identity(store, iid, client, 200)
    resource = result["resources"]["playlists"]
    assert resource["status"] == "partial" and resource["refreshed"] == 0
    assert resource["skipped"] == resource["incomplete"] == 1
    assert store.playlists.get_playlist(before.id) == before
    assert store.playlists.get_playlist_track_ids(before.id) == ordered
    assert store.playlists.get_playlists() == [before]


def test_partial_neutrals_cannot_erase_ratings_or_independent_history(store, cached):
    iid, client, before, ordered = cached
    other = store.identities.upsert_identity("Other", "bridge", "other", False)
    ratings.record_confirmed(store, iid, "a", "LIKE", 110)
    ratings.record_confirmed(store, other, "a", "DISLIKE", 111)
    store.history.record_history_plays(other, 100, ["a|artist"], source_timezone="UTC")
    history = store.history.recent_play_counts(100)
    assert history == {"a|artist": 1}
    client.response = {"tracks": [client._tracks["PL"][1],
                                  {**_track("new", "New song", "Artist"), "likeStatus": "LIKE"}],
                       "continuation": "next"}
    result = sync.sync_identity(store, iid, client, 200)
    assert result["resources"]["ratings"]["status"] == "partial"
    assert store.tracks.item_rating(iid, "a")["status"] == "LIKE"
    assert store.tracks.item_rating(other, "a")["status"] == "DISLIKE"
    assert store.tracks.item_rating(iid, "new")["status"] == "LIKE"
    assert store.history.recent_play_counts(100) == history
    assert store.playlists.get_playlist(before.id) == before
    assert store.playlists.get_playlist_track_ids(before.id) == ordered


@pytest.mark.parametrize("read", [{"tracks": [], "continuation": "next"},
                                 {"tracks": [], "trackCount": 3}, {}, None])
def test_targeted_refresh_rejects_non_authoritative_membership(store, cached, monkeypatch, read):
    iid, client, before, ordered = cached
    monkeypatch.setattr(client, "get_playlist", lambda *args, **kwargs: read)
    with pytest.raises(ValueError, match="complete playlist"):
        sync.refresh_playlist(store, iid, client, "PL", "Mix", 200)
    assert store.playlists.get_playlist(before.id) == before
    assert store.playlists.get_playlist_track_ids(before.id) == ordered


@pytest.mark.parametrize('targeted', [False, True])
@pytest.mark.parametrize('edit', ['append', 'remove', 'reorder', 'rename', 'radio', 'delete',
                                'recreate', 'cancel_placement'])
def test_complete_read_cannot_replace_a_newer_confirmed_edit(store, cached, monkeypatch, targeted, edit):
    iid, client, before, ordered = cached
    pending = None
    if edit == 'cancel_placement':
        pending = store.actions.record_action('add_tracks', '{}', json.dumps({
            'playlist_id': before.id, 'after_video_id': 'b', 'video_ids': ['c']}), 'pending', '{}', 150)
    read = client.get_playlist
    changed = {}

    def delayed(*args, **kwargs):
        # A complete response is already captured when the newer operation confirms.
        response = deepcopy(read(*args, **kwargs))
        monkeypatch.setattr(client, 'get_playlist', read)
        if edit == 'append':
            executor.add_tracks_to_playlist(store, before.id, [
                {'videoId': 'd', 'title': 'D', 'artist': 'Artist', 'duration': 200}], client, 210)
        elif edit == 'remove':
            executor.remove_track(store, before.id, 'a', client, 210)
        elif edit == 'reorder':
            executor.reorder_track(store, before.id, 'c', 'b', client, 210)
        elif edit == 'rename':
            executor.rename_playlist(store, before.id, 'Renamed', client, 210)
        elif edit == 'radio':
            store.playlists.publish_confirmed_tracks(before.id, 'Radio', [
                {'video_id': v, 'title': v.upper(), 'artist': 'Artist'} for v in ('b', 'a', 'c', 'd')],
                210, expected_prefix=['b', 'a', 'c'])
        elif edit in ('delete', 'recreate'):
            store.playlists.remove_playlist(before.id)
            if edit == 'recreate':
                pid = store.playlists.upsert_playlist(iid, 'PL', 'Recreated', 1, 'new', 210)
                store.playlists.set_playlist_tracks(pid, [ordered[2]])
        else:
            playlist_positions.cancel_insertions(store, before.id, 210)
        changed['playlists'] = store.playlists.get_playlists()
        changed['tracks'] = {p.id: store.playlists.get_playlist_track_ids(p.id) for p in changed['playlists']}
        # This catalog observation is safe, but stale membership must not adopt it.
        response['tracks'].append(_track('observed', 'Observed only', 'Artist'))
        response['trackCount'] = len(response['tracks'])
        return response

    monkeypatch.setattr(client, 'get_playlist', delayed)
    if targeted:
        with pytest.raises(ValueError, match='newer edits kept'):
            sync.refresh_playlist(store, iid, client, 'PL', 'Mix', 200)
    else:
        result = sync.sync_all(store, {iid: client}, 200)
        resource = result['accounts'][str(iid)]['resources']['playlists']
        assert resource['status'] == 'partial' and resource['refreshed'] == 0
        assert resource['items']['PL']['last_success_at'] == 100
        assert resource['items']['PL']['retry_at'] > 200
        assert store.settings.get_setting('last_sync_at') == '100'
    assert store.playlists.get_playlists() == changed['playlists']
    assert {p.id: store.playlists.get_playlist_track_ids(p.id) for p in changed['playlists']} == changed['tracks']
    assert store.tracks.identity_key_for_video('observed') == 'observed only|artist'
    assert not store.conn.execute('SELECT 1 FROM library_memberships l JOIN catalog_entries e ON e.id=l.entry_id '
                                  "WHERE e.video_id='observed'").fetchone()
    if pending is not None:
        assert store.actions.get_action(pending).status == 'cancelled'
        assert client.edited == []


def test_listing_metadata_is_owned_before_the_library_fetch(store, cached, monkeypatch):
    iid, client, before, ordered = cached
    listing = client.get_library_playlists

    def delayed(*args, **kwargs):
        result = deepcopy(listing(*args, **kwargs))
        store.playlists.set_playlist_title(before.id, 'Newer title', 210)
        return result

    monkeypatch.setattr(client, 'get_library_playlists', delayed)
    result = sync.sync_all(store, {iid: client}, 200)
    assert result['status'] == 'partial'
    assert store.playlists.get_playlist(before.id).title == 'Newer title'
    assert store.playlists.get_playlist_track_ids(before.id) == ordered


def test_listing_absence_cannot_prune_a_newer_edit(store, cached, monkeypatch):
    iid, client, before, ordered = cached

    def delayed(*args, **kwargs):
        store.playlists.set_playlist_title(before.id, 'Still mine', 210)
        return [{'playlistId': 'OTHER', 'title': 'Other'}]

    monkeypatch.setattr(client, 'get_library_playlists', delayed)
    result = sync.sync_all(store, {iid: client}, 200)
    assert result['status'] == 'partial'
    assert store.playlists.get_playlist(before.id).title == 'Still mine'
    assert store.playlists.get_playlist_track_ids(before.id) == ordered


@pytest.mark.parametrize('reappears', [False, True])
def test_listing_conflict_retries_fresh_evidence_before_resolving_absence(store, cached, monkeypatch, reappears):
    iid, client, before, ordered = cached
    read_listing = client.get_library_playlists
    reads = []

    def listing(*args, **kwargs):
        reads.append(True)
        if len(reads) == 1:
            store.playlists.set_playlist_title(before.id, 'Newer title', 210)
            return [{'playlistId': 'OTHER', 'title': 'Other'}]
        return read_listing(*args, **kwargs) if reappears else [{'playlistId': 'OTHER', 'title': 'Other'}]

    monkeypatch.setattr(client, 'get_library_playlists', listing)
    first = sync.sync_all(store, {iid: client}, 200)
    resources = first['accounts'][str(iid)]['resources']
    assert resources['playlist_listing']['status'] == 'partial'
    assert resources['playlist_listing']['retry_at'] == 500
    assert resources['playlist_listing']['last_success_at'] == 100
    assert resources['playlists']['items']['PL']['status'] == 'partial'
    assert store.playlists.get_playlist(before.id).title == 'Newer title'
    assert store.playlists.get_playlist_track_ids(before.id) == ordered
    assert not sync_due(store, 499, 86400)
    assert sync_due(store, 500, 86400)

    retained = sync.sync_all(store, {iid: client}, 499, automatic=True)
    assert len(reads) == 1
    assert retained['accounts'][str(iid)]['resources']['playlists']['items']['PL']['status'] == 'partial'
    assert store.playlists.get_playlist_track_ids(before.id) == ordered
    settled = sync.sync_all(store, {iid: client}, 500, automatic=True)
    assert len(reads) == 2
    resources = settled['accounts'][str(iid)]['resources']
    assert resources['playlist_listing']['status'] == 'complete'
    assert resources['playlist_listing']['last_success_at'] == 500
    assert resources['playlists']['status'] == 'complete'
    if reappears:
        assert resources['playlists']['items']['PL']['last_success_at'] == 500
        assert store.playlists.get_playlist_track_ids(before.id) == ordered
        assert store.playlists.get_playlist(before.id).last_seen == 500
    else:
        assert 'PL' not in resources['playlists']['items']
        assert store.playlists.get_playlist(before.id) is None
