"""#38 §3 (reframed): rank genre/era-described tracks that have NO co-occurrence vector by the content
vector they already have. content_taste = the content-space sibling of playlist_taste; the Catalog card
includes content-vectored-but-collab-vectorless owned tracks."""
from tests.enrichment_support import seed_enrichment
from yt_playlist.rec import surfaces, embedding_vectors as embed
from tests import model_support
from yt_playlist.rec.scoring import content_taste


def _trk(s, vid, title, artist, genre):
    tid = s.tracks.upsert_track(vid, title, artist, None, None)
    seed_enrichment(s, tid, genre, "2020")
    return tid


def test_catalog_ranks_content_only_owned_track(store):
    s = store; iid = s.accounts.upsert_account("m", "c", None, True)
    a = _trk(s, "v1", "A", "X", "Techno"); b = _trk(s, "v2", "B", "Y", "Techno"); c = _trk(s, "v3", "C", "Z", "Techno")
    pl = s.playlists.upsert_playlist(iid, "P", "Techno", 3, "h", 0.0); s.playlists.set_playlist_tracks(pl, [a, b, c])
    _trk(s, "v4", "D", "W", "Techno")              # owned, genre, NOT in any playlist, NO co-occurrence vector
    model_support.build_content_model(s)  # content vectors for all; no collaborative vectors built
    ct = content_taste(s)
    assert ct and "Techno" in ct.titles
    assert "d|w" not in embed.load_vectors(s)[2]
    candidates = {item.key: item for item in surfaces.explore_for_you(s, 0.0, limit=10)}
    assert candidates["d|w"].score is not None  # a real content score, rather than the unscored catalog fallback
