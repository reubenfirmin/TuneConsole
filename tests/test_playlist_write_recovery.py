"""An uncertain write must never be replayed or reported as a rejected occurrence."""
import pytest
from yt_playlist.library import playlist_creation, playlist_generated

from yt_playlist.library.playlist_writes import add_items, add_items_resilient, PlaylistAddUncertain


class Client:
    def __init__(self, videos=(), outcomes=()):
        self.tracks = [{"videoId": v, "setVideoId": f"old-{i}"} for i, v in enumerate(videos)]
        self.outcomes = iter(outcomes)
        self.calls = []
        self.incomplete = False

    def get_playlist(self, playlist_id, limit=None):
        return {"tracks": list(self.tracks),
                "trackCount": len(self.tracks) + int(self.incomplete)}

    def add_playlist_items(self, playlist_id, videos, **kwargs):
        self.calls.append((list(videos), kwargs))
        delivered, result = next(self.outcomes)
        for video in delivered:
            self.tracks.append({"videoId": video, "setVideoId": f"new-{len(self.tracks)}"})
        if isinstance(result, Exception):
            raise result
        return result


def test_lost_response_after_commit_is_confirmed_without_replay():
    client = Client(outcomes=[(["a", "b"], TimeoutError())])
    handles = {}
    assert add_items(client, "PL", ["a", "b"], handles) == (2, [])
    assert len(client.calls) == 1
    assert handles == {"a": "new-0", "b": "new-1"}


def test_partial_unknown_outcome_is_not_skipped_or_replayed():
    client = Client(outcomes=[(["a"], TimeoutError())])
    with pytest.raises(PlaylistAddUncertain) as failure:
        add_items(client, "PL", ["a", "b"])
    assert failure.value.confirmed == ("a",)
    assert failure.value.skipped == ()
    assert failure.value.uncertain == ("b",)
    assert len(client.calls) == 1


def test_rejected_batch_retries_only_missing_items_and_retains_rejections():
    client = Client(outcomes=[(["a"], "STATUS_FAILED"), (["b"], "STATUS_SUCCEEDED"),
                              ([], "STATUS_FAILED")])
    assert add_items(client, "PL", ["a", "b", "c"]) == (2, ["c"])
    assert [call[0] for call in client.calls] == [["a", "b", "c"], ["b"], ["c"]]


def test_duplicate_occurrence_recovery_uses_baseline_counts_and_new_handle():
    client = Client(["a"], outcomes=[(["a"], TimeoutError())])
    handles = {}
    assert add_items(client, "PL", ["a"], handles) == (1, [])
    assert client.calls == [(["a"], {"duplicates": True})]
    assert handles == {"a": "new-1"}


def test_stale_complete_read_does_not_authorize_an_ambiguous_retry():
    client = Client(["a"], outcomes=[([], TimeoutError())])
    with pytest.raises(PlaylistAddUncertain):
        add_items(client, "PL", ["a"])
    assert len(client.calls) == 1


def test_incomplete_readback_blocks_fallback_and_optimistic_success():
    client = Client(outcomes=[([], "STATUS_FAILED")])
    # A known new destination can be written before it is readable, but recovery still needs
    # complete contents. A partial response is never evidence for an individual retry.
    client.incomplete = True
    with pytest.raises(PlaylistAddUncertain):
        add_items(client, "PL", ["a"], prior_tracks=[])
    assert len(client.calls) == 1


def test_uncertain_later_batch_retains_earlier_confirmed_accounting():
    client = Client(outcomes=[(["a"], "STATUS_SUCCEEDED"), ([], TimeoutError())])
    with pytest.raises(PlaylistAddUncertain) as failure:
        add_items_resilient(client, "PL", ["a", "b"], batch_size=1)
    assert failure.value.confirmed == ("a",)
    assert failure.value.uncertain == ("b",)


@pytest.mark.parametrize("operation", ["copy", "move"])
def test_uncertain_new_playlist_keeps_confirmed_membership_and_undo(store, monkeypatch, operation):
    from tests.conftest import FakeClient, _track
    from yt_playlist.library import executor

    source_identity = store.identities.upsert_identity("Source", "cred", None, True)
    target_identity = store.identities.upsert_identity("Target", "cred", None, False)
    tracks = [_track("v1", "One", "Artist"), _track("v2", "Two", "Artist")]
    tids = [store.tracks.upsert_track(t["videoId"], t["title"], "Artist", None, 200)
            for t in tracks]
    source = store.playlists.upsert_playlist(source_identity, "SOURCE", "Original", 2, "", 1)
    store.playlists.set_playlist_tracks(source, tids)
    source_client = FakeClient(tracks={"SOURCE": tracks})
    destination = FakeClient(catalog={t["videoId"]: t for t in tracks})
    original_add = destination.add_playlist_items
    calls = []

    def partial_commit(playlist_id, videos, **kwargs):
        calls.append(list(videos))
        original_add(playlist_id, videos[:1])
        raise TimeoutError("The response was lost after one occurrence was committed")

    monkeypatch.setattr(destination, "add_playlist_items", partial_commit)
    with pytest.raises(PlaylistAddUncertain) as failure:
        if operation == "copy":
            playlist_creation.copy_playlist(store, [source], "Copy", destination, 10)
        else:
            executor.copy_or_move_playlist(store, source, target_identity, source_client,
                                          destination, 10, delete_source=True)
    assert calls == [["v1", "v2"]]
    assert failure.value.confirmed == ("v1",)
    assert failure.value.uncertain == ("v2",)
    assert not source_client.deleted
    assert store.playlists.get_playlist(source) is not None
    created = next((p for p in store.playlists.get_playlists()
                    if p.ytm_playlist_id == failure.value.playlist_id), None)
    assert created is not None, "The partially populated remote playlist must stay visible locally"
    assert store.playlists.get_playlist_track_ids(created.id) == tids[:1]
    assert store.actions.get_actions(), "The new remote playlist must retain an Undo/recovery action"
    import json
    action = store.actions.get_actions()[0]
    undo = json.loads(action.undo_json)
    assert undo["post"]["videos"] == ["v1"]
    assert undo["addition"]["uncertain"] == ["v2"]
    assert json.loads(action.params_json)["added"] == 1


def test_uncertain_batch_distinguishes_later_unattempted_items():
    client = Client(outcomes=[([], TimeoutError())])
    with pytest.raises(PlaylistAddUncertain) as failure:
        add_items_resilient(client, "PL", ["a", "b", "c"], batch_size=1)
    assert failure.value.uncertain == ("a",)
    assert failure.value.unattempted == ("b", "c")
    assert len(client.calls) == 1


def test_partial_merge_does_not_claim_a_concurrent_addition_for_undo(store, monkeypatch):
    import json
    from tests.conftest import FakeClient, _track
    from yt_playlist.library import executor, playlist_undo

    iid = store.identities.upsert_identity('Main', 'cred', None, True)
    keeper = store.playlists.upsert_playlist(iid, 'KEEP', 'Keeper', 1, '', 1)
    source = store.playlists.upsert_playlist(iid, 'SOURCE', 'Source', 2, '', 1)
    tracks = {v: _track(v, v, 'Artist') for v in ('a', 'b', 'c', 'd')}
    tids = {v: store.tracks.upsert_track(v, v, 'Artist', '', 200) for v in tracks}
    store.playlists.set_playlist_tracks(keeper, [tids['a']])
    store.playlists.set_playlist_tracks(source, [tids['b'], tids['d']])
    client = FakeClient(tracks={'KEEP': [tracks['a']], 'SOURCE': [tracks['b'], tracks['d']]}, catalog=tracks)
    add = client.add_playlist_items

    def partial(playlist, videos, **kwargs):
        add(playlist, videos[:1], **kwargs)
        client._tracks[playlist].append(tracks['c'])  # An independent concurrent edit.
        raise TimeoutError('response lost')

    monkeypatch.setattr(client, 'add_playlist_items', partial)
    with pytest.raises(PlaylistAddUncertain) as caught:
        executor.apply_result(store, {iid: client}, [keeper, source], ['a', 'b', 'd'], keeper, 2)
    assert caught.value.confirmed == ('b',) and caught.value.uncertain == ('d',)
    assert caught.value.skipped == () and caught.value.unattempted == ()
    assert not client.deleted and store.playlists.get_playlist(source)
    assert store.playlists.get_playlist_track_ids(keeper) == [tids[v] for v in ('a', 'b', 'c')]
    action = store.actions.get_actions()[0]
    entry = json.loads(action.undo_json)['restored'][0]
    assert entry['prev'] == ['a'] and entry['post_state'] is None and entry['post'] is None
    writes = (list(client.added), list(client.removed), list(client.edited))
    with pytest.raises(ValueError, match='snapshot could not be verified'):
        playlist_undo.undo_action(store, action.id, {iid: client}, 3)
    assert (client.added, client.removed, client.edited) == writes
    assert [t['videoId'] for t in client._tracks['KEEP']] == ['a', 'b', 'c']


@pytest.mark.parametrize("operation", ["generated", "album", "copy_into", "add_tracks", "merge"])
def test_other_mutation_boundaries_retain_partial_progress(store, monkeypatch, operation):
    import json
    from tests.conftest import FakeClient, _track
    from yt_playlist.library import executor

    identity = store.identities.upsert_identity("Main", "cred", None, True)
    tracks = [_track("v1", "One", "Artist"), _track("v2", "Two", "Artist")]
    tids = [store.tracks.upsert_track(t["videoId"], t["title"], "Artist", None, 200) for t in tracks]
    source = store.playlists.upsert_playlist(identity, "SOURCE", "Source", 2, "", 1)
    store.playlists.set_playlist_tracks(source, tids)
    target = store.playlists.upsert_playlist(identity, "TARGET", "Target", 0, "", 1)
    destination = FakeClient(catalog={t["videoId"]: t for t in tracks},
                             albums={"ALBUM": {"title": "Album", "tracks": tracks,
                                               "artists": [{"name": "Artist"}]}})
    original_add = destination.add_playlist_items
    calls = []

    def partial(playlist_id, videos, **kwargs):
        calls.append(list(videos))
        original_add(playlist_id, videos[:1])
        raise TimeoutError()

    monkeypatch.setattr(destination, "add_playlist_items", partial)
    with pytest.raises(PlaylistAddUncertain) as failure:
        if operation == "generated":
            playlist_generated.create_generated_playlist(store, "Generated", [
                {"video_id": t["videoId"], "title": t["title"], "artist": "Artist", "duration": 200}
                for t in tracks], destination, 10, identity_id=identity)
        elif operation == "album":
            playlist_creation.create_playlist_from_album(store, "ALBUM", "Album", destination, 10, identity)
        elif operation == "copy_into":
            playlist_creation.copy_into_playlist(store, [source], target, destination, 10)
        elif operation == "merge":
            executor.apply_result(store, {identity: destination}, [source, target], ["v1", "v2"], target, 10)
        else:
            executor.add_tracks_to_playlist(store, target, [
                {"videoId": t["videoId"], "title": t["title"], "artist": "Artist", "duration": 200}
                for t in tracks], destination, 10)
    assert calls == [["v1", "v2"]]
    retained = next(p for p in store.playlists.get_playlists()
                    if p.ytm_playlist_id == failure.value.playlist_id)
    assert store.playlists.get_playlist_track_ids(retained.id) == tids[:1]
    action = store.actions.get_actions()[0]
    assert json.loads(action.params_json)["added"] == 1
    undo = json.loads(action.undo_json)
    addition = undo["restored"][0]["addition"] if operation == "merge" else undo["addition"]
    assert addition["uncertain"] == ["v2"]
    if operation == "generated":
        assert store.playlists.is_quarantined_playlist(retained.scope_ref)


@pytest.mark.parametrize("operation", ["copy", "generated"])
def test_http_result_exposes_partial_destination_and_recovery(store, monkeypatch, operation):
    import json
    from fastapi.testclient import TestClient
    from tests.conftest import FakeClient, _track
    from yt_playlist.web.app import create_app

    identity = store.identities.upsert_identity("Main", "cred", None, True)
    tracks = [_track("v1", "One", "Artist"), _track("v2", "Two", "Artist")]
    tids = [store.tracks.upsert_track(t["videoId"], t["title"], "Artist", None, 200) for t in tracks]
    source = store.playlists.upsert_playlist(identity, "SOURCE", "Source", 2, "", 1)
    store.playlists.set_playlist_tracks(source, tids)
    destination = FakeClient(catalog={t["videoId"]: t for t in tracks})
    original_add = destination.add_playlist_items

    def partial(playlist_id, videos, **kwargs):
        original_add(playlist_id, videos[:1])
        raise TimeoutError()

    monkeypatch.setattr(destination, "add_playlist_items", partial)
    app = create_app(store, lambda: {identity: destination}, now_fn=lambda: 10)
    client = TestClient(app, base_url="http://127.0.0.1")
    if operation == "generated":
        response = client.post("/home/generate", data={"name": "Generated", "tracks": json.dumps([
            {"video_id": t["videoId"], "title": t["title"], "artist": "Artist", "duration": 200}
            for t in tracks])})
    else:
        response = client.post("/playlists/copy", data={"ids": str(source), "name": "Copy"})
    assert response.status_code == 200
    if operation == "generated":
        assert "Partially saved" in response.text
        assert "Review partial playlist" in response.text
        assert "Action Log and recovery" in response.text
        assert "window.location.assign" not in response.text
        assert "1 addition(s) remain unconfirmed" in client.get("/actions").text
        return
    result = response.json()
    assert result["status"] == "partial"
    assert result["copy"]["added"] == 1 and result["copy"]["uncertain"] == 1
    assert result["copy"]["new_ytm"] == json.loads(store.actions.get_actions()[0].undo_json)["new_ytm"]
    assert "Action Log" in result["message"]
    assert "were kept" in result["guidance"]
    assert "1 addition(s) remain unconfirmed" in client.get("/actions").text
