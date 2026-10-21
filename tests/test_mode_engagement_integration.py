"""#60: rendered offers -> saved mode playlists -> persisted listening -> real scoreboard."""
import json
from html.parser import HTMLParser
from types import SimpleNamespace

import numpy as np
import pytest
from fastapi.testclient import TestClient

from tests.conftest import FakeClient
from yt_playlist.library import live_plays, player_events
from yt_playlist.library.listen_derive import classify_exit
from yt_playlist.rec import mode_eval
from yt_playlist.web.app import create_app


class _Cards(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.cards, self.current = [], None
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "section" and "gen-card" in attrs.get("class", "").split():
            self.current = {"tracks": []}
        if self.current is None:
            return
        if "data-recipe" in attrs:
            self.current["recipe"] = json.loads(attrs["data-recipe"])
            self.current["name"] = attrs["data-name"]
        if tag == "li" and "gen-row" in attrs.get("class", "").split():
            self.current["tracks"].append({"video_id": attrs["data-vid"], "title": attrs["data-title"],
                                            "artist": attrs["data-artist"], "duration": 200})

    def handle_endtag(self, tag):
        if tag == "section" and self.current is not None:
            self.cards.append(self.current)
            self.current = None


@pytest.fixture
def engagement(store):
    iid = store.identities.upsert_identity("main", "cred", None, True)
    store.modes.replace_modes([
        {"mode_id": mid, "label": genre, "families": [[genre, 1]],
         "centroid": np.eye(2, dtype=np.float32)[mid - 1], "size": 40, "rep_keys": []}
        for mid, genre in ((1, "house"), (2, "techno"))
    ], retired_ids=[], now=1)
    payload = {"_meta": {"comfort_pool": 0, "year_cuts": None, "full_pools": True}}
    for mid, lane, genre in ((1, "wheelhouse", "house"), (2, "explore", "techno")):
        payload[str(mid)] = {lane: [
            {"key": f"song {mid} {i}|artist {mid} {i}", "video_id": f"v{mid}_{i}",
             "title": f"Song {mid} {i}", "artist": f"Artist {mid} {i}", "album": "",
             "thumbnail": None, "plays": 0, "reason": "", "lane": lane, "genre": genre}
            for i in range(20)]}
    store.rec_surface.put_proposals("mode_bundles", payload, 1)
    clock = [1_700_000_000.0]
    fake = FakeClient()
    client = TestClient(create_app(store, lambda: {iid: fake}, now_fn=lambda: clock[0]),
                        base_url="http://127.0.0.1")
    response = client.get("/home/cards")
    assert response.status_code == 200
    cards = {card["recipe"]["mode_id"]: card for card in _Cards(response.text).cards
             if card.get("recipe", {}).get("model") == "mode"}
    assert set(cards) == {1, 2}
    assert store.modes.impression_counts() == {1: 1, 2: 1}
    selected = {}
    for mid in (1, 2):
        card = cards[mid]
        response = client.post("/home/generate", data={
            "name": card["name"], "tracks": json.dumps(card["tracks"]),
            "recipe": json.dumps(card["recipe"])})
        assert response.status_code == 200
        pid = next(pid for pid, mode in store.modes.pick_rows() if mode == mid)
        selected[mid] = next(playlist for playlist in store.playlists.get_playlists() if playlist.id == pid)
    # These are the production ingestion functions used by /bridge/ws; no listen-stat mock.
    ctx = SimpleNamespace(store=store)
    first, second = cards[1]["tracks"][:2]
    for offset, track, position in ((120, first, 190), (480, second, 20), (86400 + 120, first, 190)):
        at = clock[0] + offset
        assert live_plays.handle_play_event(ctx, {
            "type": "play", "videoId": track["video_id"], "title": track["title"],
            "artist": track["artist"], "playlist": selected[1].ytm_playlist_id}, at)
        assert player_events.handle_player_event(ctx, {
            "type": "pevent", "kind": "track_exit", "videoId": track["video_id"],
            "position": position, "duration": 200, "playlist": selected[1].ytm_playlist_id}, at + position)
    yield SimpleNamespace(store=store, cards=cards, selected=selected, ctx=ctx, start=clock[0])
    client.close()


def test_saved_mode_retains_attributed_plays_and_exit_depth_over_multiple_days(engagement):
    data = engagement
    events = data.store.history.play_events_since(data.start)
    assert len(events) == 3
    assert {event["playlist_ytm_id"] for event in events} == {data.selected[1].ytm_playlist_id}
    assert events[-1]["played_at"] - events[0]["played_at"] == 86400
    exits = data.store.player_events.player_events_since(data.start, kind="track_exit")
    assert [classify_exit(event["position"], event["duration"]) for event in exits] == ["completion", "skip", "completion"]
    assert {event["playlist_ytm_id"] for event in exits} == {data.selected[1].ytm_playlist_id}
    board = {row["mode_id"]: row for row in mode_eval.mode_scoreboard(data.store)}
    assert (board[1]["offered"], board[1]["picked"], board[1]["plays"]) == (1, 1, 3)
    assert (board[2]["offered"], board[2]["picked"], board[2]["plays"]) == (1, 1, 0)
    assert (board[1]["completions"], board[1]["skips"]) == (2, 1)
    assert (board[2]["completions"], board[2]["skips"]) == (0, 0)
    assert board[1]["last_play"] == events[-1]["played_at"]


def test_saved_mode_retains_the_link_needed_to_read_skip_and_completion_depth(engagement):
    store = engagement.store
    picks = dict(store.modes.pick_rows())
    playlist_ids = {playlist.ytm_playlist_id: playlist.id for playlist in store.playlists.get_playlists()}
    exits = store.player_events.player_events_since(engagement.start, kind="track_exit")
    # Part C requires a persistent trace of mode choice and engagement. Use the stored pick,
    # playlist provenance and production exit classifier, without inventing a scoreboard API.
    depth = [(picks[playlist_ids[event["playlist_ytm_id"]]],
              classify_exit(event["position"], event["duration"])) for event in exits]
    assert depth == [(1, "completion"), (1, "skip"), (1, "completion")]
    assert exits[-1]["at"] - exits[0]["at"] == 86400


def test_organic_play_does_not_count_as_engagement_with_a_saved_mode(engagement):
    data = engagement
    track = data.cards[2]["tracks"][0]
    assert live_plays.handle_play_event(data.ctx, {
        "type": "play", "videoId": track["video_id"], "title": track["title"],
        "artist": track["artist"]}, data.start + 2 * 86400)
    assert data.store.history.play_events_since(data.start)[-1]["playlist_ytm_id"] is None
    board = {row["mode_id"]: row for row in mode_eval.mode_scoreboard(data.store)}
    assert board[2]["plays"] == 0, "Playing the same song organically is not a mode-playlist selection"
