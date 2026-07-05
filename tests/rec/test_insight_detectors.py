from yt_playlist.rec import trend_rollups as tr


def test_latest_full_week_excludes_in_progress():
    # weeks 0,7,14. now_day 21: 14+7=21<=21 -> full, latest 14. now_day 20: 14+7=21>20 -> latest 7.
    assert tr._latest_full_week_start([0, 7, 14], 21) == 14
    assert tr._latest_full_week_start([0, 7, 14], 20) == 7


def test_emergence_fires_and_censors():
    # Bicep first_day 100 (> floor 50 + 7 -> uncensored), now_day 126 (26d old <= 42).
    # weeks: 98 -> 1 play, 119 -> 12 plays. latest full week among {98,119} with w+7<=126: 119.
    # 12 >= EMERGENCE_MIN_LATEST(8) and 12 >= 3.0 * 1 -> fires. weeks_ago = (119-98)//7 = 3.
    aw = {"Bicep": {98: 1, 119: 12}}
    got = tr.detect_emergence(aw, {"Bicep": 100}, floor_day=50, now_day=126)
    assert len(got) == 1 and got[0]["event_day"] == 119
    assert got[0]["magnitude"] == 12 / 8 and "3 weeks ago" in got[0]["detail"]
    # censored: first_day 55 <= floor 50 + 7 -> nothing, even with the same ramp.
    assert tr.detect_emergence(aw, {"Bicep": 55}, floor_day=50, now_day=126) == []


def test_emergence_singular_week_grammar():
    """L1: 'N weeks ago' must read '1 week ago' (not '1 weeks ago') when the ramp is a single week
    old. weeks 112 -> 1 play, 119 -> 12 plays: wk_ago = (119-112)//7 = 1."""
    aw = {"Bicep": {112: 1, 119: 12}}
    got = tr.detect_emergence(aw, {"Bicep": 100}, floor_day=50, now_day=126)
    assert len(got) == 1
    assert "1 week ago" in got[0]["detail"]
    assert "1 weeks ago" not in got[0]["detail"]


def test_put_to_bed():
    # Jan 10 + Feb 9 plays (both >= 8, consecutive) -> 2-month run. last play day 58, now 110.
    # silence 110-58 = 52 >= 42 -> fires. peak 10, weeks silent 52//7 = 7. event 58+42 = 100.
    # magnitude stays PEAK-based (10/8); the "N+ plays a month" COPY claim must cite the run's MINIMUM
    # (9), since that's the only figure both cited months actually cleared.
    got = tr.detect_put_to_bed({"Bed": {"1970-01": 10, "1970-02": 9}},
                               {"Bed": {20: 5, 58: 1}}, now_day=110)
    assert len(got) == 1 and got[0]["magnitude"] == 10 / 8 and got[0]["event_day"] == 100
    assert "7 weeks" in got[0]["detail"]
    assert "9+ plays a month" in got[0]["detail"]
    # one sustained month only -> no fire
    assert tr.detect_put_to_bed({"Bed": {"1970-01": 10}}, {"Bed": {20: 1}}, 110) == []


def test_put_to_bed_detail_cites_the_runs_minimum_not_its_peak():
    """H1 (honesty bug): a qualifying run of (Jan: 10, Feb: 8) must render '8+ plays a month', not
    '10+ plays a month' -- the peak (10) is true for January but FALSE for February, so citing it as a
    per-month floor overstates what every cited month actually cleared. min() is always safe here since
    every month in the run is, by construction, >= PTB_MONTHLY_MIN(8)."""
    got = tr.detect_put_to_bed({"Bed": {"1970-01": 10, "1970-02": 8}},
                               {"Bed": {20: 5, 58: 1}}, now_day=110)
    assert len(got) == 1
    assert got[0]["detail"].startswith("8+ plays a month")
    assert "10+ plays a month" not in got[0]["detail"]
    # magnitude is still peak-based (10/8 = 1.25): the run's true "how big" figure, not softened to the
    # minimum -- only the per-month COPY CLAIM needed the min, not the ranking signal.
    assert got[0]["magnitude"] == 10 / 8


def test_put_to_bed_cites_run_immediately_before_silence_not_longest_ever():
    """PUT TO BED's copy says '...then quiet': a temporal-adjacency claim. The cited run must therefore
    be the consecutive-months run immediately preceding the silence, not whichever run is historically
    longest. Day numbering (day 0 = 1970-01-01, a non-leap year): Jan 1-31 -> days 0-30, Feb 1-28 ->
    31-58, Mar 1-31 -> 59-89, Apr 1-30 -> 90-119, May 1-31 -> 120-150, Jun 1-30 -> 151-180,
    Jul 1-31 -> 181-211 (verified via datetime.fromtimestamp(day*86400, tz=utc))."""
    # Run A (early, historically LONGEST): Jan-Apr, 10/9/8/9 plays, all >= PTB_MONTHLY_MIN(8) and
    # consecutive -> a 4-month run.
    # Break: May has only 3 plays (< 8) -> resets the run.
    # Run B (recent, the one that actually precedes the silence): Jun-Jul, 8/8 plays -> a 2-month run,
    # exactly meeting PTB_MONTHS(2). No months exist after Jul, i.e. Run B is what the artist was doing
    # right before going quiet.
    months = {"1970-01": 10, "1970-02": 9, "1970-03": 8, "1970-04": 9,
              "1970-05": 3,
              "1970-06": 8, "1970-07": 8}
    # last_play day 200 (1970-07-20, within Jul's 181-211) -> now_day 250: silence 250-200=50 >= 42.
    ad = {"Bed2": {10: 2, 200: 4}}
    got = tr.detect_put_to_bed({"Bed2": months}, ad, now_day=250)
    assert len(got) == 1
    # Fixed behavior: cites Run B (Jun-Jul). peak = max(8, 8) = 8 -> magnitude 8/8 = 1.0. Evidence must
    # read "2 months", not "4 months".
    assert got[0]["magnitude"] == 8 / 8
    assert got[0]["evidence"][0] == {"k": "sustained", "v": "2 months"}
    # wks silent = 50 // 7 = 7 (int floor division); event_day = last_play + PTB_SILENCE_DAYS = 200+42=242.
    assert got[0]["event_day"] == 242 and "7 weeks" in got[0]["detail"]
    # Old (buggy) behavior tracked whichever run was longest EVER SEEN: Jan-Apr (len 4 > Jun-Jul's len 2),
    # peak 10 -> magnitude 10/8 = 1.25 and evidence "4 months". That run ended in April, 3 months before
    # the 2-month Jun-Jul run even started, so "then quiet" citing it would misleadingly imply the
    # 4-month run was what had just ended when the artist actually kept going (below-threshold, then
    # sustained again) for 3 more months after it. The fixed code must NOT reproduce that.
    assert got[0]["magnitude"] != 10 / 8
    assert got[0]["evidence"][0] != {"k": "sustained", "v": "4 months"}


def test_repetition_needs_strictly_shrinking_tail():
    D = 86400.0
    # plays days 0,14,22,26,28 -> gaps 14,8,4,2 (days). tail(last 3)=8,4,2 strictly decreasing -> fires.
    series = {"k1": [0 * D, 14 * D, 22 * D, 26 * D, 28 * D]}
    got = tr.detect_repetition(series)
    assert len(got) == 1 and got[0]["magnitude"] == (8 * D) / (2 * D) and got[0]["event_day"] == 28
    assert got[0]["detail"].endswith("8d -> 4d -> 2d.")
    # only 3 plays (2 gaps) -> below the >=4-play floor -> no fire
    assert tr.detect_repetition({"k2": [0.0, 5 * D, 8 * D]}) == []
    # non-monotone tail (2,2,6) -> no fire
    assert tr.detect_repetition({"k3": [0.0, 2 * D, 4 * D, 10 * D]}) == []


def test_revival():
    # prior play day 10; recent plays days 200-203; now 203 -> win_start 197. gap 197-10 = 187 >= 180.
    # depth 203-floor(10) = 193 >= 187. recent 4 >= 3. months 187//30 = 6.
    ad = {"Rev": {10: 1, 200: 1, 201: 1, 202: 1, 203: 1}}
    got = tr.detect_revival(ad, floor_day=10, now_day=203)
    assert len(got) == 1 and got[0]["magnitude"] == 4 / 3 and "6 months" in got[0]["detail"]
    # shallow history (fresh install): depth 55-10=45 < 187 -> nothing, guarding the Takeout artifact.
    # NOTE: this fixture is double-blocked -- it fails BOTH the global depth floor (45 < 187) AND the
    # per-artist gap (win_start = 55-7+1 = 49; prior = {10}; gap = 49-10 = 39 < 180), so it cannot prove
    # the depth floor alone is load-bearing. See test_revival_global_depth_floor_is_not_redundant below
    # for a fixture that isolates the depth floor.
    assert tr.detect_revival({"Rev": {10: 1, 55: 4}}, floor_day=10, now_day=55) == []


def test_revival_global_depth_floor_is_not_redundant():
    """Is the global depth floor (max_day - floor_day >= REVIVAL_DORMANT_DAYS + REVIVAL_RECENT_DAYS ==
    187) mathematically implied by the per-artist gap check (gap >= REVIVAL_DORMANT_DAYS == 180) alone,
    making it pure defense-in-depth? Reasoned out, not assumed:

    gap = win_start - max(prior), where win_start = now_day - REVIVAL_RECENT_DAYS + 1, and prior days are
    bounded below by floor_day (no play can predate the floor), so max(prior) >= floor_day, which gives
    gap <= win_start - floor_day. Also max_day (the GLOBAL max day across all artists) is always
    >= win_start whenever this artist has qualifying recent plays (that's what makes recent >= 3 possible
    at all), so global_depth = max_day - floor_day >= win_start - floor_day >= gap. That means gap >= 180
    only forces global_depth >= 180, NOT >= 187 -- there is a genuine 7-day band
    (180 <= global_depth < 187) where the per-artist gap is satisfied but the global floor still blocks.
    So the floor is NOT redundant: it is REVIVAL_RECENT_DAYS(7) days stricter than what the per-artist
    gap alone would enforce. The two fixtures below isolate exactly that band, holding the per-artist gap
    fixed at >=180 (satisfied in both) and varying only global depth across the 186/187 boundary.
    """
    # floor_day = 0. now_day = 186 -> win_start = 186-7+1 = 180. prior = {0} (the artist's only play
    # before win_start) -> gap = 180-0 = 180 >= REVIVAL_DORMANT_DAYS(180): the per-artist gap check WOULD
    # pass if reached. recent = plays on day 186 = 3 >= REVIVAL_RECENT_MIN(3): would also pass.
    # But global depth = max_day(186) - floor_day(0) = 186 < 187 -> the top-level guard returns []
    # before the per-artist loop ever runs. This isolates the global floor as the sole blocker.
    blocked = tr.detect_revival({"Rev": {0: 1, 186: 3}}, floor_day=0, now_day=186)
    assert blocked == []
    # Same per-artist gap arithmetic (this time 181, still >= 180), but now_day = 187 pushes global depth
    # to exactly 187 - 0 = 187, clearing the floor's boundary. win_start = 187-7+1 = 181; prior = {0};
    # gap = 181-0 = 181 >= 180; recent = plays on day 187 = 3 >= 3 -> fires. months = 181//30 = 6.
    fired = tr.detect_revival({"Rev": {0: 1, 187: 3}}, floor_day=0, now_day=187)
    assert len(fired) == 1 and fired[0]["magnitude"] == 3 / 3 and "6 months" in fired[0]["detail"]


def test_one_song():
    # One: k1 12 plays, k2/k3 zero -> fires on k1 (12 >= 10). magnitude 12/10.
    meta = {"k1": ("One", None), "k2": ("One", None), "k3": ("One", None)}
    got = tr.detect_one_song({"k1": {0: 12}}, meta)
    assert len(got) == 1 and got[0]["subject"] == "k1" and got[0]["magnitude"] == 12 / 10
    # a second played track by the artist disqualifies it
    assert tr.detect_one_song({"k1": {0: 11}, "k2": {0: 1}}, meta) == []


def test_binge():
    # 28 prior days at 2 plays (median 2) + day 128 with 20 plays, 12 (60%) artist A.
    dt = {d: 2 for d in range(100, 128)}
    dt[128] = 20
    da = {128: {"A": 12, "B": 8}}
    got = tr.detect_binge(dt, da, now_day=129)
    # 20 >= 3.0*2, 12/20 = 0.60 >= 0.50 -> fires. magnitude 20/(3*2) = 3.333..., artist A.
    assert len(got) == 1 and got[0]["artist"] == "A" and got[0]["magnitude"] == 20 / 6
    # Day-number-to-date: day 0 = 1970-01-01 (a Thursday). Day 128 -> Jan(31,days 0-30) + Feb(28,31-58)
    # + Mar(31,59-89) + Apr(30,90-119) -> May 1 = day 120, so day 128 = May 1 + 8 = 1970-05-09.
    # Weekday: 128 mod 7 = 2 (128 = 18*7 + 2), Thursday + 2 = Saturday. Verified via
    # datetime.fromtimestamp(128*86400, tz=utc) == 1970-05-09 (Sat). Spec Detail template:
    # "{Weekday Mon D}: {n} plays, {pct}% {artist}." -> "Sat May 9: 20 plays, 60% A."
    assert got[0]["event_day"] == 128
    assert got[0]["detail"] == "Sat May 9: 20 plays, 60% A."
    # concentration below 50% -> no fire. NOTE: brief's fixture here was {"A": 8, "B": 12}; detect_binge
    # picks the top artist by max COUNT (not name "A"), so B:12 would be top at 12/20=60% and WOULD fire,
    # contradicting the comment's intent and the == [] assertion below. Corrected the input (not the
    # assertion) so the top artist is genuinely sub-threshold: max(8, 2) -> A:8, pct = 8/20 = 40% < 50%.
    assert tr.detect_binge(dt, {128: {"A": 8, "B": 2}}, 129) == []


def test_song_of_week():
    # week 14 tracks k1=6, k2=3. now 21 -> latest full week 14. k1 6 >= 5 -> fires. event 20.
    got = tr.detect_song_of_week({14: {"k1": 6, "k2": 3}}, now_day=21)
    assert len(got) == 1 and got[0]["subject"] == "k1" and got[0]["event_day"] == 20
    assert got[0]["magnitude"] == 6 / 5
    assert tr.detect_song_of_week({14: {"k1": 4}}, now_day=21) == []       # below SOTW_MIN_PLAYS


def test_song_of_week_says_last_week_not_this_week():
    """L2: `lw` (the latest FULL week) is the previous, already-concluded week, not the in-progress
    current one -- the copy must say "last week", matching how emergence already phrases the same
    data ("Last week: N plays"), not the dishonest "this week"."""
    got = tr.detect_song_of_week({14: {"k1": 6, "k2": 3}}, now_day=21)
    assert "last week" in got[0]["detail"]
    assert "this week" not in got[0]["detail"]
    assert got[0]["evidence"] == [{"k": "plays", "v": "6 last week"}]


def test_rank_orders_rarity_over_recency():
    # revival: recency 1.0 (event=now), rarity 1.0, mag min(4/3,4)/4 = 0.3333 -> score 0.3333.
    # song_of_week: recency 1-7/42 = 0.83333, rarity 0.35, mag 1.2/4 = 0.3 -> score 0.0875.
    raw = [{"kind": "song_of_week", "event_day": 196, "magnitude": 6 / 5, "signature": "s"},
           {"kind": "revival", "event_day": 203, "magnitude": 4 / 3, "signature": "r"}]
    ranked = tr.rank_insights(raw, now_day=203)
    assert [i["kind"] for i in ranked] == ["revival", "song_of_week"]
    assert ranked[0]["score"] == 1.0 * 1.0 * (min(4 / 3, 4.0) / 4.0)
    assert abs(ranked[1]["score"] - 0.83333333 * 0.35 * 0.3) < 1e-9


def test_rank_insights_drops_stale_zero_score():
    """M3: an insight whose event_day is >= RECENCY_HORIZON_DAYS(42) old scores exactly 0 (recency
    weight fully decayed). Those must be dropped, not merely ranked last -- otherwise a returning user
    with nothing fresh firing would see a months-old "A binge day" card, and (since insights[0] doubles
    as the Home spotlight candidate) that stale entry could even become the spotlight."""
    raw = [{"kind": "binge", "event_day": 0, "magnitude": 5.0, "signature": "old"}]
    assert tr.rank_insights(raw, now_day=100) == []          # 100 - 0 = 100 >= 42 -> rec clipped to 0
    # A stale entry must not survive even when ranked alongside a fresh one (and must not be able to
    # sneak into index 0 -- the spotlight slot -- ahead of the real, non-zero-score insight).
    raw2 = [{"kind": "binge", "event_day": 0, "magnitude": 5.0, "signature": "old"},
            {"kind": "song_of_week", "event_day": 95, "magnitude": 1.2, "signature": "new"}]
    ranked = tr.rank_insights(raw2, now_day=100)
    assert [i["signature"] for i in ranked] == ["new"]


def test_takeout_backfill_immune_to_first_day_eq_floor_day():
    """The dangerous Takeout artifact: a single import snapshot gives EVERY artist the same
    first_day == floor_day exactly. Emergence's guard is first_day-based (first_day > floor_day +
    FLOOR_CENSOR_DAYS), so it must never fire off this artifact no matter how dramatic the apparent
    weekly ramp looks. Revival's guard is independent of first_day (observed-depth only: max_day -
    floor_day >= REVIVAL_DORMANT_DAYS + REVIVAL_RECENT_DAYS), so shallow post-backfill history also
    can't fire (not enough elapsed time to observe a genuine 187-day gap) -- but a genuinely deep
    history still correctly fires even though this artist's first_day is censored, proving the guard
    is the depth floor, not first_day (revival is not blinded by the same artifact)."""
    floor_day = 1000
    artist_first = {"Aphex": floor_day, "Bicep": floor_day}   # backfill: first_day == floor_day for all

    # Aphex: a dramatic-looking ramp (1 play in the import week -> 40 plays in the latest full week),
    # 40 days after the import (<= EMERGENCE_MAX_AGE_DAYS(42)). Without the first_day guard this would
    # fire: 40 >= EMERGENCE_MIN_LATEST(8) and 40 >= EMERGENCE_GROWTH(3.0) * 1. It must not.
    w0 = tr.week_start(floor_day)
    now_day = floor_day + 40
    aw = {"Aphex": {w0: 1, w0 + 35: 40}}
    assert tr.detect_emergence(aw, artist_first, floor_day=floor_day, now_day=now_day) == []

    # Revival: shallow depth (only 40 days since the import) can't observe a 187-day gap, so it
    # correctly can't fire either, even with a qualifying-looking recent burst.
    shallow_ad = {"Bicep": {floor_day: 1, now_day - 1: 1, now_day: 3}}
    assert tr.detect_revival(shallow_ad, floor_day=floor_day, now_day=now_day) == []

    # But a genuinely deep history (400 days since the import) with a real gap DOES fire: depth
    # 400 >= 187. win_start = 1400 - 7 + 1 = 1394. recent (>=1394) = 3 + 2 = 5 >= REVIVAL_RECENT_MIN(3).
    # gap = 1394 - max(prior)=1000 -> 394 >= REVIVAL_DORMANT_DAYS(180). magnitude = 5/3.
    deep_now = floor_day + 400
    win_start = deep_now - tr.REVIVAL_RECENT_DAYS + 1
    deep_ad = {"Bicep": {floor_day: 1, win_start: 3, win_start + 1: 2}}
    got = tr.detect_revival(deep_ad, floor_day=floor_day, now_day=deep_now)
    assert len(got) == 1 and got[0]["artist"] == "Bicep" and got[0]["magnitude"] == 5 / 3
