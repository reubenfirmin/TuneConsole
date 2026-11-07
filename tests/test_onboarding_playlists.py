import pytest
from yt_playlist.rec import onboarding


def test_library_sample_spreads_genres(store):
    rows = [("house", i) for i in range(10)] + [("rock-indie", i) for i in range(10)] + \
           [("jazz", i) for i in range(10)]
    for g, i in rows:
        from tests.conftest import seed_catalog_track
        seed_catalog_track(store,f"{g}|{i}",f"{g}{i}",f"art{g}{i}",genre=g,video_id=f"v{g}{i}")
    store.conn.commit()
    out = onboarding.library_sample(store, n=6)
    assert 1 <= len(out) <= 6
    assert len({d["key"].split("|")[0] for d in out}) >= 2   # spans multiple genre families


@pytest.mark.parametrize('minutes', [5, 20, 120])
def test_library_starter_applies_saved_length_limit_before_sampling(store, minutes):
    from tests.conftest import seed_catalog_track
    from yt_playlist.rec import rec_params

    rec_params.set_param(store, 'max_track_minutes', minutes)
    lengths = {'song': 180, 'boundary': minutes * 60, 'unknown': None,
               'too-long': minutes * 60 + 1, 'dj-mix': 9000}
    for video, duration in lengths.items():
        seed_catalog_track(store, video + '|artist', video, 'Artist',
                           video_id=video, duration_s=duration)
    tracks = onboarding.library_sample(store, n=3)
    assert {t['video_id']: t['duration'] for t in tracks} == {
        'song': 180, 'boundary': minutes * 60, 'unknown': None}


def test_radio_starter_skips_long_home_tracks_including_cached_lengths(store):
    store.tracks.observe_track_entry('cached-mix', 'Cached DJ mix', 'Artist', duration_s=9000)

    class Client:
        def get_home(self):
            return [{'contents': [
                {'videoId': 'dj-mix', 'title': 'DJ mix', 'duration': '2:30:00'},
                {'videoId': 'concert', 'title': 'Concert', 'duration_seconds': 9000},
                {'videoId': 'cached-mix', 'title': 'Cached DJ mix'},
                {'videoId': 'song', 'title': 'Song', 'duration': '3:00'},
                {'videoId': 'unknown', 'title': 'Unknown length'}]}]

    tracks = onboarding.radio_sample(store, Client(), now=1, n=2)
    assert [(t['video_id'], t['duration']) for t in tracks] == [('song', 180), ('unknown', None)]


def test_radio_starter_excludes_long_discoveries(store, monkeypatch):
    from types import SimpleNamespace
    from yt_playlist.rec import surfaces

    store.tracks.observe_track_entry('dj-mix', 'DJ mix', 'Artist', duration_s=9000)
    candidates = [SimpleNamespace(video_id=video, title=video, artist='Artist', thumbnail=None, key=video)
                  for video in ('dj-mix', 'song')]
    monkeypatch.setattr(surfaces, 'cold_candidates', lambda *args, **kwargs: candidates)

    class Client:
        def get_home(self):
            return []

    assert [t['video_id'] for t in onboarding.radio_sample(store, Client(), now=1)] == ['song']


def test_radio_sample_falls_back_to_home_when_no_seeds(store):
    class FakeClient:
        def get_home(self):
            return [{"contents": [
                {"videoId": "h1", "title": "Home One", "artists": [{"name": "HA"}]},
                {"videoId": "h2", "title": "Home Two", "artists": [{"name": "HB"}]}]}]
        def get_watch_playlist(self, vid):
            return {"tracks": []}
    out = onboarding.radio_sample(store, FakeClient(), now=1.0, n=12)
    assert any(d["title"] == "Home One" for d in out)        # empty account -> get_home seeds it
    assert all(d.get("video_id") for d in out)


def test_radio_sample_no_client(store):
    assert onboarding.radio_sample(store, None, now=1.0, n=12) == []
