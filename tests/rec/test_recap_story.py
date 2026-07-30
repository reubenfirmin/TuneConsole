"""#79 Monthly recap story: beat builders are honest (fire/omit correctly), personality axes degrade
gracefully without acoustic data, and build_story assembles a coherent reel."""
from yt_playlist.rec import recap_story as rs


# ── beat honesty ───────────────────────────────────────────────────────────────────────────────
def test_all_in_only_when_an_artist_dominates():
    # 3% of plays is NOT going all in -> omitted; 30% is.
    thin = {"plays": 100, "top_artists": [{"artist": "A", "plays": 3}]}
    assert rs._beat_all_in(thin) is None
    fat = {"plays": 100, "top_artists": [{"artist": "A", "plays": 30}]}
    b = rs._beat_all_in(fat)
    assert b and b["artist"] == "A" and b["share"] == 0.30


def test_wide_net_is_the_honest_counterpart():
    # No dominant artist + many artists -> wide_net fires; a dominant artist -> it steps aside.
    spread = {"plays": 535, "top_artists": [{"artist": "A", "plays": 14}]}
    row = {"distinct_artists": 300}
    b = rs._beat_wide_net(spread, row)
    assert b and b["artists"] == 300 and b["plays"] == 535
    concentrated = {"plays": 100, "top_artists": [{"artist": "A", "plays": 40}]}
    assert rs._beat_wide_net(concentrated, {"distinct_artists": 300}) is None
    assert rs._beat_wide_net(spread, {"distinct_artists": 5}) is None   # not actually wide


def test_new_obsession_needs_real_repetition():
    assert rs._beat_new_obsession({"top_new_artist": {"artist": "X", "plays": 2}}) is None
    b = rs._beat_new_obsession({"top_new_artist": {"artist": "X", "plays": 9}})
    assert b and b["artist"] == "X" and b["plays"] == 9


def test_diversity_direction_and_deadband():
    months = [{"month": "1970-01", "diversity": 0.60}, {"month": "1970-02", "diversity": 0.68}]
    b = rs._beat_diversity(months, "1970-02")
    assert b and b["direction"] == "branched out" and b["delta"] == 0.08
    flat = [{"month": "1970-01", "diversity": 0.60}, {"month": "1970-02", "diversity": 0.61}]
    assert rs._beat_diversity(flat, "1970-02") is None            # <0.03 change -> omitted
    assert rs._beat_diversity(months, "1970-01") is None          # no prior month


# ── personality axes ───────────────────────────────────────────────────────────────────────────
def test_energy_axis_falls_back_to_genre_when_no_acoustic():
    # No acoustic rows at all: energy comes purely from the genre prior (techno is high-energy).
    val, src = rs.energy_axis({"k": 10}, audio={}, meta={"k": ("A", "techno")})
    assert src == "genre" and val > 0.7
    # ambient with no acoustic -> low
    val2, _ = rs.energy_axis({"k": 10}, audio={}, meta={"k": ("A", "ambient")})
    assert val2 < 0.3


def test_energy_axis_uses_acoustic_when_present():
    audio = {"k": {"energy": 0.9, "bpm": 140, "dance": 0.8}}
    val, src = rs.energy_axis({"k": 20}, audio, meta={"k": ("A", "ambient")})
    assert val > 0.7   # acoustic 0.9 dominates the ambient prior once coverage is full
    assert src in ("acoustic", "genre")


def test_rhythm_axis_counts_the_night_window():
    day = [0] * 24
    day[13] = 10
    assert rs.rhythm_axis(day) == 0.0
    night = [0] * 24
    night[23] = 5
    night[2] = 5
    assert rs.rhythm_axis(night) == 1.0


def test_exploration_axis_blends_discovery_and_diversity():
    loyal = {"plays": 100, "new_artist_plays": 0, "diversity": 0.35}
    explorer = {"plays": 100, "new_artist_plays": 30, "diversity": 0.75}
    assert rs.exploration_axis(loyal) < 0.2
    assert rs.exploration_axis(explorer) > 0.8


def test_archetype_matrix_is_total():
    # every (energy, explore, night) corner has a name -> personality never crashes
    for e in (True, False):
        for x in (True, False):
            for n in (True, False):
                assert (e, x, n) in rs._ARCHETYPES


# ── assembly ────────────────────────────────────────────────────────────────────────────────────
class _FakeTrends:
    def ledger_track_plays(self, since, until): return {"k1": 10, "k2": 4}
    def track_audio(self): return {"k1": {"energy": 0.5, "bpm": 120, "dance": 0.7}}
    def play_hours(self, since, until): h = [0] * 24; h[22] = 8; h[14] = 6; return h
    def track_meta(self): return {"k1": ("A1", "house"), "k2": ("A2", "ambient")}


class _FakeStore:
    trends = _FakeTrends()


def test_build_story_assembles_cover_personality_and_closing():
    review = {"month": "2026-06", "plays": 535, "listen_days": 19, "longest_streak": 14,
              "top_artists": [{"artist": "A", "plays": 14}], "top_new_artist": None,
              "binge": {"day": 20625, "plays": 109, "pct": 0.07, "artist": "DJ"}}
    months = [{"month": "2026-05", "diversity": 0.67, "distinct_artists": 200},
              {"month": "2026-06", "diversity": 0.60, "distinct_artists": 300, "plays": 535,
               "new_artist_plays": 40}]
    story = rs.build_story(_FakeStore(), months, review, insights=[], month_name="June")
    kinds = [b["kind"] for b in story["beats"]]
    assert kinds[0] == "cover" and kinds[-1] == "closing" and "personality" in kinds
    assert "wide_net" in kinds and "all_in" not in kinds          # 14/535 -> wide net, not all in
    cover = story["beats"][0]
    assert cover["thesis"] == "The month you cast a wide net."
    assert story["month_name"] == "June"


def test_build_story_none_without_review():
    assert rs.build_story(_FakeStore(), [], None, [], None) is None
