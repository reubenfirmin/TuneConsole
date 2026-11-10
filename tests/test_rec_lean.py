"""Repository lean lifecycle: defaults, canonical keys, graduation and clearing."""


def test_lean_setting_graduation_and_clear_share_one_lifecycle(store):
    s = store
    s.rec_model.set_lean("genre:Techno", 1.4, 1000.0)
    assert s.rec_model.get_lean("genre:Techno") == 1.4
    assert s.rec_model.get_lean("genre:House") == 1.0          # default neutral
    assert s.rec_model.get_leans() == {"genre:techno": 1.4}
    s.rec_model.set_lean_graduated_day("genre:Techno", "2026-06-25")
    row = {r["axis"]: r for r in s.rec_model.lean_rows()}["genre:techno"]
    assert row["value"] == 1.4
    assert row["updated_at"] == 1000.0
    assert row["last_graduated_day"] == "2026-06-25"
    s.rec_model.clear_lean("genre:Techno")
    assert s.rec_model.get_leans() == {}
    assert s.rec_model.get_lean('genre:Techno') == 1.0
    assert s.rec_model.lean_rows() == []
