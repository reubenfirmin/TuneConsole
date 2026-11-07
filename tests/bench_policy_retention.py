"""Opt-in real-default policy storage budget acceptance.

uv run pytest tests/bench_policy_retention.py -s
"""
from tests.rec.test_policy_storage import check_preparation_retention
from yt_playlist.core.store import Store


def test_repeated_preparation_default_retention_has_bounded_bytes(tmp_path, monkeypatch):
    # Preserve the real 80-generation / 1000-variant bound independently of the
    # compact normal-suite transition. Unshared payloads would exceed 3.8 GB.
    # Private readers of an in-memory Store clone the retained policy database.
    # Use the application's actual file-backed reader/publication path.
    store = Store(tmp_path / 'policies.db')
    try:
        store.init_schema()
        check_preparation_retention(store, monkeypatch, generations=80, variants=1000,
            evidence_bytes=48000, maximum_generations=32, byte_bound=7 * 1024 * 1024)
    finally:
        store.conn.close()
