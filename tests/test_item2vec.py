import numpy as np
import pytest

from yt_playlist.rec import embed, embedding_vectors as vectors
from tests import model_support


@pytest.mark.parametrize('method', ['svd', 'item2vec'])
def test_fit_is_invariant_to_basket_and_member_order(method):
    from yt_playlist.rec import embedding_training as training
    baskets = [[f'key-{(start + offset) % 20}' for offset in range(7)] for start in range(20)]
    baskets += [baskets[0]]  # Repeated baskets still carry repeated evidence.
    reordered = [list(reversed(basket)) + [basket[0]] for basket in reversed(baskets)]
    first = training.build_vectors_from_baskets(baskets, dim=4, method=method)
    second = training.build_vectors_from_baskets(reordered, dim=4, method=method)
    assert first.keys == second.keys
    np.testing.assert_array_equal(first.matrix, second.matrix)


def test_item2vec_is_invariant_to_python_hash_seed():
    import os
    import subprocess
    import sys

    code = '''
import hashlib
from yt_playlist.rec.embedding_training import build_vectors_from_baskets
baskets = [list({f'key-{(start + offset) % 20}' for offset in range(7)}) for start in range(20)]
fit = build_vectors_from_baskets(baskets, dim=4, method='item2vec')
print(hashlib.sha256(fit.matrix.tobytes()).hexdigest())
'''
    hashes = [subprocess.check_output([sys.executable, '-c', code],
              env={**os.environ, 'PYTHONHASHSEED': seed}, text=True).strip() for seed in ('1', '42')]
    assert hashes[0] == hashes[1]


def test_item2vec_builds_unit_vectors(store):
    iid = store.identities.upsert_identity("main", "cred", None, True)
    A = [store.tracks.upsert_track(f"a{i}", f"A{i}", "AB", None, None) for i in range(8)]
    B = [store.tracks.upsert_track(f"b{i}", f"B{i}", "BB", None, None) for i in range(8)]
    store.playlists.set_playlist_tracks(store.playlists.upsert_playlist(iid, "PA", "PA", 8, "h", 0.0), A)
    store.playlists.set_playlist_tracks(store.playlists.upsert_playlist(iid, "PB", "PB", 8, "h2", 0.0), B)
    store.settings.set_setting("rec_embed_method", "item2vec")

    assert model_support.build_track_model(store, dim=8) == 16
    keys, V, idx = vectors.load_vectors(store)
    assert V.shape == (16, 8)
    assert np.allclose(np.linalg.norm(V, axis=1), 1.0, atol=1e-3)   # L2-normalized
    assert embed.neighbors(store, "a0|ab", topn=3)                  # produces neighbours
    # quality is measured by recall@k in autotune, not asserted here (noisy on tiny data)


def test_method_setting_switches_builder(store):
    iid = store.identities.upsert_identity("main", "cred", None, True)
    A = [store.tracks.upsert_track(f"a{i}", f"A{i}", "AB", None, None) for i in range(14)]
    store.playlists.set_playlist_tracks(store.playlists.upsert_playlist(iid, "PA", "PA", 14, "h", 0.0), A)
    store.settings.set_setting("rec_embed_method", "svd")
    assert model_support.build_track_model(store, dim=8) == 14
    store.settings.set_setting("rec_embed_method", "item2vec")
    assert model_support.build_track_model(store, dim=8) == 14  # both methods build cleanly
