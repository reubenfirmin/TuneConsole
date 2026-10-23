import pytest

from yt_playlist.rec import road_trip_candidates as candidates


@pytest.fixture(autouse=True)
def metadata(monkeypatch):
    candidates._FACTS_CACHE.clear()
    candidates._ARTIST_GENRE_CACHE.clear()
    candidates._GENRE_ARTIST_CACHE.clear()
    now = [0]
    monkeypatch.setattr(candidates, "monotonic", lambda: now[0])
    monkeypatch.setattr(candidates.lastfm, "api_key", lambda store: store)
    yield now
    candidates._FACTS_CACHE.clear()
    candidates._ARTIST_GENRE_CACHE.clear()
    candidates._GENRE_ARTIST_CACHE.clear()


def test_missing_and_changed_credentials_are_seen_immediately(monkeypatch):
    calls = []
    monkeypatch.setattr(candidates.lastfm, "artist_genre",
                        lambda artist, key: calls.append(key) or key)
    assert candidates.artist_genre(None, "Artist") is None
    assert candidates.artist_genre("first", "Artist") == "first"
    assert candidates.artist_genre("second", "Artist") == "second"
    assert calls == ["first", "second"]


def test_ambiguous_miss_retries_and_positive_result_expires(metadata, monkeypatch):
    answers = iter([None, "Rock", "Folk"])
    monkeypatch.setattr(candidates.lastfm, "artist_genre", lambda *args: next(answers))
    assert candidates.artist_genre("key", "Artist") is None
    assert candidates.artist_genre("key", "Artist") is None
    metadata[0] += candidates._METADATA_RETRY_TTL
    assert candidates.artist_genre("key", "Artist") == "Rock"
    assert candidates.artist_genre("key", "Artist") == "Rock"
    metadata[0] += candidates._METADATA_TTL
    assert candidates.artist_genre("key", "Artist") == "Folk"


def test_genre_limits_and_credentials_are_part_of_result_contract(monkeypatch):
    monkeypatch.setattr(candidates.lastfm, "tag_top_artists",
                        lambda genre, key, limit: [f"{key}-{i}" for i in range(limit)])
    assert len(candidates.genre_artists("first", "Rock", limit=2)) == 2
    assert len(candidates.genre_artists("first", "Rock", limit=10)) == 10
    assert candidates.genre_artists("second", "Rock", limit=2) == ["second-0", "second-1"]
    result = candidates.genre_artists("first", "Rock", limit=2)
    result.clear()
    assert len(candidates.genre_artists("first", "Rock", limit=2)) == 2


def test_fallback_retries_lastfm_after_failure(metadata, monkeypatch):
    answers = iter([[], ["Recovered"]])
    monkeypatch.setattr(candidates.lastfm, "tag_top_artists", lambda *a, **kw: next(answers))
    monkeypatch.setattr(candidates.musicbrainz, "tag_artists", lambda *a, **kw: ["Fallback"])
    assert candidates.genre_artists("key", "Rock") == ["Fallback"]
    assert candidates.genre_artists("key", "Rock") == ["Fallback"]
    metadata[0] += candidates._METADATA_RETRY_TTL
    assert candidates.genre_artists("key", "Rock") == ["Recovered"]


def test_genre_missing_key_then_configured_and_empty_retry(metadata, monkeypatch):
    answers = iter([[], ["Recovered"]])
    monkeypatch.setattr(candidates.lastfm, "tag_top_artists", lambda *a, **kw: next(answers))
    monkeypatch.setattr(candidates.musicbrainz, "tag_artists", lambda *a, **kw: ["Fallback"])
    assert candidates.genre_artists(None, "Rock") == ["Fallback"]
    assert candidates.genre_artists("key", "Rock") == ["Fallback"]
    metadata[0] += candidates._METADATA_RETRY_TTL
    assert candidates.genre_artists("key", "Rock") == ["Recovered"]


def test_decade_partial_result_retries_missing_provider(metadata, monkeypatch):
    answers = iter([[], ["Dated"]])
    monkeypatch.setattr(candidates.lastfm, "tag_top_artists", lambda *a, **kw: ["Ranked"])
    monkeypatch.setattr(candidates.musicbrainz, "tag_artists", lambda *a, **kw: next(answers))
    assert candidates.genre_artists("key", "Rock", decade="1990") == ["Ranked"]
    metadata[0] += candidates._METADATA_RETRY_TTL
    assert candidates.genre_artists("key", "Rock", decade="1990") == ["Ranked", "Dated"]


def test_metadata_caches_evict_least_recently_used(monkeypatch):
    monkeypatch.setattr(candidates, "_METADATA_CACHE_LIMIT", 2)
    calls = []
    monkeypatch.setattr(candidates.lastfm, "artist_genre",
                        lambda artist, key: calls.append(artist) or "Rock")
    monkeypatch.setattr(candidates.lastfm, "tag_top_artists", lambda *a, **kw: ["Artist"])
    for artist in ["A", "B", "A", "C", "B"]:
        candidates.artist_genre("key", artist)
        candidates.genre_artists("key", artist)
    assert calls == ["A", "B", "C", "B"]
    assert len(candidates._ARTIST_GENRE_CACHE) == len(candidates._GENRE_ARTIST_CACHE) == 2


@pytest.mark.parametrize('initial', [
    {"duration": None, "year": None, "genre": None, "popularity": None},
    {"duration": 180, "year": 1990, "genre": None, "popularity": 10},
])
def test_deezer_missing_and_partial_facts_retry_after_failure_window(metadata, monkeypatch, initial):
    recovered = {"duration": 180, "year": 1990, "genre": "Rock", "popularity": 10}
    answers = iter([initial, recovered])
    monkeypatch.setattr(candidates.deezer, "lookup", lambda *args: next(answers))
    assert candidates._facts("Song", "Artist") == initial
    metadata[0] = candidates._METADATA_RETRY_TTL - 1
    assert candidates._facts("Song", "Artist") == initial
    metadata[0] += 1
    assert candidates._facts("Song", "Artist") == recovered


def test_deezer_success_expires_and_zero_popularity_is_complete(metadata, monkeypatch):
    original = {"duration": 180, "year": 1990, "genre": "Rock", "popularity": 0}
    changed = dict(original, genre="Folk")
    answers = iter([original, changed])
    monkeypatch.setattr(candidates.deezer, "lookup", lambda *args: next(answers))
    result = candidates._facts("Song", "Artist")
    result.clear()
    metadata[0] = candidates._METADATA_TTL - 1
    assert candidates._facts("Song", "Artist") == original
    metadata[0] += 1
    assert candidates._facts("Song", "Artist") == changed


def test_deezer_lru_eviction_preserves_recent_hits_without_locking_provider(monkeypatch):
    monkeypatch.setattr(candidates, "_METADATA_CACHE_LIMIT", 2)
    calls = []

    def lookup(title, artist):
        # Other metadata requests must still be able to read their caches during provider I/O.
        assert candidates._METADATA_CACHE_LOCK.acquire(blocking=False)
        candidates._METADATA_CACHE_LOCK.release()
        calls.append(title)
        return {"duration": 180, "year": 1990, "genre": "Rock", "popularity": 10}

    monkeypatch.setattr(candidates.deezer, "lookup", lookup)
    for title in ["A", "B", "A", "C", "B"]:
        candidates._facts(title, "Artist")
    assert calls == ["A", "B", "C", "B"]
    assert len(candidates._FACTS_CACHE) == 2
