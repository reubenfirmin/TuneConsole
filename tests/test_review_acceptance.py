"""Known QA defects deferred for deployment; remove each ticketed skip when fixed.

These isolated checks use no live accounts or network writes.
"""
import datetime
import io
import json
import zipfile

import numpy as np
import pytest

from tests.conftest import FakeClient, _track
from yt_playlist.core.store import Store
from yt_playlist.library.spotify_library import import_spotify_library
from yt_playlist.library.executor import add_tracks_to_playlist, create_generated_playlist
from yt_playlist.rec import discover, graduation, scoring, surfaces

DAY = 86400


@pytest.fixture
def db():
    store = Store(":memory:")
    store.init_schema()
    store.identities.upsert_identity("main", "bridge", None, True)
    yield store
    store.conn.close()


def spotify_zip(count=1):
    out = io.BytesIO()
    items = [{"track": {"trackName": f"Song {i}", "artistName": "Artist"}}
             for i in range(count)]
    with zipfile.ZipFile(out, "w") as archive:
        archive.writestr("Playlist1.json", json.dumps({"playlists": [
            {"name": "Road songs", "items": items}]}))
    return out.getvalue()


class SearchClient(FakeClient):
    def search(self, query, filter="songs"):
        title = query.removesuffix(" Artist")
        track = _track("vid_" + title.replace(" ", "_"), title, "Artist")
        self._catalog[track["videoId"]] = track
        return [track]


def shown(db, request, at):
    db.rec_surface.record_served_impressions(request, "home", [{
        "lane": "wheelhouse", "identity_key": "song|artist", "rank": 1,
        "model_version": "test"}], at)


def test_85_old_mood_has_less_ranking_influence(db):
    now = 1000 * DAY
    db.rec_surface.record_mood(["song|artist"], 1, now)
    vectors = np.eye(2)
    idx = {"song|artist": 0, "other|artist": 1}
    fresh = scoring._mood_components(np.zeros(2), db, now, vectors, idx)["transient"][0]
    old = scoring._mood_components(np.zeros(2), db, now + 21 * DAY, vectors, idx)["transient"][0]
    assert old <= fresh / 8 + 1e-9, {"fresh_delta": fresh, "21_day_delta": old}


def test_22_unrelated_same_name_in_other_account_does_not_block_import(db):
    other_identity = db.identities.upsert_identity("other", "other-bridge", None, False)
    tid = db.tracks.upsert_track("unrelated", "Unrelated song", "Another artist", "", 200)
    pid = db.playlists.upsert_playlist(other_identity, "OTHER", "Road songs", 1, "hash", 1)
    db.playlists.set_playlist_tracks(pid, [tid])
    report = import_spotify_library(db, spotify_zip(), SearchClient(), 1, 100)
    assert report["playlists_imported"] == 1, report["details"]["playlists"]


def test_22_retry_resumes_playlist_after_second_batch_failure(db):
    class FailSecondBatch(SearchClient):
        calls = 0

        def add_playlist_items(self, playlistId, videoIds):
            self.calls += 1
            if self.calls == 2:
                raise RuntimeError("transient failure after first 100 tracks")
            return super().add_playlist_items(playlistId, videoIds)

    client = FailSecondBatch()
    raw = spotify_zip(101)
    first = import_spotify_library(db, raw, client, 1, 100)
    assert first["playlists_failed"] == 1
    assert first["tracks_imported"] == 100
    assert first["details"]["playlists"][0]["tracks_added"] == 100
    assert db.playlists.get_playlists()[0].track_count == 100
    again = import_spotify_library(db, raw, client, 1, 101)
    assert again["playlists_imported"] == 1
    assert again["tracks_imported"] == 1
    assert len(client.created) == 1, {"remote_playlists_created": client.created}
    assert len(client._tracks[client.created[0][0]]) == 101
    assert db.playlists.get_playlists()[0].track_count == 101


def test_113_later_impression_outside_report_owns_the_outcome(db):
    db.tracks.upsert_track("vid", "song", "artist", None, None)
    shown(db, "first", 100)
    shown(db, "second", 150)
    db.history.record_play_event(1, "song|artist", "vid", 160)
    first_report = db.rec_surface.recommendation_scorecard(90, 110, min_impressions=1)
    assert first_report["played"] == 0, first_report


def test_113_generated_provenance_survives_playlist_promotion(db):
    db.tracks.upsert_track("vid", "song", "artist", None, None)
    db.conn.execute("INSERT INTO playlist_group(ytm,name) VALUES ('PLGEN','Generated')")
    db.playlists.mark_generated_playlist('PLGEN')
    db.conn.commit()
    shown(db, "one", 100)
    db.history.record_play_event(1, "song|artist", "vid", 120, playlist_ytm_id="RDAMPLPLGEN")
    before = db.rec_surface.recommendation_scorecard(90, 200)
    assert before["generated"] == 1 and before["organic"] == 0
    db.conn.execute("DELETE FROM playlist_group WHERE ytm='PLGEN'")
    db.conn.commit()
    after = db.rec_surface.recommendation_scorecard(90, 200)
    assert after["generated"] == 1 and after["organic"] == 0, after


def test_58_same_local_day_today_survives_utc_midnight(db):
    # Both captures occur on Oct 20 in Costa Rica (17:30 and 18:30).
    first = datetime.datetime(2026, 10, 20, 23, 30, tzinfo=datetime.timezone.utc).timestamp()
    second = first + 3600
    items = [("song|artist", "Today")]
    assert db.history.record_history_plays(1, first, items, source_timezone="America/Costa_Rica") == 1
    assert db.history.record_history_plays(1, second, items, source_timezone="America/Costa_Rica") == 0


def test_114_explicit_genre_mute_survives_automatic_graduation(db):
    now = 1000 * DAY
    db.rec_model.set_weight("genre:jazz", 0, lo=0, hi=2, now=now)
    assert db.rec_model.get_weights(now=now + DAY)["genre:jazz"] == 0
    graduation.graduate_facet(db, "genre:jazz", 2, now + DAY)
    assert db.rec_model.get_weights(now=now + DAY)["genre:jazz"] == 0


def test_40_alternate_remote_position_survives_readback_lag(db):
    anchor = _track("anchor", "Original", "Artist", set_video_id="set_anchor")
    following = _track("following", "Next", "Other", set_video_id="set_following")
    alternate = _track("alt", "Alternate", "Artist", set_video_id="set_alt")
    tids = [db.tracks.upsert_track(t["videoId"], t["title"], t["artists"][0]["name"], "", 200)
            for t in (anchor, following)]
    pid = db.playlists.upsert_playlist(1, "PL", "Playlist", 2, "hash", 1)
    db.playlists.set_playlist_tracks(pid, tids)

    class LaggingClient(FakeClient):
        def get_playlist(self, playlistId, limit=100):
            result = super().get_playlist(playlistId, limit)
            return {"tracks": [t for t in result["tracks"] if t["videoId"] != "alt"]}

    client = LaggingClient(tracks={"PL": [anchor, following]}, catalog={"alt": alternate})
    add_tracks_to_playlist(db, pid, [{"videoId": "alt", "title": "Alternate", "artist": "Artist",
                                    "duration": 200}], client, 100, after_video_id="anchor")
    assert [t["video_id"] for t in db.charts.playlist_tracks_detail(pid)] == ["anchor", "alt", "following"]
    assert [t["videoId"] for t in client._tracks["PL"]] == ["anchor", "alt", "following"]


def test_44_cold_discovery_long_mix_is_excluded_before_saving(db):
    for key, video, title in [("one|a", "one", "Song one"), ("mix|dj", "mix", "DJ concert"),
                              ("two|b", "two", "Song two")]:
        db.discovery.upsert_discovered_track(key, video, title, key.split("|")[1], "", None,
                                   "Techno", None, "source", 100)
    candidates = surfaces.cold_candidates(db, 100)
    tracks = [{"video_id": t.video_id, "title": t.title, "artist": t.artist} for t in candidates]
    client = FakeClient(song_durations={"one": 200, "mix": 7200, "two": 200})
    create_generated_playlist(db, "Fresh songs", tracks, client, 100, identity_id=1)
    assert "mix" not in [video for _, batch in client.added for video in batch]


def test_86_boosting_cold_genre_does_not_lower_its_rank(db, monkeypatch):
    class Projection:
        def predict(self, genre, year=None, audio=None, artist=None):
            return {"jazz": np.array([-.5, np.sqrt(.75)]),
                    "ambient": np.array([-.6, .8])}[genre]

    for key, genre in [("one|a", "jazz"), ("two|b", "ambient")]:
        db.discovery.upsert_discovered_track(key, key[0], key, key[-1], "", None,
                                   genre, None, "source", 100)
    monkeypatch.setattr(discover.ContentProjection, "fit", classmethod(lambda cls, store: Projection()))
    monkeypatch.setattr(surfaces, "playlist_taste", lambda store: scoring.PlaylistTaste(
        ["context"], np.array([[1., 0.]]), np.array([1.])))
    before = [t.key for t in surfaces.cold_candidates(db, 100)]
    assert before == ["one|a", "two|b"]
    db.rec_model.set_weight("genre:jazz", 2, lo=0, hi=2, now=100)
    after = [t.key for t in surfaces.cold_candidates(db, 100)]
    assert after.index("one|a") <= before.index("one|a"), {"before": before, "after": after}
