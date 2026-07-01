import pytest
from yt_playlist.core.store import Store
from yt_playlist.rec import onboarding


@pytest.fixture
def store():
    s = Store(":memory:"); s.init_schema(); return s


def test_library_sample_spreads_genres(store):
    rows = [("house", i) for i in range(10)] + [("rock-indie", i) for i in range(10)] + \
           [("jazz", i) for i in range(10)]
    for g, i in rows:
        store.conn.execute("INSERT INTO tracks (identity_key, title, artist, genre, video_id) "
                           "VALUES (?,?,?,?,?)", (f"{g}|{i}", f"{g}{i}", f"art{g}{i}", g, f"v{g}{i}"))
    store.conn.commit()
    out = onboarding.library_sample(store, n=6)
    assert 1 <= len(out) <= 6
    assert len({d["key"].split("|")[0] for d in out}) >= 2   # spans multiple genre families


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
