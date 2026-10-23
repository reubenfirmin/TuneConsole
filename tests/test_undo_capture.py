import logging

import pytest

from yt_playlist.library.undo_capture import capture_post
from yt_playlist.library.undo_conflicts import require_unchanged_copy


class Readback:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.reads = 0

    def get_playlist(self, playlist_id, limit=None):
        self.reads += 1
        result = next(self.responses)
        if isinstance(result, Exception):
            raise result
        return result


def detail(videos):
    return {"tracks": [{"videoId": v, "setVideoId": str(i)} for i, v in enumerate(videos)],
            "trackCount": len(videos), "title": "Copy", "description": "Created"}


@pytest.mark.parametrize("first", [TimeoutError("secret payload"),
                                   {"tracks": [], "trackCount": 2}, detail([]),
                                   {"tracks": [{}]}])
def test_transient_capture_failure_retries_validation_and_expected_contents(first, caplog):
    client = Readback([first, detail(["a", "b"])])
    with caplog.at_level(logging.WARNING):
        state = capture_post(client, "PL", expected_videos=["a", "b"], sleep=lambda _: None)
    assert client.reads == 2
    assert state["videos"] == ["a", "b"]
    assert state["handles"] == ["0", "1"]
    assert "secret payload" not in caplog.text
    assert "operation=" in caplog.text and "attempt=1/3" in caplog.text


def test_persistent_failure_never_adopts_later_state(caplog):
    client = Readback([ValueError("Authorization: secret"), detail([]), detail(["a", "later"])])
    with caplog.at_level(logging.WARNING):
        assert capture_post(client, "PL", expected_videos=["a"], sleep=lambda _: None) is None
    assert client.reads == 3
    assert "Authorization" not in caplog.text
    with pytest.raises(ValueError, match="snapshot could not be verified"):
        require_unchanged_copy(None, detail(["a", "later"]))


def test_later_metadata_is_not_adopted_as_the_created_baseline():
    client = Readback([detail(["a"])] * 3)
    assert capture_post(client, "PL", expected_videos=["a"],
                        expected_metadata={"title": "Original title"}, sleep=lambda _: None) is None


def test_capture_without_operation_evidence_never_reads_a_later_baseline():
    client = Readback([detail(['a', 'external'])])
    assert capture_post(client, 'PL', expected_videos=None, sleep=lambda _: None) is None
    assert client.reads == 0


def test_verified_snapshot_still_protects_later_edits():
    client = Readback([detail(["a"])])
    state = capture_post(client, "PL", expected_videos=["a"], sleep=lambda _: None)
    with pytest.raises(ValueError, match="changed since this action"):
        require_unchanged_copy(state, {**state, "videos": ["a", "later"]})


def test_forward_copy_remains_recorded_when_capture_fails(store, monkeypatch):
    import json
    from tests.conftest import FakeClient, _track
    from yt_playlist.library import executor, undo_capture

    identity = store.identities.upsert_identity("Main", "cred", None, True)
    tid = store.tracks.upsert_track("a", "Song", "Artist", None, 200)
    source = store.playlists.upsert_playlist(identity, "SOURCE", "Source", 1, "", 1)
    store.playlists.set_playlist_tracks(source, [tid])
    client = FakeClient(catalog={"a": _track("a", "Song", "Artist")})
    monkeypatch.setattr(client, "get_playlist", lambda *args, **kwargs: {"tracks": [], "trackCount": 1})
    monkeypatch.setattr(executor, "_capture_post", lambda *args, **kwargs:
                        undo_capture.capture_post(*args, **kwargs, sleep=lambda _: None))
    result = executor.copy_playlist(store, [source], "Copy", client, 2)
    action = store.actions.get_actions()[0]
    assert action.status == "executed"
    assert json.loads(action.undo_json)["post"] is None
    assert json.loads(action.undo_json)["new_ytm"] == result["new_ytm"]
    assert result["added"] == 1


def test_merge_retry_does_not_claim_an_external_addition(store, monkeypatch):
    import json
    from tests.conftest import FakeClient, _track
    from yt_playlist.library import executor, playlist_undo, undo_capture

    iid = store.identities.upsert_identity('Main', 'cred', None, True)
    keeper = store.playlists.upsert_playlist(iid, 'KEEP', 'Keeper', 1, '', 1)
    other = store.playlists.upsert_playlist(iid, 'OTHER', 'Other', 1, '', 1)
    client = FakeClient(tracks={'KEEP': [_track('a', 'A', 'Artist')],
                                'OTHER': [_track('b', 'B', 'Artist')]},
                        catalog={'b': _track('b', 'B', 'Artist')})
    get = client.get_playlist
    post_reads = []

    def read(playlist, **kwargs):
        if playlist == 'KEEP' and client.added:
            post_reads.append(playlist)
            if len(post_reads) == 1:
                return {'tracks': [], 'trackCount': 2}
            if len(post_reads) == 2:
                client._tracks['KEEP'].append(_track('c', 'External', 'Artist'))
        return get(playlist, **kwargs)

    monkeypatch.setattr(client, 'get_playlist', read)
    monkeypatch.setattr(executor, '_capture_post', lambda *a, **kw:
                        undo_capture.capture_post(*a, **kw, sleep=lambda _: None))
    executor.apply_result(store, {iid: client}, [keeper, other], ['a', 'b'], 'all', 2)
    action = store.actions.get_actions()[0]
    entry = next(e for e in json.loads(action.undo_json)['restored'] if e['ytm'] == 'KEEP')
    assert len(post_reads) == 3 and entry['post_state'] is None and entry['post'] is None
    writes = (list(client.added), list(client.removed), list(client.edited))
    with pytest.raises(ValueError, match='snapshot could not be verified'):
        playlist_undo.undo_action(store, action.id, {iid: client}, 3)
    assert [t['videoId'] for t in client._tracks['KEEP']] == ['a', 'b', 'c']
    assert (client.added, client.removed, client.edited) == writes
