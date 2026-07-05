import pytest
from yt_playlist.rec import trend_rollups as tr
from yt_playlist.util import genre_map


@pytest.fixture(autouse=True)
def toy_genres(monkeypatch):
    # Identity family map + a controlled distance table so diversity is hand-checkable.
    monkeypatch.setattr(genre_map, "family", lambda g: (g or "").lower())
    dist = {("house", "techno"): 0.3, ("house", "ambient"): 0.8, ("techno", "ambient"): 0.9}
    def d(a, b):
        if a == b:
            return 0.0
        return dist.get((a, b)) or dist.get((b, a)) or 1.0
    monkeypatch.setattr(genre_map, "family_distance", d)


def test_week_and_month_bucketing():
    assert tr.week_start(2) == 0 and tr.week_start(5) == 0 and tr.week_start(9) == 7
    # day 0 = 1970-01-01 UTC
    assert tr.month_of(0) == "1970-01"


def test_diversity_index():
    # shares house 0.5, techno 0.5: D = 2 * 0.5 * 0.5 * 0.3 = 0.15
    assert tr.diversity_index({"house": 1, "techno": 1}) == pytest.approx(0.15)
    # shares 0.5 / 0.3 / 0.2: house-techno 2*.5*.3*.3=.09 ; house-ambient 2*.5*.2*.8=.16 ;
    #                         techno-ambient 2*.3*.2*.9=.108 ; sum = 0.358
    assert tr.diversity_index({"house": 5, "techno": 3, "ambient": 2}) == pytest.approx(0.358)
    assert tr.diversity_index({"house": 4}) == 0.0            # single family -> 0


def test_longest_streak():
    assert tr.longest_streak({0, 1, 2, 4, 5}) == 3           # run 0,1,2
    assert tr.longest_streak({10, 11, 12, 13}) == 4
    assert tr.longest_streak(set()) == 0


def test_current_streak():
    # Cross-month run in day-number terms: days 27,28,29,30,31,32,33 are 7 unbroken consecutive days
    # (whatever month boundary might fall inside that range is irrelevant to this pure function -- the
    # point is detector 5 needs to see 7, not a month-split 4+3; see the detector-level test in
    # test_trend_rollups_build.py for that wiring).
    assert tr.current_streak({27, 28, 29, 30, 31, 32, 33}) == 7
    # A gap breaks the streak: max is 5; 5,4 present but 3 missing -> run stops at {4,5} -> length 2.
    assert tr.current_streak({1, 2, 4, 5}) == 2
    # Empty input -> 0.
    assert tr.current_streak(set()) == 0
    # The run must end AT the max day: {10,11,12,20} -> max=20, 19 not in set -> run length 1 (20 alone),
    # even though a longer run (10,11,12 -> 3) exists earlier -- that's longest_streak's job, not this one.
    assert tr.current_streak({10, 11, 12, 20}) == 1


def test_discovery_spike():
    # prev8 = [1,1,2,1,1,2,1,1] -> median 1.0 ; latest 6 >= 2*1.0 and >= 5 -> spike
    assert tr.discovery_spike([1, 1, 2, 1, 1, 2, 1, 1, 6]) is True
    # prev8 median 3 -> 2*3=6 ; latest 5 < 6 -> no spike (bigger, but not 2x)
    assert tr.discovery_spike([3, 3, 3, 3, 3, 3, 3, 3, 5]) is False
    # burst from silence: median 0 -> ratio trivially met, absolute floor 5 gates it -> spike
    assert tr.discovery_spike([0, 0, 0, 0, 0, 0, 0, 0, 5]) is True
    # below the absolute floor never trips
    assert tr.discovery_spike([0, 0, 0, 0, 0, 0, 0, 0, 4]) is False


def test_compute_weeks_new_artist_and_diversity():
    # snapshots: day2 {k1,k3}, day5 {k1,k2}, day9 {k3,k4}. Counts:
    day_counts = [(2, "k1", 1), (2, "k3", 1), (5, "k1", 1), (5, "k2", 1), (9, "k3", 1), (9, "k4", 1)]
    meta = {"k1": ("A1", "house"), "k2": ("A1", "house"),
            "k3": ("A2", "techno"), "k4": ("A3", "ambient")}
    artist_first = {"A1": 1, "A2": 2, "A3": 8}       # A3 first seen day 8 -> week 7
    track_first = {"k1": 1, "k2": 5, "k3": 2, "k4": 8}
    weeks = tr.compute_weeks(day_counts, meta, artist_first, track_first)
    w0, w1 = weeks[0], weeks[1]
    assert w0["week_start_day"] == 0 and w1["week_start_day"] == 7
    # Week 0 (days 2,5): 4 plays; A1,A2 both first-seen in week 0 -> all 4 are new-artist plays.
    assert w0["plays"] == 4 and w0["new_artist_plays"] == 4 and w0["distinct_artists"] == 2
    # families: house k1(2)+k2(1)=3, techno k3(1)=1 -> shares 0.75/0.25 -> 2*0.75*0.25*0.3 = 0.1125
    assert w0["families"] == {"house": 3, "techno": 1}
    assert w0["diversity"] == pytest.approx(0.1125)
    # Week 1 (day 9): 2 plays; A2 first-seen week 0 (not new), A3 first-seen week 7 -> 1 new-artist play.
    assert w1["plays"] == 2 and w1["new_artist_plays"] == 1
    # families techno 1, ambient 1 -> 2*0.5*0.5*0.9 = 0.45
    assert w1["diversity"] == pytest.approx(0.45)


def test_compute_weeks_new_track_plays():
    # Single artist A1, two tracks. k1 is "old" (first-seen week 0); k2 is "new" (first-seen week 7),
    # even though k2 already has plays in week 0 here -- compute_weeks is a pure fold over whatever
    # track_first it's handed, so this isolates new_track_plays from new_artist_plays cleanly.
    day_counts = [(2, "k1", 3), (2, "k2", 2), (9, "k1", 1)]
    meta = {"k1": ("A1", "house"), "k2": ("A1", "house")}
    artist_first = {"A1": 1}                          # week 0 -> every A1 play is a new-artist play
    track_first = {"k1": 1, "k2": 9}                  # k1 -> week 0, k2 -> week 7
    weeks = tr.compute_weeks(day_counts, meta, artist_first, track_first)
    w0, w1 = weeks[0], weeks[1]
    assert w0["week_start_day"] == 0 and w1["week_start_day"] == 7
    # Week 0 (day 2): k1=3 + k2=2 = 5 plays, all new-artist (A1 first-seen week 0).
    assert w0["plays"] == 5 and w0["new_artist_plays"] == 5
    # But only k1's plays are new-track (k1's track_first is week 0; k2's is week 7) -> 3, not 5.
    assert w0["new_track_plays"] == 3
    # Week 1 (day 9): k1=1 play; A1 isn't new here (new_artist_plays=0) and neither is k1's track
    # (k1's track_first is week 0, not week 7) -> new_track_plays=0 even though k2's track_first
    # falls in week 7, because k2 has no plays in week 7.
    assert w1["plays"] == 1 and w1["new_artist_plays"] == 0 and w1["new_track_plays"] == 0


def test_compute_months_bucketing_streak_and_new_artist():
    # Jan 1970 (days 0-30) vs Feb 1970 (day 31+): day 31 = 1970-02-01 (Jan has 31 days, day 30 = Jan 31).
    # Jan: k1 plays days 2,3,9 (3 plays); k3 plays days 2,4 (2 plays) -> 5 plays, artists A1,A2.
    # Feb: k4 plays day 31 (1 play, A3); k3 plays day 35 (1 play, A2, NOT new -- A2 first seen Jan).
    day_counts = [(2, "k1", 1), (2, "k3", 1), (3, "k1", 1), (4, "k3", 1), (9, "k1", 1),
                  (31, "k4", 1), (35, "k3", 1)]
    meta = {"k1": ("A1", "house"), "k3": ("A2", "techno"), "k4": ("A3", "ambient")}
    artist_first = {"A1": 1, "A2": 2, "A3": 31}        # A1, A2 first-seen Jan; A3 first-seen Feb
    months = tr.compute_months(day_counts, meta, artist_first)
    assert [m["month"] for m in months] == ["1970-01", "1970-02"]
    jan, feb = months

    # Jan: k1=3 (days 2,3,9), k3=2 (days 2,4) -> 5 plays; both A1 and A2 first-seen Jan -> all new.
    assert jan["plays"] == 5 and jan["distinct_artists"] == 2 and jan["new_artist_plays"] == 5
    assert jan["families"] == {"house": 3, "techno": 2}
    # shares 0.6/0.4, house-techno distance 0.3 -> 2*0.6*0.4*0.3 = 0.144
    assert jan["diversity"] == pytest.approx(0.144)
    # listen_days: distinct days {2,3,9} for k1 plus {2,4} for k3 -> {2,3,4,9} = 4 days.
    assert jan["listen_days"] == 4
    # longest_streak wiring: {2,3,4,9} -> the 2,3,4 run is 3 long (9 is isolated) -> longest_streak==3,
    # i.e. it's really longest_streak(mo["days"]) and not e.g. len(days) (which would be 4).
    assert jan["longest_streak"] == 3

    # Feb: k4=1 (day31, A3) + k3=1 (day35, A2) -> 2 plays, 2 distinct artists (A3, A2).
    assert feb["plays"] == 2 and feb["distinct_artists"] == 2
    # Only A3's play is new (first-seen Feb); A2's Feb play doesn't count (A2 first-seen Jan) ->
    # new_artist_plays == 1, not 2 -- this is the new-artist-by-month wiring under test.
    assert feb["new_artist_plays"] == 1
    assert feb["families"] == {"ambient": 1, "techno": 1}
    # shares 0.5/0.5, techno-ambient distance 0.9 -> 2*0.5*0.5*0.9 = 0.45
    assert feb["diversity"] == pytest.approx(0.45)
    # listen_days {31, 35} = 2, non-consecutive -> longest_streak == 1 (not 2).
    assert feb["listen_days"] == 2 and feb["longest_streak"] == 1
