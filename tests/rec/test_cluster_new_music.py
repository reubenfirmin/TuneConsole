import numpy as np

from yt_playlist.rec import embed, embedding_vectors as vectors
from tests import model_support
from yt_playlist.util.matching import identity_key


def test_include_new_surfaces_discovered_tracks_in_the_shared_model_space(store):
    s = store
    # two library tracks (techno seed + ambient other), each with a collaborative vector
    sid = s.tracks.upsert_track("vS", "Seed", "ArtistS", None, None); s.tracks.set_track_genre(sid, "Techno")
    oid = s.tracks.upsert_track("vO", "Other", "ArtistO", None, None); s.tracks.set_track_genre(oid, "Ambient")
    skey, okey = identity_key("Seed", "ArtistS"), identity_key("Other", "ArtistO")
    s.rec_model.replace_rec_vectors([(skey, np.array([1, 0, 0, 0], dtype=np.float32).tobytes()),
                           (okey, np.array([0, 1, 0, 0], dtype=np.float32).tobytes())])
    # an OUT-OF-CORPUS techno track (not in the library)
    dkey = identity_key("NewTrack", "NewArtist")
    s.discovery.upsert_discovered_track(dkey, "vD", "NewTrack", "NewArtist", "Alb", "th",
                              "Techno", "2020", "bid", 1.0)
    model_support.build_content_model(s)  # builds library vectors + model + discovered vectors

    _, CV, _ = vectors.load_content_vectors(s)
    _, DV, _ = vectors.load_discovered_content_vectors(s)
    assert CV is not None and DV is not None
    assert DV.shape[1] == CV.shape[1]

    new = [k for k, _ in embed.cluster_expand(s, pos_keys=[skey], topn=10, include_new=True)]
    base = [k for k, _ in embed.cluster_expand(s, pos_keys=[skey], topn=10, include_new=False)]
    assert dkey in new          # the techno out-of-corpus track is reachable from the techno seed
    assert dkey not in base     # ...only when opted in
