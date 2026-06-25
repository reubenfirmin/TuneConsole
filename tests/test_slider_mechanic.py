# tests/test_slider_mechanic.py
import time
from yt_playlist.core.store import Store
from yt_playlist.rec import recommend, rec_params, genre_map

DAY = 86400.0


def _store():
    s = Store(":memory:")
    s.init_schema()
    tid = s.upsert_track("v", "s", "band", None, None)
    s.set_track_genre(tid, "Techno")
    return s, "genre:" + genre_map.family("Techno")


def test_exposure_graduates_once_per_held_day():
    s, axis = _store()
    s.set_lean(axis, 2.0, 0.0)                       # full lean magnitude 1.0
    # day 0: +1.0*0.5 = 0.5 ledger, below 1.2 threshold -> no permanent nudge yet
    recommend.graduate_slider_exposure(s, 0.0)
    assert axis not in s.get_weights()
    assert abs(s.get_theme(axis) - rec_params.SOURCE_W_SLIDER * 1.0) < 1e-9
    # same day again: no double-count
    recommend.graduate_slider_exposure(s, 100.0)
    assert abs(s.get_theme(axis) - rec_params.SOURCE_W_SLIDER * 1.0) < 1e-9


def test_exposure_migrates_conserving_effective_multiplier():
    s, axis = _store()
    s.set_lean(axis, 2.0, 0.0)
    before_eff = s.get_weights().get(axis, 1.0) * s.get_lean(axis)   # 1.0 * 2.0 = 2.0
    # three held days cross threshold (3 * 0.5 = 1.5 >= 1.2)
    for d in range(3):
        recommend.graduate_slider_exposure(s, d * DAY)
    after_eff = s.get_weights().get(axis, 1.0) * s.get_lean(axis)
    assert 1.0 < s.get_weights()[axis] < rec_params.GRADUATE_UP      # one graduation, ~1.0475 due to shrinkage
    assert abs(after_eff - before_eff) < 1e-9                        # displayed bar conserved


def test_neutral_lean_stops_accrual():
    s, axis = _store()
    s.set_lean(axis, 1.0, 0.0)                       # neutral -> magnitude 0
    recommend.graduate_slider_exposure(s, 0.0)
    assert s.get_theme(axis) in (None, 0.0)
