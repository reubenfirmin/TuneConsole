import numpy as np

from yt_playlist.core.store import Store
from yt_playlist.rec import embed
from yt_playlist.util.matching import identity_key


def _store(tmp_path):
    s = Store(str(tmp_path / "t.db")); s.init_schema()
    return s


def test_include_new_surfaces_out_of_corpus_track(tmp_path):
    s = _store(tmp_path)
    # two library tracks (techno seed + ambient other), each with a collaborative vector
    sid = s.upsert_track("vS", "Seed", "ArtistS", None, None); s.set_track_genre(sid, "Techno")
    oid = s.upsert_track("vO", "Other", "ArtistO", None, None); s.set_track_genre(oid, "Ambient")
    skey, okey = identity_key("Seed", "ArtistS"), identity_key("Other", "ArtistO")
    s.replace_rec_vectors([(skey, np.array([1, 0, 0, 0], dtype=np.float32).tobytes()),
                           (okey, np.array([0, 1, 0, 0], dtype=np.float32).tobytes())])
    # an OUT-OF-CORPUS techno track (not in the library)
    dkey = identity_key("NewTrack", "NewArtist")
    s.upsert_discovered_track(dkey, "vD", "NewTrack", "NewArtist", "Alb", "th",
                              "Techno", "2020", "bid", 1.0)
    embed.build_content_and_store(s)        # builds library vectors + model + discovered vectors

    new = [k for k, _ in embed.cluster_expand(s, pos_keys=[skey], topn=10, include_new=True)]
    base = [k for k, _ in embed.cluster_expand(s, pos_keys=[skey], topn=10, include_new=False)]
    assert dkey in new          # the techno out-of-corpus track is reachable from the techno seed
    assert dkey not in base     # ...only when opted in


def test_discovered_vectors_share_the_library_model_space(tmp_path):
    s = _store(tmp_path)
    sid = s.upsert_track("vS", "Seed", "ArtistS", None, None); s.set_track_genre(sid, "Techno")
    dkey = identity_key("NewTrack", "NewArtist")
    s.upsert_discovered_track(dkey, "vD", "NewTrack", "NewArtist", "Alb", "th",
                              "Techno", "2020", "bid", 1.0)
    embed.build_content_and_store(s)
    _, CV, _ = embed.load_content_vectors(s)
    _, DV, _ = embed.load_discovered_content_vectors(s)
    assert CV is not None and DV is not None
    assert DV.shape[1] == CV.shape[1]      # same dimensionality ⇒ cosines are comparable
