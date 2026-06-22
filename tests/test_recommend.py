from yt_playlist.rec import embed, genre_map, recommend
from yt_playlist.core.store import Store


def test_wheelhouse_excludes_play_recency_lane(store):
    """Wheelhouse is the taste/genre model, not play-recency: a high-play dormant track that isn't
    a deep cut or taste neighbour must NOT surface in for_you (it belongs to Comfort Listening)."""
    iid = store.upsert_identity("main", "cred", None, True)
    store.upsert_track("v1", "Gem", "X", None, None)         # key "gem|x" — played a lot, then dormant
    store.upsert_track("v2", "Bench", "X", None, None)       # X's never-played track -> X's deep cut
    day = 86400.0
    now = 200 * day
    for d in (120, 100, 80, 60):                              # 4 plays, last 60 days ago
        store.add_history_snapshot(iid, now - d * day, ["gem|x"])

    # Bench (never played) is X's deep cut, so Gem isn't; with no playlists Gem isn't a neighbour
    # either. Wheelhouse must not resurface Gem on play-recency alone.
    assert "Gem" not in {i.title for i in recommend.for_you(store, now=now, limit=10)}
    # ...but Comfort Listening is exactly where it belongs.
    assert "Gem" in {i.title for i in recommend.comfort_listening(store, now=now, limit=10)}


def test_comfort_listening_favors_high_play_not_recent(store):
    """Comfort = your high-rotation favorites, demoted the more recently you've heard them."""
    iid = store.upsert_identity("main", "cred", None, True)
    store.upsert_track("v1", "Old Favorite", "X", None, None)
    store.upsert_track("v2", "Recent Favorite", "X", None, None)
    day = 86400.0
    now = 400 * day
    for d in (300, 250, 200, 150, 90):                       # 5 plays, last 90 days ago
        store.add_history_snapshot(iid, now - d * day, ["old favorite|x"])
    for d in (20, 15, 10, 5, 1):                             # 5 plays, last yesterday
        store.add_history_snapshot(iid, now - d * day, ["recent favorite|x"])

    items = recommend.comfort_listening(store, now=now, limit=10)
    titles = [i.title for i in items]
    assert {"Old Favorite", "Recent Favorite"} <= set(titles)
    assert titles.index("Old Favorite") < titles.index("Recent Favorite")   # not-recent ranks higher
    assert all(i.lane == "comfort" for i in items)


def test_comfort_listening_excludes_never_and_barely_played(store):
    """Comfort is grounded in real rotation: never-played and below-min_plays tracks don't show."""
    iid = store.upsert_identity("main", "cred", None, True)
    store.upsert_track("v1", "Never", "X", None, None)       # zero plays
    store.upsert_track("v2", "Barely", "X", None, None)      # one play, below default min_plays=4
    store.upsert_track("v3", "Worn", "X", None, None)        # 5 plays
    day = 86400.0
    now = 400 * day
    store.add_history_snapshot(iid, now - 50 * day, ["barely|x"])
    for d in (200, 150, 100, 80, 60):
        store.add_history_snapshot(iid, now - d * day, ["worn|x"])

    titles = {i.title for i in recommend.comfort_listening(store, now=now, limit=10)}
    assert "Worn" in titles
    assert "Never" not in titles
    assert "Barely" not in titles


def test_for_you_genre_suppression_reduces_that_family(store):
    """Per-genre weights re-rank for_you: muting a family yields fewer of its tracks than favoring it."""
    iid = store.upsert_identity("main", "cred", None, True)
    techno = [store.upsert_track(f"t{i}", f"T{i}", "TechnoBand", None, None) for i in range(6)]
    folk = [store.upsert_track(f"f{i}", f"F{i}", "FolkBand", None, None) for i in range(6)]
    store.set_playlist_tracks(store.upsert_playlist(iid, "PT", "Techno", 6, "h", 0.0), techno)
    store.set_playlist_tracks(store.upsert_playlist(iid, "PF", "Folk", 6, "h2", 0.0), folk)
    store.add_history_snapshot(iid, 1.0, ["t0|technoband"])
    store.add_history_snapshot(iid, 1.0, ["f0|folkband"])
    for t in techno:
        store.set_track_genre(t, "Techno")
    for f in folk:
        store.set_track_genre(f, "Folk")
    embed.build_and_store(store, dim=4)        # 12 tracks; build needs len(keys) >= dim + 5
    fam = genre_map.family("Folk")

    def folk_count():
        return sum(1 for i in recommend.for_you(store, now=1000.0, limit=6) if i.artist == "FolkBand")

    store.set_weight(f"genre:{fam}", 2.0, lo=0.0, hi=2.0)       # favor folk
    boosted = folk_count()
    store.set_weight(f"genre:{fam}", 0.0, lo=0.0, hi=2.0)       # mute folk
    suppressed = folk_count()
    assert boosted > suppressed


def test_comfort_candidates_scores_plays_times_recency(store):
    """comfort_candidates ranks by plays * min(1, days_since_last / recency_full_days): a dormant
    4-play track beats a recently-spun 8-play one, and below-min_plays tracks are excluded."""
    iid = store.upsert_identity("main", "cred", None, True)
    store.upsert_track("v1", "Dormant", "X", None, None)        # key "dormant|x"
    store.upsert_track("v2", "HeavyRecent", "X", None, None)    # key "heavyrecent|x"
    store.upsert_track("v3", "Barely", "X", None, None)         # key "barely|x"
    day = 86400.0
    now = 400 * day
    for d in (300, 250, 200, 120):                              # 4 plays, last 120d ago -> factor 1.0
        store.add_history_snapshot(iid, now - d * day, ["dormant|x"])
    for d in (40, 35, 30, 25, 20, 15, 10, 1):                  # 8 plays, last yesterday -> factor ~1/30
        store.add_history_snapshot(iid, now - d * day, ["heavyrecent|x"])
    store.add_history_snapshot(iid, now - 10 * day, ["barely|x"])   # 1 play -> below min_plays

    res = store.comfort_candidates(now=now, min_plays=4, recency_full_days=30, limit=10)
    titles = [r["title"] for r in res]
    assert titles[0] == "Dormant"                              # 4*1.0 > 8*(1/30)
    assert set(titles) == {"Dormant", "HeavyRecent"}           # Barely excluded (below min_plays)
    assert res[0]["plays"] == 4 and res[0]["last_played"] == now - 120 * day


def test_for_you_blends_real_signals_with_reasons(store):
    iid = store.upsert_identity("main", "cred", None, True)
    # Artist you play a lot: "Hit" played, "Neglected" not -> a deep cut
    hit = store.upsert_track("v1", "Hit", "Fav", None, None)        # key "hit|fav"
    store.upsert_track("v2", "Neglected", "Fav", None, None)        # key "neglected|fav"
    # A neighbour that co-occurs in a playlist with your most-played track
    nb = store.upsert_track("v3", "Neighbour", "Other", None, None)  # key "neighbour|other"
    pl = store.upsert_playlist(iid, "PL1", "Mix", 2, "h", 0.0)
    store.set_playlist_tracks(pl, [hit, nb])
    now = 1000.0
    store.add_history_snapshot(iid, now - 100, ["hit|fav"])
    store.add_history_snapshot(iid, now - 50, ["hit|fav"])

    items = recommend.for_you(store, now=now, limit=10)
    titles = {i.title for i in items}

    assert "Neglected" in titles          # deep cut from an artist you play
    assert "Neighbour" in titles          # shares a playlist with your most-played
    assert "Hit" not in titles            # your already-played seed isn't recommended back
    assert all(i.reason for i in items)   # every rec explains itself


def test_for_you_never_empty_without_history_depth(store):
    """The day-one failure that shipped empty: a normal library must still yield recs."""
    iid = store.upsert_identity("main", "cred", None, True)
    a = store.upsert_track("v1", "Played", "Band", None, None)     # key "played|band"
    store.upsert_track("v2", "Bench", "Band", None, None)          # neglected by same artist
    store.set_playlist_tracks(store.upsert_playlist(iid, "PL", "P", 1, "h", 0.0), [a])
    now = 1000.0
    store.add_history_snapshot(iid, now - 60, ["played|band"])     # all history is RECENT
    store.add_history_snapshot(iid, now - 30, ["played|band"])

    items = recommend.for_you(store, now=now, limit=10)
    assert items, "for_you must not be empty on a fresh library with plays"


def test_rotate_sample_is_stable_per_epoch_then_changes(store):
    """A list card holds its content within an epoch, and reseeds to a different slice the next one."""
    items = list(range(40))
    e0a = recommend.rotate_sample(items, 12, epoch=0)
    e0b = recommend.rotate_sample(items, 12, epoch=0)
    e1 = recommend.rotate_sample(items, 12, epoch=1)
    assert e0a == e0b                      # same epoch -> identical (the "refresh twice = same" rule)
    assert e0a != e1                       # next epoch -> a fresh random slice
    assert len(e0a) == 12 and set(e0a) <= set(items)
    assert recommend.rotate_sample([1, 2, 3], 12, epoch=5) == [1, 2, 3]   # pool smaller than card: as-is


def test_rotate_page_advances_and_wraps(store):
    """A grid card pages forward each epoch and wraps once the pool is exhausted (rotate, not empty)."""
    items = list(range(10))
    assert recommend.rotate_page(items, 4, epoch=0) == [0, 1, 2, 3]
    assert recommend.rotate_page(items, 4, epoch=1) == [4, 5, 6, 7]
    assert recommend.rotate_page(items, 4, epoch=2) == [8, 9, 0, 1]      # wraps through the end
    assert recommend.rotate_page([], 4, epoch=3) == []


def test_taste_sample_is_a_rotating_slice_not_the_top_n(store):
    """The Taste page's 'refresh sample' must show a *random slice* of matching tracks: each refresh
    a new set (even with knobs unchanged), drawn from beyond the deterministic top-N."""
    iid = store.upsert_identity("main", "cred", None, True)
    now = 1000.0
    for n in range(30):                                            # 30 artists -> 30 deep-cut candidates
        store.upsert_track(f"h{n}", f"Hit{n}", f"Band{n}", None, None)
        store.upsert_track(f"d{n}", f"Deep{n}", f"Band{n}", None, None)
        store.add_history_snapshot(iid, now - 100, [f"hit{n}|band{n}"])
        store.add_history_snapshot(iid, now - 50, [f"hit{n}|band{n}"])

    runs = [tuple(i.key for i in recommend.taste_sample(store, now, limit=8)) for _ in range(15)]
    assert all(len(set(r)) == 8 for r in runs)         # a full, deduped slice every refresh
    assert len(set(runs)) > 1                           # refresh yields a new set, not the same top-8
    union = {k for r in runs for k in r}
    assert len(union) > 8                               # samples reach beyond a fixed top-8


def test_complete_playlist_suggests_fitting_owned_tracks(store):
    iid = store.upsert_identity("main", "cred", None, True)
    a = store.upsert_track("v1", "Anchor", "Band", None, None)      # in the target playlist
    b = store.upsert_track("v2", "Bonus", "Band", None, None)       # same artist, not in target
    c = store.upsert_track("v3", "Cooc", "Other", None, None)       # co-occurs with anchor elsewhere
    target = store.upsert_playlist(iid, "PT", "Target", 1, "h", 0.0)
    store.set_playlist_tracks(target, [a])
    other = store.upsert_playlist(iid, "PO", "Other", 3, "h2", 0.0)
    store.set_playlist_tracks(other, [a, b, c])                     # anchor co-occurs with b and c here

    items = recommend.complete_playlist(store, target, limit=10)
    titles = {i.title for i in items}

    assert "Bonus" in titles          # same artist as a member
    assert "Cooc" in titles           # co-occurs with a member in another playlist
    assert "Anchor" not in titles     # already in the playlist
    assert all(i.reason for i in items)


def test_take_action_auth_and_cleanup_no_sync(store):
    iid = store.upsert_identity("main", "cred", None, True)
    store.upsert_playlist(iid, "PLEMPTY", "Empties", 0, "h", 0.0)   # empty playlist
    recommend.refresh_cleanup(store, now=1000.0)      # worker/cleanup-page materialize the cached summary

    items = recommend.take_action(store, now=1000.0, auth_expired={iid: "main"})

    kinds = [i.kind for i in items]
    assert "sync" not in kinds                       # sync nudge now lives in the Sync card
    assert kinds[0] == "auth"
    assert "Re-authenticate main" in items[0].title
    assert any(i.kind == "cleanup" and i.cta_href == "/cleanup" for i in items)


def test_take_action_empty_when_clean(store):
    store.upsert_identity("main", "cred", None, True)
    assert recommend.take_action(store, now=1000.0, auth_expired={}) == []


def test_take_action_enrichment_ranked_by_playcount(store):
    iid = store.upsert_identity("main", "cred", None, True)
    hot = [store.upsert_track(f"h{i}", f"H{i}", "HA", None, None) for i in range(6)]   # all gappy
    cold = [store.upsert_track(f"c{i}", f"C{i}", "CA", None, None) for i in range(6)]  # all gappy
    php = store.upsert_playlist(iid, "PH", "Hot List", 6, "h", 0.0, "http://t/hot.jpg")
    plp = store.upsert_playlist(iid, "PL", "Cold List", 6, "h2", 0.0)
    store.set_playlist_tracks(php, hot)
    store.set_playlist_tracks(plp, cold)
    store.add_history_snapshot(iid, 1.0, ["h0|ha"])           # only Hot List has plays

    items = recommend.take_action(store, now=1000.0, auth_expired={})
    enrich = [i for i in items if i.kind == "enrich"]

    assert len(enrich) == 2                           # one card per gappy playlist
    assert all(i.severity == "low" for i in enrich)
    assert "Hot List" in enrich[0].title              # ranked by playcount: Hot List first
    assert "Cold List" in enrich[1].title
    assert enrich[0].cta_href == f"/playlist/{php}?enrich=1"
    assert enrich[0].thumbnail == "http://t/hot.jpg"  # card carries the playlist thumbnail


def test_enrichment_skips_mostly_enriched_playlist(store):
    """A playlist enriched down to a few untaggable residuals must stop nagging (the 13/639 bug)."""
    iid = store.upsert_identity("main", "cred", None, True)
    ts = [store.upsert_track(f"t{i}", f"T{i}", "A", None, None) for i in range(20)]
    pid = store.upsert_playlist(iid, "P", "Almost Done", 20, "h", 0.0)
    store.set_playlist_tracks(pid, ts)
    for t in ts[2:]:                                  # tag all but 2 -> 10% gap, below threshold
        store.set_track_genre(t, "Techno")
    items = recommend.take_action(store, now=1000.0, auth_expired={})
    assert not [i for i in items if i.kind == "enrich"]


def test_sync_status_never_synced(store):
    st = recommend.sync_status(store, now=1000.0)
    assert st.last_synced_ago is None and st.stale and st.message


def test_sync_status_recent_is_not_stale(store):
    store.set_setting("last_sync_at", "1000.0")
    st = recommend.sync_status(store, now=1000.0 + 60)
    assert st.stale is False and st.message is None and st.last_synced_ago


def test_ago_granularity():
    # Sub-hour ages must report minutes, not collapse to "just now" (a 19-min-old sync
    # showing "just now" was misleading).
    assert recommend._ago(30) == "just now"            # under a minute
    assert recommend._ago(60) == "1 minute ago"
    assert recommend._ago(19 * 60) == "19 minutes ago"
    assert recommend._ago(3600) == "1 hour ago"
    assert recommend._ago(2 * 3600) == "2 hours ago"
    assert recommend._ago(86400) == "1 day ago"


def test_sync_status_over_24h_is_stale(store):
    store.set_setting("last_sync_at", "1000.0")
    st = recommend.sync_status(store, now=1000.0 + 25 * 3600)
    assert st.stale is True and st.message


def test_sync_status_urgent_when_drifting(store):
    store.set_setting("last_sync_at", str(1000.0))
    assert recommend.sync_status(store, 1000.0 + 3600).urgent is False
    mild = recommend.sync_status(store, 1000.0 + recommend.SYNC_STALE_S + 3600)
    assert mild.stale is True and mild.urgent is False
    bad = recommend.sync_status(store, 1000.0 + recommend.SYNC_STALE_S + 5 * 86400)
    assert bad.stale is True and bad.urgent is True and "drifting" in bad.message


def _seed_two_eras(store):
    """6 nineties + 6 noughties tracks, one playlist each, model built. Returns iid."""
    iid = store.upsert_identity("main", "cred", None, True)
    nineties = [store.upsert_track(f"n{i}", f"N{i}", f"NB{i}", None, None) for i in range(6)]
    noughts = [store.upsert_track(f"o{i}", f"O{i}", f"OB{i}", None, None) for i in range(6)]
    for t in nineties:
        store.set_track_year(t, "1995")
    for t in noughts:
        store.set_track_year(t, "2005")
    store.set_playlist_tracks(store.upsert_playlist(iid, "PN", "Nineties", 6, "h", 0.0), nineties)
    store.set_playlist_tracks(store.upsert_playlist(iid, "PO", "Noughts", 6, "h2", 0.0), noughts)
    store.add_history_snapshot(iid, 1.0, ["n0|nb0"])
    store.add_history_snapshot(iid, 1.0, ["o0|ob0"])
    embed.build_and_store(store, dim=4)
    return iid


def test_for_you_era_suppression_reduces_that_decade(store):
    _seed_two_eras(store)

    def nineties_count():
        return sum(1 for i in recommend.for_you(store, now=1000.0, limit=6)
                   if i.title.startswith("N"))

    store.set_weight("era:1990", 2.0, lo=0.0, hi=2.0)      # favor the 90s
    boosted = nineties_count()
    store.set_weight("era:1990", 0.0, lo=0.0, hi=2.0)      # mute the 90s
    suppressed = nineties_count()
    assert boosted > suppressed


def test_for_you_artist_boost_lifts_that_artist(store):
    _seed_two_eras(store)

    def nb0_count():
        return sum(1 for i in recommend.for_you(store, now=1000.0, limit=6) if i.artist == "NB0")
    store.set_weight("artist:NB0", 2.0, lo=0.0, hi=2.0)
    boosted = nb0_count()
    store.set_weight("artist:NB0", 0.0, lo=0.0, hi=2.0)
    suppressed = nb0_count()
    assert boosted >= suppressed
    assert boosted >= 1


def test_axis_adjusted_scores_neutral_is_noop(store):
    scores = {"a": 0.5, "b": 0.4}
    assert recommend.axis_adjusted_scores(scores, {"a": 1.0, "b": 1.0}) == scores
    assert recommend._axis_weights_for(store, ["a", "b"]) is None   # nothing stored -> neutral


def test_complete_playlist_caps_flooding_artist_on_eclectic_playlist(store):
    """An eclectic playlist (many artists) shouldn't get flooded by one artist's big catalog — the
    '529 repeats' bug, where a 12-track/10-artist playlist returned 9 tracks by one band."""
    from collections import Counter
    iid = store.upsert_identity("main", "cred", None, True)
    # "WL" has a big catalog; 9 other artists have a couple tracks each
    wl = [store.upsert_track(f"wl{i}", f"WL song {i}", "WL", None, None) for i in range(11)]
    others = {}
    for n in range(9):
        a = f"Art{n}"
        others[a] = [store.upsert_track(f"a{n}_{i}", f"{a} song {i}", a, None, None) for i in range(2)]
    # eclectic target: 1 WL + 1 from each of the 9 others -> 10 distinct artists
    target = store.upsert_playlist(iid, "PT", "Eclectic", 10, "h", 0.0)
    store.set_playlist_tracks(target, [wl[0]] + [others[a][0] for a in others])
    # a big co-occurrence playlist so the rest are candidates (deterministic fallback path; no vectors)
    store.set_playlist_tracks(store.upsert_playlist(iid, "PO", "All", 29, "h2", 0.0),
                              wl + [t for ts in others.values() for t in ts])

    items = recommend.complete_playlist(store, target, limit=12)
    by_artist = Counter(i.artist for i in items)
    assert by_artist.get("WL", 0) <= 2     # the big-catalog artist no longer floods (was ~9)
    assert len(by_artist) >= 4             # eclectic variety preserved


def test_playlist_facets_groups_genres_eras_tracks(store):
    """The transient feedback panel needs the mix's facets (genres/eras/tracks) + the keys to tilt."""
    iid = store.upsert_identity("main", "cred", None, True)
    a = store.upsert_track("v1", "SongA", "ArtA", None, None); store.set_track_genre(a, "Techno"); store.set_track_year(a, "1995")
    b = store.upsert_track("v2", "SongB", "ArtB", None, None); store.set_track_genre(b, "Techno"); store.set_track_year(b, "2005")
    c = store.upsert_track("v3", "SongC", "ArtC", None, None); store.set_track_genre(c, "Folk"); store.set_track_year(c, "1995")
    pid = store.upsert_playlist(iid, "PL", "Mix", 3, "h", 0.0)
    store.set_playlist_tracks(pid, [a, b, c])

    f = recommend.playlist_facets(store, pid)
    gnames = {g["name"] for g in f["genres"]}
    assert genre_map.family("Techno") in gnames and genre_map.family("Folk") in gnames
    assert {"1990", "2000"} <= {e["name"] for e in f["eras"]}
    assert {t["title"] for t in f["tracks"]} == {"SongA", "SongB", "SongC"}
    techno = next(g for g in f["genres"] if g["name"] == genre_map.family("Techno"))
    assert len(techno["keys"]) == 2            # both techno tracks' keys, to tilt that subset


def _seed_pl(store, iid, ytm, title, n, now, played_at=None, group=None, play_each_at=None):
    """Make a library playlist of `n` tracks, optionally with play history.

    play_each_at: list aligned to the n tracks — play_each_at[i] is when track i was last played
                  (None entry = that track was never played). One snapshot per played track.
    played_at:    legacy shorthand — play just the *first* track at this time (the rest never played).
    """
    from yt_playlist.util.matching import identity_key
    keys, tids = [], []
    for i in range(n):
        t, a = f"{ytm} song {i}", f"{ytm} artist"
        tids.append(store.upsert_track(f"v_{ytm}_{i}", t, a, None, None))
        keys.append(identity_key(t, a))
    pid = store.upsert_playlist(iid, ytm, title, n, f"h_{ytm}", now)
    store.set_playlist_tracks(pid, tids)
    if group:
        store.set_playlist_group(ytm, group)
    if played_at is not None:
        store.add_history_snapshot(iid, played_at, [keys[0]])
    for k, ts in zip(keys, play_each_at or []):
        if ts is not None:
            store.add_history_snapshot(iid, ts, [k])
    return pid


def test_rediscover_ranks_by_aggregate_track_staleness(store):
    iid = store.upsert_identity("main", "cred", None, True)
    day = 86400.0
    now = 300 * day
    # Every track played at the same time -> the playlist's median recency is exactly that time.
    _seed_pl(store, iid, "PA", "Ancient", 8, now, play_each_at=[now - 200 * day] * 8)
    _seed_pl(store, iid, "PB", "Dusty", 8, now, play_each_at=[now - 150 * day] * 8)
    _seed_pl(store, iid, "PC", "Fresh", 8, now, play_each_at=[now - 3 * day] * 8)
    _seed_pl(store, iid, "LM", "Liked Music", 8, now, play_each_at=[now - 400 * day] * 8)  # system -> skip
    store.upsert_playlist(iid, "PE", "Empty", 0, "h_pe", now)                               # empty -> skip
    _seed_pl(store, iid, "PG", "Gen", 8, now, play_each_at=[now - 500 * day] * 8, group="Generated")  # skip

    out = recommend.rediscover_playlists(store, now, count=2, per=5)
    assert [p["title"] for p in out] == ["Ancient", "Dusty"]   # coldest median first; others excluded
    assert len(out[0]["tracks"]) == 5                          # a teaser, not the full 8
    assert out[0]["track_count"] == 8 and out[0]["last_played"]


def test_rediscover_one_fresh_track_does_not_unstale_a_cold_playlist(store):
    # The reported bug: a playlist with a single recently-played track was ranked as "played hours
    # ago". With median-of-tracks it stays cold, because most of it is cold.
    iid = store.upsert_identity("main", "cred", None, True)
    day = 86400.0
    now = 300 * day
    # "Decoy": 7 cold tracks (180d) + 1 fresh (1d). MAX would say ~1 day; median says ~180 days.
    _seed_pl(store, iid, "PD", "Decoy", 8, now, play_each_at=[now - 1 * day] + [now - 180 * day] * 7)
    _seed_pl(store, iid, "PW", "Warm", 8, now, play_each_at=[now - 20 * day] * 8)

    out = recommend.rediscover_playlists(store, now, count=2, per=5)
    assert out[0]["title"] == "Decoy"                          # cold-in-aggregate leads, not the fresh track
    # the displayed time reflects the median (~180d), not the single fresh track (~1d)
    assert "180 days ago" in out[0]["last_played"]


def test_rediscover_treats_never_played_as_coldest(store):
    iid = store.upsert_identity("main", "cred", None, True)
    day = 86400.0
    now = 100 * day
    _seed_pl(store, iid, "PP", "Played", 3, now, play_each_at=[now - 10 * day] * 3)
    _seed_pl(store, iid, "PN", "Untouched", 3, now)            # never played -> coldest of all

    out = recommend.rediscover_playlists(store, now, count=2, per=5)
    assert [p["title"] for p in out] == ["Untouched", "Played"]   # never-played is coldest -> leads
    never = next(p for p in out if p["title"] == "Untouched")
    assert never["last_played"] is None                           # -> "mostly never played" in the UI


def test_rediscover_rotates_through_cold_pool_by_epoch(store):
    # Same erosion/rotation as other Home cards: page through the ranked-by-coldness pool, one page
    # per epoch, wrapping — so you cycle through cold playlists instead of always seeing the same two.
    iid = store.upsert_identity("main", "cred", None, True)
    day = 86400.0
    now = 400 * day
    for ytm, title, age in [("PA", "A", 200), ("PB", "B", 150), ("PC", "C", 100), ("PD", "D", 50)]:
        _seed_pl(store, iid, ytm, title, 4, now, play_each_at=[now - age * day] * 4)
    titles = lambda e: [p["title"] for p in recommend.rediscover_playlists(store, now, count=2, epoch=e)]
    assert titles(0) == ["A", "B"]        # coldest page leads
    assert titles(1) == ["C", "D"]        # next epoch advances to the next-coldest page
    assert titles(2) == ["A", "B"]        # wraps back around the ranked pool


def test_rediscover_rotation_stays_within_coldest_pool(store):
    # The rotating pool is capped to the coldest N: warmer playlists never surface, even across epochs.
    iid = store.upsert_identity("main", "cred", None, True)
    day = 86400.0
    now = 400 * day
    for ytm, title, age in [("PA", "A", 300), ("PB", "B", 250), ("PC", "C", 200),
                            ("PD", "D", 150), ("PE", "E", 100), ("PF", "F", 50)]:
        _seed_pl(store, iid, ytm, title, 4, now, play_each_at=[now - age * day] * 4)
    seen = set()
    for e in range(6):
        seen.update(p["title"] for p in recommend.rediscover_playlists(store, now, count=2, epoch=e, pool=4))
    assert seen == {"A", "B", "C", "D"}   # coldest 4 cycle through; warmest E, F never appear
