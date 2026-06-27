"""#50/#53: the rec worker's Fresh proposal is the taste-scored cold pool ONLY (pool-only, no radio
fallback). Every item carries a key; an empty pool yields an empty proposal."""
from types import SimpleNamespace

from yt_playlist.rec import surfaces
from yt_playlist.rec.rec_worker import RecWorker
from yt_playlist.rec.surfaces import ForYouItem


def _worker(store):
    return RecWorker(SimpleNamespace(store=store))


def test_uses_cold_pool(monkeypatch, store):
    item = ForYouItem("T", "A", "", "v1", None, 0, "New, fits your taste", "k1", lane="cold")
    monkeypatch.setattr(surfaces, "cold_candidates", lambda s, now, **k: [item])
    out = _worker(store)._fresh_proposal(0.0)
    assert out == [{"video_id": "v1", "title": "T", "artist": "A", "thumbnail": None,
                    "key": "k1", "reason": "New, fits your taste", "lane": "cold"}]


def test_empty_pool_yields_empty_proposal_no_radio_fallback(monkeypatch, store):
    monkeypatch.setattr(surfaces, "cold_candidates", lambda s, now, **k: [])
    assert _worker(store)._fresh_proposal(0.0) == []
