import pytest
from yt_playlist.core.store import Store
from yt_playlist.rec import trend_rollups as tr
from yt_playlist.util import genre_map


@pytest.fixture
def store():
    s = Store(":memory:")
    s.init_schema()
    s.upsert_identity("me", "c", None, True)   # identity id=1, referenced by history_snapshots FK
    return s


@pytest.fixture(autouse=True)
def toy_genres(monkeypatch):
    monkeypatch.setattr(genre_map, "family", lambda g: (g or "").lower())
    monkeypatch.setattr(genre_map, "family_distance", lambda a, b: 0.0 if a == b else 0.5)


def _snap(store, day, keys):
    """One history snapshot at taken_at = day*86400 containing `keys`. Returns snapshot id."""
    cur = store.conn.execute("INSERT INTO history_snapshots(identity_id, taken_at) VALUES (1, ?)",
                             (day * 86400.0,))
    sid = cur.lastrowid
    for k in keys:
        store.conn.execute("INSERT INTO history_items(snapshot_id, identity_key) VALUES (?, ?)", (sid, k))
    store.conn.commit()
    return sid


def _track(store, key, artist, genre=""):
    store.conn.execute(
        "INSERT INTO tracks(identity_key, video_id, title, artist, genre) VALUES (?,?,?,?,?)",
        (key, "v" + key, "T" + key, artist, genre))
    store.conn.commit()


def test_build_materializes_payload(store):
    _track(store, "k1", "A1", "house")
    _snap(store, 2, ["k1"])
    _snap(store, 5, ["k1"])
    payload = tr.build(store, now=6 * 86400.0)
    # persisted under the rec_proposals surface and readable back
    assert store.get_proposals("trend_rollups")["weeks"][0]["plays"] == 2
    assert payload["first_play_floor_day"] == 2
    assert payload["health"]["total_tracks"] == 1


def test_month_day_counts_zero_fills_and_sums():
    """#79 heat strip data: one entry per calendar day of the month, ascending, zero-filled for days
    with no listens, summing counts for days that have more than one (key, count) row."""
    # Feb 1970 = days 31..58 (day 0 = 1970-01-01 UTC, so day 31 = 1970-02-01). 28 days in Feb 1970.
    day_counts = [(31, "k1", 2), (31, "k2", 1), (33, "k1", 5)]
    out = tr.month_day_counts(day_counts, "1970-02")
    assert len(out) == 28
    assert out[0] == {"day": 1, "n": 3}     # day 31: 2 + 1
    assert out[2] == {"day": 3, "n": 5}     # day 33
    assert out[1] == {"day": 2, "n": 0}     # day 32: no listens
    assert all(d["n"] == 0 for d in out[3:])
    # rows for a different month are ignored
    out_jan = tr.month_day_counts(day_counts, "1970-01")
    assert all(d["n"] == 0 for d in out_jan)


def test_build_review_includes_play_day_heat_strip(store):
    """build() threads its own day_counts into month_review so the rollup's review carries per-day
    heat-strip data (#79) without a second query. now=59*86400.0 (Mar 1, 1970-03) so Feb (day 31) is a
    genuinely completed past month under month_review's own `now` (not real wall-clock time.time())."""
    _track(store, "k1", "A1", "house")
    for d in (0, 1, 2, 3):
        _snap(store, d, ["k1"])
    _snap(store, 31, ["k1"])
    payload = tr.build(store, now=59 * 86400.0)
    review = payload["review"]
    assert review["month"] == "1970-02"
    play_days = review["play_days"]
    assert len(play_days) == 28                 # Feb 1970
    assert play_days[0] == {"day": 1, "n": 1}    # day 31 -> Feb 1


def test_spotlight_month_rollover_fires_once(store, monkeypatch):
    # Two calendar months of a single-genre history so a full PAST month exists. now=59*86400.0 (Mar 1,
    # 1970-03) so Feb (the day-31 snapshot) is a genuinely completed past month under month_review's own
    # `now` (not real wall-clock time.time()) -- i.e. we're a full calendar month past Feb, not still in it.
    _track(store, "k1", "A1", "house")
    for d in (0, 1, 2, 3):
        _snap(store, d, ["k1"])
    _snap(store, 31, ["k1"])
    now = 59 * 86400.0
    payload = tr.build(store, now)
    sp = payload["spotlight"]
    assert sp is not None and sp["signature"].startswith("month_review:")
    # Hand-verified review recap for the last fully-elapsed month (1970-02, the day-31 snapshot):
    # this month's artist plays = 1 (day31), prior month's (Jan, days 0-3) = 4 -> delta -3.
    # A1's first play is Jan (1970-01), so it is not "new" in Feb -> top_new_artist is None.
    # riser requires delta > 0 (none here, so None); faller requires delta < 0 -> A1 at -3.
    review = payload["review"]
    assert review["month"] == "1970-02"
    assert review["plays"] == 1
    assert review["listen_days"] == 1
    assert review["longest_streak"] == 1
    assert review["top_new_artist"] is None
    assert review["riser"] is None
    assert review["faller"] == {"artist": "A1", "delta": -3}
    # M1: since the review candidate is the one that actually won the spotlight cascade here (no
    # insights fired), build() itself must have stamped the review month as consumed -- no manual
    # store.set_setting needed.
    assert store.get_setting("trend_spotlight_review_month") == "1970-02"
    # Once the review month is recorded, the rollover no longer fires (detector 1 is gated) on a rebuild.
    again = tr.build(store, now)
    assert again["spotlight"] is None or not again["spotlight"]["signature"].startswith("month_review:")


def test_takeout_reimport_resets_first_play_watermark(store):
    """A Takeout re-import can wipe + rebuild history_snapshots, reusing low rowids that would fall
    behind the stored watermark. build() must detect the takeout_imported_at bump, clear the first-play
    index, and rescan from watermark 0 -- otherwise the earlier backfilled day would be silently missed
    because history_track_first(wm) only looks at snapshot ids > wm."""
    _track(store, "k1", "A1", "house")
    _snap(store, 5, ["k1"])                      # snapshot id=1
    tr.build(store, now=6 * 86400.0)
    assert store.trends.first_play_map("track")["k1"] == 5
    assert store.get_setting("trend_first_play_watermark") == "1"

    # Simulate a full Takeout re-import: wipe history and reinsert with reused (low) rowids.
    store.conn.execute("DELETE FROM history_items")
    store.conn.execute("DELETE FROM history_snapshots")
    store.conn.commit()
    _snap(store, 2, ["k1"])                       # reused snapshot id=1 (table was emptied)
    store.set_setting("takeout_imported_at", "12345")

    payload = tr.build(store, now=6 * 86400.0)
    # Without the reset this would incorrectly stay at 5 (id=1 is not > old watermark=1).
    assert store.trends.first_play_map("track")["k1"] == 2
    assert payload["first_play_floor_day"] == 2
    assert store.get_setting("trend_first_play_takeout_seen") == "12345"

    # Idempotent: a second build with the same takeout_imported_at does not re-clear / regress.
    payload2 = tr.build(store, now=6 * 86400.0)
    assert store.trends.first_play_map("track")["k1"] == 2
    assert payload2["first_play_floor_day"] == 2


def test_detect_spotlight_cascade_prioritizes_month_review(store):
    """Detector priority 1 (month rollover) wins even when detector 2 (discovery spike) would also
    fire. week1's new_artist_plays=10 vs week0's 0 -> median(prev)=0, latest(10)>=2*0 and >=5 -> spike
    would fire on its own, but review is checked first."""
    review = {"month": "2024-05", "plays": 10, "listen_days": 5, "longest_streak": 2,
              "top_new_artist": None, "riser": None, "faller": None}
    weeks = [
        {"week_start_day": 0, "plays": 1, "distinct_artists": 1, "new_artist_plays": 0,
         "new_track_plays": 0, "families": {}, "diversity": 0.0},
        {"week_start_day": 7, "plays": 10, "distinct_artists": 1, "new_artist_plays": 10,
         "new_track_plays": 10, "families": {}, "diversity": 0.0},
    ]
    assert tr.discovery_spike([w["new_artist_plays"] for w in weeks]) is True   # sanity: would fire alone
    result = tr.detect_spotlight(weeks, [], review, None, store, now=0.0)
    assert result["signature"] == "month_review:2024-05"


def test_detect_spotlight_cascade_prioritizes_discovery_over_family(store):
    """Detector priority 2 (discovery spike) wins even when detector 3 (a family newly in the weekly
    top 5) would also fire. week1 adds 'techno' to the family mix (new vs week0's lone 'house'), which
    would trip the family-entrance detector, but the discovery spike is checked first."""
    weeks = [
        {"week_start_day": 0, "plays": 5, "distinct_artists": 1, "new_artist_plays": 0,
         "new_track_plays": 0, "families": {"house": 5}, "diversity": 0.0},
        {"week_start_day": 7, "plays": 55, "distinct_artists": 1, "new_artist_plays": 50,
         "new_track_plays": 50, "families": {"house": 5, "techno": 50}, "diversity": 0.5},
    ]
    assert tr.discovery_spike([w["new_artist_plays"] for w in weeks]) is True
    entered = tr._top_families(weeks[1]) - tr._top_families(weeks[0])
    assert entered == {"techno"}                                              # sanity: would fire alone
    result = tr.detect_spotlight(weeks, [], None, None, store, now=0.0)
    assert result["signature"].startswith("discovery_spike:")


def test_detect_spotlight_streak_crosses_month_boundary(store):
    """Detector 5 must use the CURRENT ACTIVE streak (current_streak over the full raw day set), not
    months[-1]['longest_streak'] (month-bucketed). Days 28,29,30 fall in Jan 1970 (day 30 = Jan 31,
    since day 0 = Jan 1) and days 31,32,33,34 fall in Feb 1970 (day 31 = Feb 1) -- together an unbroken
    7-day run, but split 3+4 by the month boundary:
      - old (broken) behavior: months[-1] is Feb, whose own longest_streak is only 4 (days 31-34) ->
        4 < STREAK_THRESHOLDS[0]=7 -> no threshold crossed -> detector 5 would NOT fire.
      - new (correct) behavior: current_streak({28..34}) == 7 (unbroken run ending at day 34, the max)
        -> crosses threshold 7 -> detector 5 fires with signature "streak:7".
    now=100*86400.0 (day 100) keeps the (unrelated) diversity detector's floor_month computation
    (day 100 - 90 = day 10, non-negative) out of the way; it can't fire anyway since len(months)=2 is
    below DIVERSITY_MIN_MONTHS=3."""
    jan_days = {28, 29, 30}
    feb_days = {31, 32, 33, 34}
    days = jan_days | feb_days
    assert len(days) == 7                                                          # sanity: 7 distinct days
    months = [
        {"month": "1970-01", "plays": 3, "distinct_artists": 0, "new_artist_plays": 0,
         "families": {}, "diversity": 0.0, "listen_days": len(jan_days),
         "longest_streak": tr.longest_streak(jan_days)},
        {"month": "1970-02", "plays": 4, "distinct_artists": 0, "new_artist_plays": 0,
         "families": {}, "diversity": 0.0, "listen_days": len(feb_days),
         "longest_streak": tr.longest_streak(feb_days)},
    ]
    assert months[0]["longest_streak"] == 3                        # Jan run 28,29,30
    assert months[1]["longest_streak"] == 4                        # Feb run 31,32,33,34
    assert months[-1]["longest_streak"] < 7                        # sanity: old month-scoped logic misses it
    assert tr.current_streak(days) == 7                             # unbroken run ending at day 34
    result = tr.detect_spotlight([], months, None, None, store, now=100 * 86400.0, days=days)
    assert result is not None and result["signature"] == "streak:7"


def test_build_emits_ranked_insights_and_spotlight(store):
    """One track "on the floor" (A1/k1, day 50) establishes floor_day=50 so it is censored for
    EMERGENCE. A separate, later-arriving artist (B1/k2) ramps from 1 play in week 98 to 12 plays in
    week 119, which is uncensored (98 > floor_day(50)+FLOOR_CENSOR_DAYS(7)=57) and clears EMERGENCE's
    age/growth/floor thresholds -- see tests/rec/test_insight_detectors.py::test_emergence_fires_and_censors
    for the identical arithmetic, hand-verified there. now_day=126 also makes week 119 (12 plays, all on
    k2, B1's only track) the latest FULL week, so ONE_SONG (13 total plays >= 10 on a single track) and
    SONG_OF_WEEK (12 plays >= 5 in the latest full week) both additionally fire on B1/k2.

    Hand-ranked scores (rec = max(0, 1-(126-event_day)/42), mag = min(magnitude,4)/4):
      emergence   event_day=119 rec=1-7/42=0.8333  mag=1.5/4=0.375  rarity=0.80 -> score=0.2500
      song_of_week event_day=125 rec=1-1/42=0.9762 mag=2.4/4=0.600  rarity=0.35 -> score=0.2050
      one_song    event_day=119 rec=1-7/42=0.8333  mag=1.3/4=0.325  rarity=0.60 -> score=0.1625
    -> rank order: emergence, song_of_week, one_song. Rank-1 (emergence) becomes the spotlight."""
    _track(store, "k1", "A1", "house")
    _snap(store, 50, ["k1"])                      # floor_day=50 (k1's first play)
    _track(store, "k2", "B1", "house")
    _snap(store, 98, ["k2"])                       # week 98: 1 play
    _snap(store, 119, ["k2"] * 12)                 # week 119: 12 plays, latest full week (119+7<=126)
    payload = tr.build(store, now=126 * 86400.0)
    insights = payload["insights"]
    assert [i["signature"] for i in insights] == ["emergence:B1:119", "song_of_week:119:k2",
                                                   "one_song:B1:k2"]
    # art/anchor/cta baked onto every insight (artist-subject here: B1 has no thumbnail seeded, and
    # no album_browse_id path applies to an artist-subject insight either way).
    for i in insights:
        assert "art" in i and "anchor" in i and "cta" in i and "computed_at" in i
    assert insights[0]["anchor"] == "/artist?name=B1"
    # the spotlight, when insights exist, is the rank-1 insight pointing at the insights section
    assert payload["spotlight"]["signature"] == insights[0]["signature"] == "emergence:B1:119"
    assert payload["spotlight"]["anchor"] == "insights"
    assert payload["spotlight"]["headline"] == insights[0]["headline"]
    assert payload["insights"] is insights and "insights" in payload
    # M2: song_of_week's headline must name the track (baked once its title is known in _bake_art) --
    # both the Insights card and the Home spotlight card render only headline+detail.
    sotw = next(i for i in insights if i["kind"] == "song_of_week")
    assert sotw["title"] == "Tk2" and "Tk2" in sotw["headline"]
    # M1: a review month DOES exist here too (Feb from day 50, Apr from days 98/119 -> cur="1970-04"),
    # but since an insight (emergence) outranks it for the spotlight slot, build() must NOT burn/consume
    # the review month -- it needs to stay available so "Your month in review is ready" can still reach
    # Home on a later build once nothing outranks it.
    assert payload["review"] is not None and payload["review"]["month"] == "1970-04"
    assert store.get_setting("trend_spotlight_review_month") is None


def test_bake_art_names_track_in_repetition_and_song_of_week_headline(store):
    """M2: repetition and song_of_week's raw headlines ("A track is hooking in" / "Song of the week")
    never name the track -- and both the Insights card and the Home spotlight card (trend_spotlight.html)
    render ONLY headline+detail, so a card that never names its subject leaves the user guessing which
    track. _bake_art must fold the track's title into the headline once it looks it up."""
    _track(store, "k1", "A1", "house")
    store.conn.execute("UPDATE tracks SET title=? WHERE identity_key=?", ("Windowlicker", "k1"))
    store.conn.commit()
    raw = [
        {"kind": "repetition", "subject_kind": "track", "subject": "k1", "artist": None, "title": None,
         "event_day": 28, "magnitude": 4.0, "signature": "repetition:k1:28",
         "headline": "A track is hooking in", "detail": "You keep coming back faster: 8d -> 4d -> 2d.",
         "evidence": []},
        {"kind": "song_of_week", "subject_kind": "track", "subject": "k1", "artist": None, "title": None,
         "event_day": 20, "magnitude": 1.2, "signature": "song_of_week:14:k1",
         "headline": "Song of the week", "detail": "6 plays last week, your most-played track.",
         "evidence": []},
    ]
    rep, sotw = tr._bake_art(store, raw, now=100.0)
    assert "Windowlicker" in rep["headline"]
    assert "Windowlicker" in sotw["headline"]
    # a track with no title on file (track_cards returns None) falls back to the generic headline
    # rather than baking in "None".
    raw_notitle = [{"kind": "song_of_week", "subject_kind": "track", "subject": "missing", "artist": None,
                     "title": None, "event_day": 20, "magnitude": 1.2, "signature": "song_of_week:14:missing",
                     "headline": "Song of the week", "detail": "6 plays last week, your most-played track.",
                     "evidence": []}]
    baked = tr._bake_art(store, raw_notitle, now=100.0)
    assert baked[0]["headline"] == "Song of the week"


def test_build_review_has_calendar_and_podium(store):
    """month_review's "cur" month is the last entry of `past`, where `past` is filtered against
    `now`'s own month (month_review now threads build()'s `now` argument through instead of reading real
    wall-clock time.time()). now=59*86400.0 (Mar 1, 1970-03) puts us a full calendar month past Feb, so
    Feb (the day-31 snapshot) is a genuinely completed past month -- cur = "1970-02", matching the
    fixture/assertion pattern test_spotlight_month_rollover_fires_once already established for this same
    shape of data."""
    _track(store, "k1", "A1", "house")
    for d in (0, 1, 2, 3):
        _snap(store, d, ["k1"])
    _snap(store, 31, ["k1"])                     # Feb 1 1970 -> cur month "1970-02"
    payload = tr.build(store, now=59 * 86400.0)
    rv = payload["review"]
    assert rv["month"] == "1970-02"
    # Feb 1 1970 is a Sunday -> weekday()==6 -> Sunday-indexed (6+1)%7 == 0.
    assert rv["first_weekday"] == 0
    assert rv["top_artists"][0]["artist"] == "A1"
    assert rv["top_artists"][0]["plays"] == 1
    assert rv["top_track"]["track"] == "k1"
    assert rv["top_track"]["plays"] == 1
    # Feb's only listen day is day 31 (the one snapshot) -> the peak day by construction.
    assert rv["binge"]["day"] == 31
    assert rv["binge"]["plays"] == 1
    assert rv["binge"]["artist"] == "A1"


def test_build_health_has_rediscover_and_unopened(store):
    _track(store, "cold", "A1")
    for d in (1, 2, 3):
        _snap(store, d, ["cold"])                 # 3 plays, last day 3 -> "quiet" vs a far-future now
    payload = tr.build(store, now=400 * 86400.0)  # 400 - 3 = 397 days > 90 -> cold is a candidate
    assert any(r["identity_key"] == "cold" for r in payload["health"]["rediscover"])
    assert payload["health"]["unopened_albums"]["count"] == 0   # no saved albums seeded
    assert payload["health"]["unopened_albums"]["items"] == []
