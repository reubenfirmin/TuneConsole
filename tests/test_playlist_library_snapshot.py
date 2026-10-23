"""Playlist-library reads distinguish verified emptiness from unavailable evidence.

The terminal continuation fixture includes the itemSize display metadata observed
for the failing live account, using synthetic tracking data.
"""
from types import SimpleNamespace

import pytest

from tests.test_saved_album_snapshot import _first_page, _page, _production_client, _renderer
from tests.test_sync_outcomes import Client
from yt_playlist.core.album_snapshot import (
    LibrarySnapshotError, complete_library_playlists, response_shape,
)
from yt_playlist.library import sync


@pytest.mark.parametrize("create", [None, {"createPlaylistRenderer": {}}, {
    "musicTwoRowItemRenderer": {"navigationEndpoint": {"createPlaylistEndpoint": {}}},
}])
def test_optional_create_tile_does_not_drop_the_first_real_playlist(create):
    page = _page("VLFIRST", "VLSECOND")
    if create is not None:
        page["items"].insert(0, create)
    client, bridge = _production_client([_first_page(page)])
    assert [p["playlistId"] for p in client.get_library_playlists(limit=None)] == ["FIRST", "SECOND"]
    assert len(bridge.calls) == 1


@pytest.mark.parametrize("create_only", [False, True])
def test_empty_grid_is_explicitly_verified(create_only):
    page = _page()
    if create_only:
        page["items"].append({"createPlaylistRenderer": {}})
    client, _ = _production_client([_first_page(page)])
    result = client.get_library_playlists(limit=None)
    assert result == [] and result.complete


@pytest.mark.parametrize("ids", [(), ("VLONE", "VLTWO")])
@pytest.mark.parametrize("metadata", [{}, {"itemSize": "COLLECTION_STYLE_ITEM_SIZE_MEDIUM"}])
def test_metadata_only_terminal_continuation_preserves_every_preceding_playlist(ids, metadata):
    client, bridge = _production_client([
        _first_page(_page(*ids, continuation="next")),
        {"continuationContents": {"gridContinuation": {"trackingParams": "PRIVATE_TRACKING", **metadata}}},
    ])
    result = client.get_library_playlists(limit=None)
    assert [p["playlistId"] for p in result] == [bid[2:] for bid in ids]
    assert result.complete and len(bridge.calls) == 2


@pytest.mark.parametrize("grid", [
    {}, {"trackingParams": None}, {"trackingParams": {}}, {"trackingParams": ""},
    {"trackingParams": "tracking", "items": None},
    {"trackingParams": "tracking", "items": "invalid"},
    {"trackingParams": "tracking", "continuations": None},
    {"trackingParams": "tracking", "continuations": [
        {"nextContinuationData": {"continuation": "more"}},
    ]},
    {"trackingParams": "tracking", "contents": []},
    {"trackingParams": "tracking", "unknown": {}},
    *({"trackingParams": "tracking", "itemSize": size} for size in (None, "", 1, [], {})),
    {"trackingParams": "tracking", "itemSize": "size", "unknown": {}},
    {"trackingParams": "tracking", "itemSize": "size", "items": None},
    {"trackingParams": "tracking", "itemSize": "size", "continuations": []},
])
def test_incomplete_continuation_cannot_masquerade_as_tracking_only_terminal(grid):
    client, _ = _production_client([
        _first_page(_page("VLONE", continuation="next")),
        {"continuationContents": {"gridContinuation": grid}},
    ])
    with pytest.raises(LibrarySnapshotError, match="has no complete grid; page=2"):
        client.get_library_playlists(limit=None)


@pytest.mark.parametrize("metadata", [{}, {"itemSize": "COLLECTION_STYLE_ITEM_SIZE_MEDIUM"}])
def test_metadata_only_initial_grid_cannot_certify_empty_membership(metadata):
    client, _ = _production_client([_first_page({"trackingParams": "tracking", **metadata})])
    with pytest.raises(LibrarySnapshotError, match="has no complete grid; page=1"):
        client.get_library_playlists(limit=None)


@pytest.mark.parametrize("error", [
    {"responseContext": {"mainAppWebResponseContext": {"loggedOut": True}}},
    {"error": {"code": 500}},
])
def test_terminal_tracking_metadata_cannot_hide_a_response_error(error):
    client, _ = _production_client([
        _first_page(_page("VLONE", continuation="next")),
        {"continuationContents": {"gridContinuation": {"trackingParams": "tracking"}}, **error},
    ])
    with pytest.raises(LibrarySnapshotError):
        client.get_library_playlists(limit=None)


@pytest.mark.parametrize("response,reason", [
    ({}, "unsupported library grid layout"),
    (_first_page({}), "has no complete grid"),
    (_first_page({"items": [None]}), "unsupported playlist grid item"),
    (_first_page({"items": [{"musicTwoRowItemRenderer": {}}]}), "malformed playlist grid item"),
    ({"responseContext": {"mainAppWebResponseContext": {"loggedOut": True}}}, "signed out"),
    ({"contents": {"messageRenderer": {"button": {"signInEndpoint": {}}}}}, "signed out"),
    ({"error": {"code": 401, "message": "PRIVATE_ERROR"}}, "signed out"),
    ({"error": {"code": 500, "message": "PRIVATE_ERROR"}}, "unavailable"),
])
def test_unavailable_auth_and_unsupported_layout_have_distinct_safe_reasons(response, reason):
    client = SimpleNamespace(_check_auth=lambda: None, _send_request=lambda *a: response)
    with pytest.raises(LibrarySnapshotError) as caught:
        complete_library_playlists(client)
    assert caught.value.reason == reason
    assert str(caught.value).startswith("playlist-library snapshot ")
    assert "PRIVATE_ERROR" not in str(caught.value)
    assert sync._is_auth_error(caught.value) == (reason == "signed out")


@pytest.mark.parametrize("continuations", [None, {}, "token", [{}], [
    {"nextContinuationData": {"continuation": ""}},
], [{"nextContinuationData": {"continuation": 42}}]])
def test_malformed_pagination_cannot_certify_an_empty_library(continuations):
    client, bridge = _production_client([_first_page({"items": [], "continuations": continuations})])
    with pytest.raises(LibrarySnapshotError, match="playlist-library snapshot malformed grid continuation"):
        client.get_library_playlists(limit=None)
    assert len(bridge.calls) == 1


def test_continuation_failure_reports_the_correct_page_without_exposing_payload():
    first = _page("VLONE", continuation="PRIVATE_TOKEN")
    bad = {"musicTwoRowItemRenderer": {"title": {"runs": [{"text": "PRIVATE_TITLE"}]}}}
    client, _ = _production_client([_first_page(first), {
        "continuationContents": {"gridContinuation": {"items": [bad]}},
    }])
    with pytest.raises(LibrarySnapshotError) as caught:
        client.get_library_playlists(limit=None)
    message = str(caught.value)
    assert "page=2" in message and "musicTwoRowItemRenderer" in message
    assert "PRIVATE_" not in message


def test_structural_diagnostic_never_echoes_unknown_keys_or_values():
    shape = response_shape(_first_page({"items": [_renderer("PRIVATE_ID")],
        "continuations": [{"nextContinuationData": {"continuation": "PRIVATE_TOKEN"}}],
        "PRIVATE_KEY": "PRIVATE_VALUE"}))
    assert "gridRenderer" in shape and "items[1]" in shape and "continuations[1]" in shape
    assert "PRIVATE_" not in shape


@pytest.mark.parametrize("grid,items_kind", [
    ({"trackingParams": "PRIVATE_TOKEN"}, "missing"),
    ({"items": None}, "null"),
    ({"items": "PRIVATE_ITEMS"}, "string"),
    ({"items": []}, "list"),
])
def test_continuation_diagnostic_distinguishes_missing_and_malformed_items_without_values(grid, items_kind):
    shape = response_shape({"continuationContents": {"gridContinuation": grid}})
    assert f"gridContinuation.items={items_kind}" in shape
    assert "gridContinuation.continuations=missing" in shape
    assert "PRIVATE_" not in shape


def test_terminal_diagnostic_distinguishes_empty_tracking_and_hidden_fields():
    shape = response_shape({"continuationContents": {"gridContinuation": {
        "trackingParams": "", "itemSize": "PRIVATE_SIZE", "PRIVATE_KEY": "PRIVATE_VALUE",
    }}})
    assert "gridContinuation.trackingParams=empty-string" in shape
    assert "gridContinuation.other_fields[1]" in shape
    assert "gridContinuation.itemSize=string" in shape
    assert "PRIVATE_" not in shape


@pytest.mark.parametrize("verified_empty", [False, True, "terminal-continuation"])
def test_only_verified_empty_library_can_remove_cached_playlists(store, monkeypatch, verified_empty):
    monkeypatch.setattr(sync, "with_retry", lambda fn, **kw: fn())
    iid = store.identities.upsert_identity("Main", "bridge", None, True)
    sync.sync_all(store, {iid: Client()}, 100)
    before = store.playlists.get_playlists()
    if verified_empty == "terminal-continuation":
        responses = [_first_page(_page(continuation="next")), {
            "continuationContents": {"gridContinuation": {
                "trackingParams": "tracking", "itemSize": "COLLECTION_STYLE_ITEM_SIZE_MEDIUM",
            }},
        }]
    else:
        responses = [_first_page(_page()) if verified_empty else _first_page({})]
    client, _ = _production_client(responses)
    monkeypatch.setattr(client, "get_history", lambda: [])
    expired = []
    result = sync.sync_identity(store, iid, client, 200,
                                on_auth_expired=lambda *args: expired.append(args))
    assert not expired
    if verified_empty:
        assert not store.playlists.get_playlists()
        assert result["resources"]["playlist_listing"]["status"] == "complete"
    else:
        assert store.playlists.get_playlists() == before
        assert result["resources"]["playlist_listing"]["status"] == "failed"
