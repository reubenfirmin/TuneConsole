# tests/test_ban_manager.py
from yt_playlist.core.store import Store
from tests.retained_rating_support import apply_model_ratings, observe_rating
from yt_playlist.rec import rec_params


def _store():
    s = Store(":memory:"); s.init_schema(); return s


def test_list_dislikes_and_clear():
    s = _store()
    observe_rating(s, 'a|x', 'DISLIKE', 1000.0)
    observe_rating(s, 'b|y', 'DISLIKE', 1001.0)
    rows = s.rec_model.list_dislikes()
    keys = [r["item_key"] for r in rows]
    assert keys == ["b|y", "a|x"]                 # newest first
    s.rec_model.unban('b|y', 1002.0)
    assert [r["item_key"] for r in s.rec_model.list_dislikes()] == ["a|x"]


def test_ban_duration_param_respected():
    s = _store()
    rec_params.set_param(s, "dislike_suppress_days", 10)
    apply_model_ratings(s, {"artist|track": "DISLIKE"}, now=1000.0)
    rows = s.rec_model.list_dislikes()
    assert len(rows) == 1
    assert rows[0]["until"] == 1000.0 + 10 * 86400
