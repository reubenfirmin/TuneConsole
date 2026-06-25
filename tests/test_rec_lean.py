# tests/test_rec_lean.py
from yt_playlist.core.store import Store


def _store():
    s = Store(":memory:")
    s.init_schema()
    return s


def test_set_and_get_lean():
    s = _store()
    s.set_lean("genre:Techno", 1.4, 1000.0)
    assert s.get_lean("genre:Techno") == 1.4
    assert s.get_lean("genre:House") == 1.0          # default neutral
    assert s.get_leans() == {"genre:Techno": 1.4}


def test_lean_rows_and_graduated_day():
    s = _store()
    s.set_lean("genre:Techno", 1.4, 1000.0)
    s.set_lean_graduated_day("genre:Techno", "2026-06-25")
    row = {r["axis"]: r for r in s.lean_rows()}["genre:Techno"]
    assert row["value"] == 1.4
    assert row["updated_at"] == 1000.0
    assert row["last_graduated_day"] == "2026-06-25"


def test_clear_lean():
    s = _store()
    s.set_lean("genre:Techno", 1.4, 1000.0)
    s.clear_lean("genre:Techno")
    assert s.get_leans() == {}


def test_lean_reachable_via_flat_store_proxy():
    s = _store()
    s.set_lean("genre:Techno", 1.2, 1.0)   # flat proxy path (what routes use)
    assert s.get_lean("genre:Techno") == 1.2
