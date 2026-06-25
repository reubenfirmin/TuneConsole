"""The enrichment waterfall harness: ordering/fill, logging, MBID hand-off, conflicts, control flow."""
from yt_playlist.providers.base import EnrichmentResult
from yt_playlist.providers.waterfall import run_waterfall


class FakeProv:
    def __init__(self, name, by_title=None, avail=True):
        self.name = name
        self._by_title = by_title or {}          # {title: {field: value}}
        self._avail = avail
        self.calls = []

    def probe(self, track, store):
        self.calls.append(track["title"])
        return EnrichmentResult(self.name, dict(self._by_title.get(track["title"], {})))

    def available(self, store):
        return self._avail

    def tripped(self):
        return False

    def reset(self):
        pass


def _cfg(*names_enabled):
    return [{"name": n, "label": n.title(), "enabled": e} for n, e in names_enabled]


def _track(store, title="Hyperballad", artist="Bjork"):
    tid = store.upsert_track("v_" + title, title, artist, None, 200)
    return {"id": tid, "video_id": "v_" + title, "title": title, "artist": artist,
            "mb_recording_id": None}


def _run(store, tracks, providers, config, **kw):
    registry = {p.name: p for p in providers}
    events = []
    run_waterfall(store, tracks, config, events.append, registry=registry, **kw)
    return events


def test_first_in_order_fills_and_disagreement_is_recorded(store):
    t = _track(store)
    mb = FakeProv("musicbrainz", {"Hyperballad": {"genre": "Electronic", "year": "1995"}})
    dc = FakeProv("discogs", {"Hyperballad": {"genre": "Art Pop", "year": "1995"}})
    _run(store, [t], [mb, dc], _cfg(("musicbrainz", True), ("discogs", True)))
    # first provider in order wins the canonical fill
    assert store.get_track_enrichment(t["id"]) == ("Electronic", "1995")
    # genre disagreement recorded (year agrees -> no year conflict)
    rows = store.unresolved_conflicts_for_playlist  # exists
    c = store.conn.execute("SELECT field, candidates FROM enrichment_conflict WHERE track_id=?",
                           (t["id"],)).fetchall()
    assert len(c) == 1 and c[0]["field"] == "genre"


def test_every_provider_field_is_logged(store):
    t = _track(store)
    mb = FakeProv("musicbrainz", {"Hyperballad": {"genre": "Electronic"}})
    dc = FakeProv("discogs", {"Hyperballad": {"genre": "Art Pop"}})
    _run(store, [t], [mb, dc], _cfg(("musicbrainz", True), ("discogs", True)))
    logged = store.conn.execute(
        "SELECT provider, field, value FROM enrichment_log WHERE track_id=? ORDER BY id",
        (t["id"],)).fetchall()
    assert ("musicbrainz", "genre", "Electronic") in [tuple(r) for r in logged]
    assert ("discogs", "genre", "Art Pop") in [tuple(r) for r in logged]


def test_mbid_from_first_provider_is_visible_to_later_one(store):
    t = _track(store)
    mb = FakeProv("musicbrainz", {"Hyperballad": {"mb_recording_id": "mbid-1"}})

    class NeedsMbid(FakeProv):
        def probe(self, track, store):
            self.calls.append(track["title"])
            # only yields bpm if an MBID was handed down the waterfall
            return EnrichmentResult(self.name, {"bpm": 128.0} if track.get("mb_recording_id") else {})

    ab = NeedsMbid("acousticbrainz")
    _run(store, [t], [mb, ab], _cfg(("musicbrainz", True), ("acousticbrainz", True)))
    assert store.get_track_audio(t["id"])[0] == 128.0


def test_numeric_within_tolerance_does_not_conflict(store):
    t = _track(store)
    dz = FakeProv("deezer", {"Hyperballad": {"bpm": 120.0}})
    ab = FakeProv("acousticbrainz", {"Hyperballad": {"bpm": 121.0}})
    _run(store, [t], [dz, ab], _cfg(("deezer", True), ("acousticbrainz", True)))
    assert store.conn.execute("SELECT COUNT(*) n FROM enrichment_conflict WHERE track_id=?",
                              (t["id"],)).fetchone()["n"] == 0


def test_disabled_provider_is_skipped(store):
    t = _track(store)
    mb = FakeProv("musicbrainz", {"Hyperballad": {"genre": "Rock"}})
    dc = FakeProv("discogs", {"Hyperballad": {"genre": "Pop"}})
    _run(store, [t], [mb, dc], _cfg(("musicbrainz", True), ("discogs", False)))
    assert dc.calls == []                          # discogs never probed
    assert store.get_track_enrichment(t["id"])[0] == "Rock"


def test_unavailable_provider_is_skipped_with_notice(store):
    t = _track(store)
    lf = FakeProv("lastfm", {"Hyperballad": {"genre": "Pop"}}, avail=False)
    mb = FakeProv("musicbrainz", {"Hyperballad": {"genre": "Rock"}})
    events = _run(store, [t], [lf, mb], _cfg(("lastfm", True), ("musicbrainz", True)))
    assert lf.calls == []
    assert any("no API key" in e.get("text", "") for e in events)


def test_should_stop_halts_before_processing(store):
    t = _track(store)
    mb = FakeProv("musicbrainz", {"Hyperballad": {"genre": "Rock"}})
    _run(store, [t], [mb], _cfg(("musicbrainz", True)), should_stop=lambda: True)
    assert mb.calls == []
