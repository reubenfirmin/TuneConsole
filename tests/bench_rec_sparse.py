"""Explicit performance check (not part of default collection).

Run serially under the normal lock:
uv run pytest --test-lock-wait=60 -s tests/bench_rec_sparse.py
Each size runs in a fresh subprocess so peak RSS is independent of preceding sizes.
"""
import json
from pathlib import Path
import subprocess
import sys

import pytest


def measure(n, dim=48):
    import resource
    import time
    from types import SimpleNamespace

    import numpy as np
    from yt_playlist.core.numerics import limit_native_threads
    from yt_playlist.rec import embedding_training as embed, ppr

    limit_native_threads()
    # Album-sized groups, overlapping playlists, and shorter listening sessions;
    # fixed seeds and repeated evidence exercise accumulation, not just disjoint cliques.
    rng = np.random.default_rng(115)
    keys = [f"track-{i:05d}" for i in range(n)]
    baskets = [keys[start:start + 12] for start in range(0, n, 10)]
    for _ in range(n // 10):
        anchor = int(rng.integers(0, n))
        local = [(anchor + int(x)) % n for x in rng.choice(120, size=24, replace=False)]
        baskets.append([keys[i] for i in local])
    baseline = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    scale = 1 if sys.platform == "darwin" else 1024
    started = time.monotonic()
    actual_keys, vectors = embed.build_vectors_from_baskets(baskets, dim=dim)
    embedded = time.monotonic()
    store = SimpleNamespace(rec_evidence=SimpleNamespace(rec_baskets=lambda: baskets))
    _, transition, _ = ppr.build_transition(store)
    for mode in range(6):
        result = ppr.ppr_rank(transition, range(mode, n, 6), iters=50, tol=1e-6)
        assert abs(result.sum() - 1) < 1e-8
    finished = time.monotonic()
    assert actual_keys == keys and vectors.shape == (n, dim)
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * scale
    return {"tracks": n, "dim": dim, "baskets": len(baskets), "embedding_s": round(embedded - started, 3),
            "ppr_six_modes_s": round(finished - embedded, 3),
            "total_s": round(finished - started, 3), "peak_rss_mib": round(peak / 2**20, 2),
            "incremental_peak_mib": round((peak - baseline * scale) / 2**20, 2),
            "dense_matrix_mib": round(n * n * 8 / 2**20, 2)}


@pytest.mark.parametrize("n,dim", [(5000, 48), (10000, 128), (20000, 128)])
def test_sparse_rebuild_peak_memory_and_time(n, dim):
    child = subprocess.run([sys.executable, str(Path(__file__).resolve()), str(n), str(dim)],
                           capture_output=True, text=True, timeout=120, check=True)
    result = json.loads(child.stdout)
    print(json.dumps(result), flush=True)
    # A generous linear budget includes LAPACK workspaces and allocator overhead,
    # yet rejects even one full dense vocabulary-sized float64 allocation.
    assert result["incremental_peak_mib"] < 128 + n * 8192 / 2**20


if __name__ == "__main__":
    print(json.dumps(measure(int(sys.argv[1]), int(sys.argv[2]) if len(sys.argv) > 2 else 48)))
