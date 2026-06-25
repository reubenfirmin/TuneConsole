# tests/test_ban_manager.py
from yt_playlist.core.store import Store
from yt_playlist.rec import rec_params
from yt_playlist.rec.recommend import apply_dislikes


def _store():
    s = Store(":memory:"); s.init_schema(); return s


def test_list_dislikes_and_clear():
    s = _store()
    s.record_dislike("a|x", until=2000.0, now=1000.0)
    s.record_dislike("b|y", until=2000.0, now=1001.0)
    rows = s.list_dislikes()
    keys = [r["item_key"] for r in rows]
    assert keys == ["b|y", "a|x"]                 # newest first
    s.clear_dislike("b|y")
    assert [r["item_key"] for r in s.list_dislikes()] == ["a|x"]


def test_ban_duration_param_respected():
    s = _store()
    rec_params.set_param(s, "dislike_suppress_days", 10)
    apply_dislikes(s, {"artist|track": "DISLIKE"}, now=1000.0)
    rows = s.list_dislikes()
    assert len(rows) == 1
    assert rows[0]["until"] == 1000.0 + 10 * 86400
