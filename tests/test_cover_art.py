"""Cover art enrichment: your uploads (and any track YouTube has no art for) get a real cover from
the providers instead of YouTube's generic grey disc.

YouTube serves a placeholder image rather than 404ing when it has no art, so "has a thumbnail" is not
the same as "has a cover". These tests pin the distinction, the fill-only rule (a real YouTube cover
is never overwritten by a provider's guess), and the Deezer-before-CoverArtArchive priority.
"""
from yt_playlist.providers import deezer, waterfall
from yt_playlist.providers.base import EnrichmentResult
from yt_playlist.util.thumbnails import is_placeholder_art

_YT_TRACK_PH = "https://www.gstatic.com/youtube/media/ytm/images/cover_track_default@1200.png?"
_YT_ALBUM_PH = "https://www.gstatic.com/youtube/media/ytm/images/cover_album_default@1200.png?"
_REAL = "https://lh3.googleusercontent.com/real-cover=w544"
_DEEZER = "https://cdn-images.dzcdn.net/images/cover/09b2/1000x1000-000000-80-0-0.jpg"


def test_is_placeholder_art_spots_both_youtube_variants():
    # Both variants are live in a real library: 78 cover_track_default, 17 cover_album_default.
    assert is_placeholder_art(_YT_TRACK_PH) is True
    assert is_placeholder_art(_YT_ALBUM_PH) is True


def test_is_placeholder_art_leaves_real_art_alone():
    assert is_placeholder_art(_REAL) is False
    assert is_placeholder_art(_DEEZER) is False
    assert is_placeholder_art(None) is False
    assert is_placeholder_art("") is False


def _deezer_stub(monkeypatch, track_payload):
    """Stub Deezer's HTTP so the search resolves to one track id, then returns track_payload."""
    calls = []

    def fake_get(url):
        calls.append(url)
        if "/search/track" in url:
            return {"data": [{"id": 99}]}
        if "/track/99" in url:
            return track_payload
        return {}

    monkeypatch.setattr(deezer, "_get_json", fake_get)
    return calls


def test_deezer_returns_album_cover_from_the_track_lookup(monkeypatch):
    """The cover rides along in the /track response we already make: no extra request, no API key."""
    _deezer_stub(monkeypatch, {"bpm": 130, "rank": 500, "album": {"id": 7, "cover_xl": _DEEZER}})
    assert deezer.enrich("Don't Give Up", "Chicane")["art"] == _DEEZER


def test_deezer_art_survives_a_failing_album_lookup(monkeypatch):
    """Art comes from the track payload, so it must not depend on the /album call (which exists only
    to read `label` and is allowed to fail)."""
    def fake_get(url):
        if "/search/track" in url:
            return {"data": [{"id": 99}]}
        if "/track/99" in url:
            return {"bpm": 130, "album": {"id": 7, "cover_xl": _DEEZER}}
        raise OSError("album lookup down")

    monkeypatch.setattr(deezer, "_get_json", fake_get)
    feat = deezer.enrich("Don't Give Up", "Chicane")
    assert feat["art"] == _DEEZER
    assert feat["label"] is None


def test_deezer_art_is_none_when_the_album_has_no_cover(monkeypatch):
    _deezer_stub(monkeypatch, {"bpm": 130, "album": {"id": 7}})
    assert deezer.enrich("x", "y")["art"] is None


def test_deezer_art_is_none_on_no_match(monkeypatch):
    monkeypatch.setattr(deezer, "_get_json", lambda url: {"data": []})
    assert deezer.enrich("x", "y")["art"] is None


class _FakeProvider:
    def __init__(self, name, fields):
        self.name, self._fields = name, fields

    def probe(self, track, store=None):
        return EnrichmentResult(self.name, dict(self._fields))

    def available(self, store=None):
        return True

    def tripped(self):
        return False

    def reset(self):
        pass


def _run(store, track_id, providers, monkeypatch, caa=None):
    """Run the waterfall over one track with fake providers, stubbing Cover Art Archive."""
    seen = []

    def fake_front_cover(release_ids):
        seen.append(list(release_ids))
        return caa

    monkeypatch.setattr(waterfall.coverart, "front_cover", fake_front_cover)
    registry = {p.name: p for p in providers}
    config = [{"name": p.name, "enabled": True} for p in providers]
    tracks = store.album_tracks_for_waterfall("MPREb_x")
    waterfall.run_waterfall(store, tracks, config, on_progress=lambda e: None, registry=registry)
    return seen


def _seed(store, thumbnail=None):
    return store.upsert_track("v1", "T", "A", "Alb", 100, album_browse_id="MPREb_x",
                              thumbnail=thumbnail)


def test_waterfall_prefers_deezer_art_over_cover_art_archive(store, monkeypatch):
    """Deezer is the primary source. Provider config order runs musicbrainz FIRST, so a naive
    fill-only write would let CAA win: the pick must be by explicit priority, not run order."""
    tid = _seed(store, _YT_TRACK_PH)
    mb = _FakeProvider("musicbrainz", {"mb_release_ids": ["rel-1"]})
    dz = _FakeProvider("deezer", {"art": _DEEZER})
    caa_calls = _run(store, tid, [mb, dz], monkeypatch, caa="http://caa/front.jpg")
    assert _art_of(store, tid) == _DEEZER
    assert caa_calls == [], "CAA must not be called when Deezer already supplied a cover"


def test_waterfall_falls_back_to_cover_art_archive_when_deezer_misses(store, monkeypatch):
    tid = _seed(store, _YT_TRACK_PH)
    mb = _FakeProvider("musicbrainz", {"mb_release_ids": ["rel-1", "rel-2"]})
    dz = _FakeProvider("deezer", {"bpm": 130.0})          # no art
    caa_calls = _run(store, tid, [mb, dz], monkeypatch, caa="http://caa/front.jpg")
    assert _art_of(store, tid) == "http://caa/front.jpg"
    assert caa_calls == [["rel-1", "rel-2"]]              # release ids came from MusicBrainz


def test_waterfall_skips_art_entirely_when_the_track_already_has_real_art(store, monkeypatch):
    tid = _seed(store, _REAL)
    mb = _FakeProvider("musicbrainz", {"mb_release_ids": ["rel-1"]})
    dz = _FakeProvider("deezer", {"art": _DEEZER})
    caa_calls = _run(store, tid, [mb, dz], monkeypatch, caa="http://caa/front.jpg")
    assert _art_of(store, tid) == _REAL                   # untouched
    assert caa_calls == []


def test_release_ids_are_plumbing_and_never_logged_as_a_finding(store, monkeypatch):
    """mb_release_ids exists only to key CAA. Logging it would pollute the enrichment log (and the
    transparency UI) with an internal list that is not a finding about the track."""
    tid = _seed(store, _YT_TRACK_PH)
    mb = _FakeProvider("musicbrainz", {"genre": "Trance", "mb_release_ids": ["rel-1"]})
    _run(store, tid, [mb], monkeypatch, caa=None)
    logged = [r[0] for r in store.conn.execute(
        "SELECT field FROM enrichment_log WHERE track_id=?", (tid,))]
    assert "genre" in logged
    assert "mb_release_ids" not in logged


def test_cover_art_archive_url_is_upgraded_to_https(monkeypatch):
    """CAA hands back http:// urls. The browser loads these directly, so keep them off plaintext."""
    from yt_playlist.providers import coverart
    monkeypatch.setattr(coverart, "_get_json", lambda url: {
        "images": [{"front": True, "thumbnails": {"500": "http://coverartarchive.org/release/x/1-500.jpg"}}]})
    assert coverart.front_cover(["rel-1"]) == "https://coverartarchive.org/release/x/1-500.jpg"


def _art_of(store, tid):
    return store.conn.execute("SELECT thumbnail FROM tracks WHERE id=?", (tid,)).fetchone()["thumbnail"]


def test_set_track_art_fills_when_there_is_no_art(store):
    tid = store.upsert_track("v1", "T", "A", "Alb", 100)
    store.set_track_art(tid, _DEEZER)
    assert _art_of(store, tid) == _DEEZER


def test_set_track_art_replaces_a_youtube_placeholder(store):
    # The whole point: YouTube's grey disc is not art, and must not block a real cover.
    tid = store.upsert_track("v1", "T", "A", "Alb", 100, thumbnail=_YT_TRACK_PH)
    store.set_track_art(tid, _DEEZER)
    assert _art_of(store, tid) == _DEEZER


def test_set_track_art_never_overwrites_real_youtube_art(store):
    # YouTube's own cover is authoritative; a provider's guess must not clobber it.
    tid = store.upsert_track("v1", "T", "A", "Alb", 100, thumbnail=_REAL)
    store.set_track_art(tid, _DEEZER)
    assert _art_of(store, tid) == _REAL


def test_set_track_art_ignores_an_empty_url(store):
    tid = store.upsert_track("v1", "T", "A", "Alb", 100, thumbnail=_YT_TRACK_PH)
    store.set_track_art(tid, None)
    assert _art_of(store, tid) == _YT_TRACK_PH


def test_waterfall_pending_includes_a_fully_enriched_track_with_placeholder_art(store):
    """A track can be complete on genre/year/audio yet still show the grey disc. Without art in the
    pending predicate the waterfall would never revisit it, so the cover could never arrive."""
    tid = store.upsert_track("v1", "T", "A", "Alb", 100, album_browse_id="MPREb_x",
                             thumbnail=_YT_TRACK_PH)
    store.set_track_enrichment(tid, "Trance", "2000")
    store.set_track_audio(tid, bpm=138.0, energy=0.8, danceability=0.7)
    pending = store.album_tracks_for_waterfall("MPREb_x")
    assert [p["id"] for p in pending] == [tid]


def test_waterfall_pending_skips_a_fully_enriched_track_with_real_art(store):
    tid = store.upsert_track("v1", "T", "A", "Alb", 100, album_browse_id="MPREb_x", thumbnail=_REAL)
    store.set_track_enrichment(tid, "Trance", "2000")
    store.set_track_audio(tid, bpm=138.0, energy=0.8, danceability=0.7)
    assert store.album_tracks_for_waterfall("MPREb_x") == []
