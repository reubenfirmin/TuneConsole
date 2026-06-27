"""#38 §3 (reframed): rank genre/era-described tracks that have NO co-occurrence vector by the content
vector they already have. content_taste = the content-space sibling of playlist_taste; the Catalog card
includes content-vectored-but-collab-vectorless owned tracks."""
from yt_playlist.core.store import Store
from yt_playlist.rec import surfaces, embed, eval_recs
from yt_playlist.rec.scoring import content_taste


def _store():
    s = Store(":memory:"); s.init_schema(); return s


def _trk(s, vid, title, artist, genre):
    tid = s.upsert_track(vid, title, artist, None, None)
    s.set_track_enrichment(tid, genre, "2020")
    return tid


def test_content_taste_builds_centroids():
    s = _store(); iid = s.upsert_identity("m", "c", None, True)
    a = _trk(s, "v1", "A", "X", "Techno"); b = _trk(s, "v2", "B", "Y", "Techno")
    pl = s.upsert_playlist(iid, "P", "Techno", 2, "h", 0.0); s.set_playlist_tracks(pl, [a, b])
    embed.build_content_and_store(s)
    ct = content_taste(s)
    assert ct and "Techno" in ct.titles            # a content-space taste context was built


def test_catalog_ranks_content_only_owned_track():
    s = _store(); iid = s.upsert_identity("m", "c", None, True)
    a = _trk(s, "v1", "A", "X", "Techno"); b = _trk(s, "v2", "B", "Y", "Techno"); c = _trk(s, "v3", "C", "Z", "Techno")
    pl = s.upsert_playlist(iid, "P", "Techno", 3, "h", 0.0); s.set_playlist_tracks(pl, [a, b, c])
    _trk(s, "v4", "D", "W", "Techno")              # owned, genre, NOT in any playlist, NO co-occurrence vector
    embed.build_content_and_store(s)               # content vectors for all; no collaborative vectors built
    keys = {i.key for i in surfaces.explore_for_you(s, 0.0, limit=10)}
    assert "d|w" in keys                           # the content-only owned track is now rankable in Catalog


def test_content_rankable_counts_vectorless_owned():
    s = _store(); iid = s.upsert_identity("m", "c", None, True)
    a = _trk(s, "v1", "A", "X", "Techno"); b = _trk(s, "v2", "B", "Y", "Techno")
    pl = s.upsert_playlist(iid, "P", "Techno", 2, "h", 0.0); s.set_playlist_tracks(pl, [a, b])
    _trk(s, "v4", "D", "W", "Techno")
    embed.build_content_and_store(s)
    out = eval_recs.content_rankable(s)
    assert out["rankable"] >= 1 and out["rankable"] <= out["vectorless_owned"]
