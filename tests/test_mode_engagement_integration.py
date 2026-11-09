"""Rendered offers, saved mode choices and account-scoped listening retain their provenance."""
import json
from html.parser import HTMLParser
from types import SimpleNamespace

import numpy as np
import pytest
from fastapi.testclient import TestClient

from tests.conftest import FakeClient
from tests.home_fixture_support import home_page_bytecode, home_routes_only, without_metadata_providers
from tests.playlist_fixture_support import remote_catalog
from tests.recommendation_support import seed_publication_subjects
from tests.store_fixture_support import bounded_consumer_schema, file_consumer_store as store
from yt_playlist.library import live_plays, player_events, playlist_generated
from yt_playlist.library.listen_derive import classify_exit
from yt_playlist.rec import home_cache, home_cards, home_prepared, mode_eval
from yt_playlist.web.app import create_app

pytestmark = pytest.mark.usefixtures('unlocked_home_controls', 'home_routes_only',
                                    'without_metadata_providers')


@pytest.fixture(scope='module')
def _pristine_current_schema():
    with bounded_consumer_schema((('house', 'House'), ('techno', 'Techno'))) as schema:
        yield schema


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
def engagement(store, monkeypatch, home_page_bytecode):
    iid = store.accounts.upsert_account("main", "cred", None, True)
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
    clock = [1_700_000_000.0]
    # Provenance begins with completed offers. Keep both real mode populations,
    # then publish their selected cards without running unrelated ranking.
    seed_publication_subjects(store, [track for mid in (1, 2)
        for tracks in payload[str(mid)].values() for track in tracks])
    completed_tracks = {row['video_id']: dict(row) for row in store.conn.execute(
        'SELECT id,video_id,identity_key,title,artist FROM tracks')}

    def existing_track(video_id, title, artist, *args, **kwargs):
        track = completed_tracks[video_id]
        assert (title, artist) == (track['title'], track['artist'])
        return track['id']

    def observed_track(video_id, title, artist, **kwargs):
        existing_track(video_id, title, artist, **kwargs)
        return completed_tracks[video_id]['identity_key']

    # Provider copying and metadata ingestion are completed lower inputs. The
    # HTTP handler still associates each returned real playlist with its mode.
    def materialize(owner, title, tracks, client, now, account_id, *, recipe):
        remote_id = f'saved-mode-{recipe["mode_id"]}'
        pid = owner.playlists.upsert_playlist(account_id, remote_id, title, len(tracks),
                                             'completed-copy', now)
        owner.playlists.set_playlist_tracks(pid, [existing_track(
            track['video_id'], track['title'], track['artist']) for track in tracks])
        return {'new_ytm': remote_id, 'pid': pid, 'added': len(tracks)}

    monkeypatch.setattr(playlist_generated, 'create_generated_playlist', materialize)
    monkeypatch.setattr(store.tracks, 'observe_track', observed_track)
    protos = []
    for mid in (1, 2):
        lane, tracks = next(iter(payload[str(mid)].items()))
        proto = home_cards._proto(store, lane, f'Mode {mid}', tracks, clock[0])
        proto.update(mode_id=mid, recipe={'model': 'mode', 'mode_id': mid, 'lane': lane})
        protos.append(proto)
    result = home_cards.CardsResult(protos, [{'proto': proto, 'pool': None} for proto in protos],
                                   None, None, [], [], home_cards._epoch(store, 'cards'), None)
    home_prepared.remember(store, None, result, clock[0])
    home_cache.put(store, 'home_prepared', home_cache.read(store, 'home_tuned'), clock[0])
    fake = FakeClient(catalog=remote_catalog(
        track for mid in (1, 2) for tracks in payload[str(mid)].values() for track in tracks))
    app = create_app(store, lambda: {iid: fake}, now_fn=lambda: clock[0])
    app.state.ctx.templates.env.bytecode_cache = home_page_bytecode
    client = TestClient(app, base_url="http://127.0.0.1")
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
        pid = store.conn.execute(
            'SELECT membership_ref_id FROM rec_mode_picks WHERE mode_id=?', (mid,)).fetchone()[0]
        selected[mid] = next(playlist for playlist in store.playlists.get_playlists() if playlist.id == pid)
    # These are the production ingestion functions used by /bridge/ws; no listen-stat mock.
    ctx = SimpleNamespace(store=store)
    first, second = cards[1]["tracks"][:2]
    for offset, track, position in ((120, first, 190), (480, second, 20), (86400 + 120, first, 190)):
        at = clock[0] + offset
        assert live_plays.handle_play_event(ctx, {
            "type": "play", "videoId": track["video_id"], "title": track["title"],
            "artist": track["artist"], "playlist": selected[1].provider_playlist_id}, at)
        assert player_events.handle_player_event(ctx, {
            "type": "pevent", "kind": "track_exit", "videoId": track["video_id"],
            "position": position, "duration": 200, "playlist": selected[1].provider_playlist_id}, at + position)
    yield SimpleNamespace(store=store, cards=cards, selected=selected, ctx=ctx, start=clock[0], account_id=iid)
    client.close()


def test_saved_mode_retains_attributed_plays_and_exit_depth_over_multiple_days(engagement):
    data = engagement
    events = data.store.history.play_events_since(data.start)
    assert len(events) == 3
    assert {event["account_id"] for event in events} == {data.account_id}
    assert {event["playlist_ytm_id"] for event in events} == {data.selected[1].provider_playlist_id}
    assert events[-1]["played_at"] - events[0]["played_at"] == 86400
    exits = data.store.player_events.player_events_since(data.start, kind="track_exit")
    assert [classify_exit(event["position"], event["duration"]) for event in exits] == ["completion", "skip", "completion"]
    assert {event["playlist_ytm_id"] for event in exits} == {data.selected[1].provider_playlist_id}
    assert {event["account_id"] for event in exits} == {data.account_id}
    evidence = mode_eval.choice_evidence(data.store)
    assert [(row["mode_id"], row["offered"], row["picked"]) for row in evidence["rows"]] == [
        (1, 1, 1), (2, 1, 1)]


def test_saved_mode_retains_the_link_needed_to_read_skip_and_completion_depth(engagement):
    store = engagement.store
    picks = dict(store.conn.execute('SELECT membership_ref_id, mode_id FROM rec_mode_picks'))
    playlist_ids = {playlist.provider_playlist_id: playlist.id for playlist in store.playlists.get_playlists()}
    exits = store.player_events.player_events_since(engagement.start, kind="track_exit")
    # The saved choice remains linked to its playlist and raw exit observations.
    depth = [(picks[playlist_ids[event["playlist_ytm_id"]]],
              classify_exit(event["position"], event["duration"])) for event in exits]
    assert depth == [(1, "completion"), (1, "skip"), (1, "completion")]
    assert exits[-1]["at"] - exits[0]["at"] == 86400


def test_organic_play_retains_separate_provenance_without_changing_saved_choices(engagement):
    data = engagement
    track = data.cards[2]["tracks"][0]
    before = mode_eval.choice_evidence(data.store)
    saved_events = data.store.history.play_events_since(data.start)
    assert live_plays.handle_play_event(data.ctx, {
        "type": "play", "videoId": track["video_id"], "title": track["title"],
        "artist": track["artist"]}, data.start + 2 * 86400)
    events = data.store.history.play_events_since(data.start)
    assert events[:-1] == saved_events
    assert events[-1]["playlist_ytm_id"] is None
    assert events[-1]["account_id"] == data.account_id
    assert mode_eval.choice_evidence(data.store) == before
