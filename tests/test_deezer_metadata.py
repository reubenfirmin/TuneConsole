"""Deezer's already-fetched genre/year reach shared findings and retain metadata scope."""
import pytest

from yt_playlist.providers import deezer, waterfall
from yt_playlist.providers.base import EnrichmentResult


@pytest.fixture
def requests(monkeypatch):
    calls = []

    def get_json(url):
        calls.append(url)
        if "/search/track" in url:
            return {"data": [{"id": 42, "title": "Song", "artist": {"name": "Artist"}}]}
        if "/track/42" in url:
            return {"id": 42, "title": "Song", "artist": {"name": "Artist"}, "bpm": 128,
                    "release_date": "2000-01-02", "album": {"id": 99}, "duration": 200}
        if "/album/99" in url:
            return {"id": 99, "genres": {"data": [{"name": "Electronic"}]}, "release_date": "2001-02-03"}
        raise AssertionError(url)

    monkeypatch.setattr(deezer, "_get_json", get_json)
    return calls


def test_probe_exposes_existing_facts_without_extra_requests(requests):
    result = deezer.probe({"title": "Song", "artist": "Artist"})
    assert result.fields == {"bpm": 128, "year": "2000", "genre": "Electronic"}
    assert len(requests) == 3


@pytest.mark.parametrize("membership", ["library", "discovery"])
@pytest.mark.parametrize("chosen", [False, True])
def test_deezer_only_facts_reach_shared_sink_and_preserve_choices(store, requests, membership, chosen):
    if membership == "library":
        entry = store.tracks.upsert_track("video", "Song", "Artist", "Album", 200)
    else:
        store.discovery.upsert_discovered_track("song|artist", "video", "Song", "Artist", "Album", None,
                                      None, None, "BROWSE", 100)
        entry = store.tracks.track_ids_for_videos(["video"])["video"]
    if chosen:
        store.tracks.set_track_genre(entry, "Chosen genre")
        store.tracks.set_track_year(entry, "1990")

    class EmptyProvider:
        name = "musicbrainz"
        def available(self, store): return True
        def reset(self): pass
        def tripped(self): return False
        def probe(self, track, store): return EnrichmentResult(self.name, {})

    events = []
    completed = waterfall.run_waterfall(store, [store.tracks.catalog_subject(entry)],
        [{"name": "musicbrainz", "enabled": True}, {"name": "deezer", "enabled": True}],
        events.append, registry={"musicbrainz": EmptyProvider(), "deezer": deezer})
    assert completed
    assert len(requests) == 3
    assert store.tracks.get_track_enrichment(entry) == (("Chosen genre", "1990") if chosen else ("Electronic", "2000"))
    fields = store.enrichment_state.enrichment_attempts("song|artist")["deezer"]["fields"]
    assert fields["genre"] == "Electronic" and fields["year"] == "2000"
    logged = {row["field"]: (row["value"], row["field_scope"]) for row in store.conn.execute(
        "SELECT field,value,field_scope FROM enrichment_log WHERE provider='deezer'")}
    assert logged["genre"] == ("Electronic", "recording")
    assert logged["year"] == ("2000", "release")
    assert logged["bpm"] == ("128.0", "recording")
    assert store.tracks.get_track_audio(entry)[0] == 128
