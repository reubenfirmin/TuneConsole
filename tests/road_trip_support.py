"""Road Trip candidate builders and an explicitly imported network-isolation fixture."""
import pytest

from yt_playlist.rec import road_trip_candidates as candidates
@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Every outbound lookup their side makes, stubbed: Deezer facts, the Last.fm artist genre, and
    the genre -> top-artists resolution. autouse, because a genre input now reaches three providers
    and a test that forgets one silently makes real network calls (it did).

    Popularity descends with the title length, so 'ArtistA Song 0' is the biggest hit."""
    monkeypatch.setattr(candidates, "_facts",
                        lambda title, artist: {"popularity": 1000 - len(title), "year": 1995,
                                               "genre": "synthpop", "duration": 300})
    monkeypatch.setattr(candidates, "artist_genre", lambda store, artist: None)
    monkeypatch.setattr(candidates, "genre_artists",
                        lambda store, genre, decade=None, limit=12: [])
    return None


def _library(monkeypatch, store, songs):
    """Stand in for the whole collection. `songs` are (key, video_id, title, artist, plays, liked)."""
    monkeypatch.setattr(store.rec_query, "library_songs", lambda: [
        {"key": k, "video_id": v, "title": ti, "artist": ar, "album": "Alb", "thumbnail": None,
         "duration": 300, "genre": "", "year": None, "liked": liked}
        for k, v, ti, ar, plays, liked in songs])
    monkeypatch.setattr(store.rec_query, "play_counts", lambda: {
        k: plays for k, v, ti, ar, plays, liked in songs})


def _recipe(**kw):
    base = {"id": 1, "name": "Beach Run", "own_pct": 50, "familiarity_pct": 50,
            "target_minutes": 60, "artists": ["ArtistA"], "genres": []}
    return {**base, **kw}


def _stub_pools(monkeypatch, store=None, own_count=40, other_count=40, own_plays=None,
                own_genre=lambda i: "Rock", own_year=lambda i: 1990 + (i % 3) * 10):
    """A library of 5-minute songs on your side and an equally deep pool on theirs, so ratio
    assertions are exact. Your side is stubbed at the library query - the real seam now - and theirs
    at the per-input fetch."""
    songs = [{"key": f"k{i}", "video_id": f"m{i}", "title": f"Mine {i}",
              "artist": f"Mine Artist {i}", "album": "", "thumbnail": None, "duration": 300,
              "genre": own_genre(i), "year": own_year(i), "liked": False}
             for i in range(own_count)]
    if store is not None:
        monkeypatch.setattr(store.rec_query, "library_songs", lambda: songs)
        monkeypatch.setattr(store.rec_query, "play_counts",
                            lambda: {f"k{i}": (own_plays or (lambda i: i))(i) for i in range(own_count)})

    def songs_fn(client, kind, name, cap, store=None, decade=None):
        rows = [{"video_id": f"t{i}", "title": f"Theirs {i}", "artist": f"Their Artist {i}",
                 "album": "", "thumbnail": None, "duration": 300, "genre": "synthpop"}
                for i in range(other_count)]
        return rows[:cap], []

    monkeypatch.setattr(candidates, "other_input_songs", songs_fn)
    monkeypatch.setattr(candidates, "_facts",
                        lambda title, artist: {"popularity": 900 - int(title.split()[-1]),
                                               "year": 1980, "genre": "synthpop", "duration": 300})
