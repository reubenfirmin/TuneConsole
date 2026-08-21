# tests/test_theme_sentence.py
"""Each generated card says what is actually in it.

A card is a lane AND a rolled theme, so two "More in your wheelhouse" cards can be different music
entirely - and the old fixed per-lane line ("Deeper into what you already love") described neither.
The sentence is built from the tracks that are there, and drops any clause it can't support.
"""
from yt_playlist.core.store import Store
from yt_playlist.rec import recommend


def _store():
    s = Store(":memory:")
    s.init_schema()
    return s


def _tracks(store, spec):
    """spec: (title, artist, genre, year) tuples -> genre-attached items, as a card carries them."""
    iid = store.upsert_identity("main", "cred", None, True)
    pid = store.upsert_playlist(iid, "PL1", "Mix", len(spec), "h", 1.0)
    ids = []
    for title, artist, genre, year in spec:
        tid = store.upsert_track(f"v{title}", title, artist, "Alb", 200)
        if genre:
            store.set_track_genre(tid, genre)
        if year:
            store.set_track_year(tid, str(year))
        ids.append(tid)
    store.set_playlist_tracks(pid, ids)
    return recommend.attach_genres(store, [
        {"key": f"{t.lower()}|{a.lower()}", "title": t, "artist": a} for t, a, _, _ in spec])


def test_it_names_the_genres_and_decades_actually_present():
    s = _store()
    items = _tracks(s, [("A", "X", "Indie Rock", 2014), ("B", "Y", "Indie Rock", 2016),
                        ("C", "Z", "Post-Rock", 2021), ("D", "W", "Post-Rock", 2022)])

    said = recommend.theme_sentence(s, "explore", items)

    assert said == ("Songs you own but hardly ever play, indie rock and post-rock "
                    "from the 2010s and 2020s.")


def test_one_dominant_genre_reads_as_mostly():
    s = _store()
    items = _tracks(s, [("A", "X", "Trance", 2011), ("B", "Y", "Trance", 2012),
                        ("C", "Z", "Trance", 2013), ("D", "W", "Ambient", 2014)])

    assert "mostly trance" in recommend.theme_sentence(s, "wheelhouse", items)


def test_the_lane_still_says_what_these_songs_are_to_you():
    s = _store()
    items = _tracks(s, [("A", "X", "Techno", 2001)] * 1)

    assert recommend.theme_sentence(s, "comfort", items).startswith(
        "Favorites you haven't reached for lately")
    assert recommend.theme_sentence(s, "fresh", items).startswith("Songs you don't own yet")
    assert recommend.theme_sentence(s, "somelane", items).startswith("Songs picked for you")


def test_a_thinly_tagged_card_claims_no_genre():
    """Three tagged tracks out of twelve are not "mostly techno", they're three tagged tracks."""
    s = _store()
    spec = [("A", "X", "Techno", 2001), ("B", "Y", "Techno", 2002), ("C", "Z", "Techno", 2003)]
    spec += [(f"U{i}", f"A{i}", None, None) for i in range(9)]
    items = _tracks(s, spec)

    said = recommend.theme_sentence(s, "explore", items)

    assert said == "Songs you own but hardly ever play."


def test_a_card_spread_across_many_genres_names_none_of_them():
    s = _store()
    items = _tracks(s, [("A", "X", "Techno", 2001), ("B", "Y", "Jazz", 2002),
                        ("C", "Z", "Punk", 2003), ("D", "W", "Folk", 2004),
                        ("E", "V", "Soul", 2005)])

    said = recommend.theme_sentence(s, "explore", items)

    assert "techno" not in said                      # no genre reaches the floor
    assert said.startswith("Songs you own but hardly ever play")


def test_an_empty_card_still_reads_as_a_sentence():
    assert recommend.theme_sentence(_store(), "wheelhouse", []) == "Songs close to what you play most."


def test_the_fresh_card_describes_itself_from_what_the_track_carries():
    """Fresh proposals aren't in your library, so nothing downstream can look their genre or year
    up - they carry both, and the card would otherwise say only "songs you don't own yet"."""
    s = _store()
    items = [{"key": "a|x", "title": "A", "artist": "X", "genre": "Post-Punk", "year": 2019},
             {"key": "b|y", "title": "B", "artist": "Y", "genre": "Post-Punk", "year": 2021}]

    said = recommend.theme_sentence(s, "fresh", items)

    assert said == ("Songs you don't own yet that fit your taste, mostly post-punk "
                    "from the 2010s and 2020s.")


def test_an_untagged_track_borrows_its_artists_genre():
    """Enrichment lags the library and never reaches the discovery pool, so most Fresh tracks have no
    genre of their own - but they're usually by artists you already own."""
    s = _store()
    _tracks(s, [("Owned", "Underworld", "Techno", 2000)])       # the library knows this artist
    items = [{"key": "new|underworld", "title": "New", "artist": "Underworld", "year": 2019},
             {"key": "new2|underworld", "title": "New2", "artist": "Underworld", "year": 2019}]

    assert "techno" in recommend.theme_sentence(s, "fresh", items)


def test_an_unknown_artist_borrows_nothing():
    s = _store()
    items = [{"key": "n|q", "title": "N", "artist": "Nobody At All", "year": 2019}]

    said = recommend.theme_sentence(s, "fresh", items)

    # No genre clause, so the era follows the lead directly.
    assert said == "Songs you don't own yet that fit your taste, from the 2010s."


# --- the row's themes: four cards should explore four corners, not one ---

def _seeded_row(store, pool, now=1000.0):
    """The four cards' themes, each rolled from the pool it would have to fill."""
    from yt_playlist.web.routes import home
    return {lane: home._rolled_theme(store, lane, pool, now) for lane in home._ROW_LANES}


def _pool(spec):
    """Candidate items as a card sees them: they carry their own genre and year."""
    return [{"key": f"t{i}|a{i}", "title": f"T{i}", "artist": f"A{i}", "genre": g, "year": y}
            for i, (g, y) in enumerate(spec)]


def _library(store, spec):
    iid = store.upsert_identity("main", "cred", None, True)
    pid = store.upsert_playlist(iid, "PL1", "Mix", len(spec), "h", 1.0)
    ids = []
    for i, (genre, year) in enumerate(spec):
        tid = store.upsert_track(f"v{i}", f"T{i}", f"A{i}", "Alb", 200)
        store.set_track_genre(tid, genre)
        store.set_track_year(tid, str(year))
        ids.append(tid)
        store.record_history_plays(iid, 1000.0, [f"t{i}|a{i}"])
    store.set_playlist_tracks(pid, ids)


def test_the_four_cards_do_not_all_roll_the_same_theme():
    """Seeded by the rotation epoch alone, every card drew the identical random sequence - four
    cards, one theme. Which card you reach for is only signal if they differ."""
    s = _store()
    _library(s, [("Techno", 2001)] * 6 + [("Indie Rock", 2011)] * 6 +
                [("Jazz", 1991)] * 6 + [("Soul", 1981)] * 6)

    pool = _pool([("Techno", 2001)] * 6 + [("Indie Rock", 2011)] * 6 +
                 [("Jazz", 1991)] * 6 + [("Soul", 1981)] * 6)
    themes = _seeded_row(s, pool)
    genres = [t["facets"].get("genres", [None])[0] for t in themes.values()]

    assert len(set(genres)) > 1, f"all four cards rolled {genres[0]}"


def test_a_theme_is_never_built_on_a_one_track_tag():
    """Avoiding the big families pushes the roll into the tail, which is mostly junk tags carried in
    by a single track ("other:dj"). A theme has to be something you actually have a body of."""
    s = _store()
    _library(s, [("Techno", 2001)] * 40 + [("House", 2002)] * 40 + [("Likedis Auto", 2003)])

    pool = _pool([("Techno", 2001)] * 40 + [("House", 2002)] * 40 + [("Likedis Auto", 2003)])
    for theme in _seeded_row(s, pool).values():
        for fam in theme["facets"].get("genres", []):
            assert "likedis" not in fam


def test_fresh_items_are_focused_on_the_rolled_theme():
    """Fresh rolled a theme and then ignored it: theme_filter read `.key` with getattr (always empty
    on the dicts Fresh uses) and looked genres up in the library, which knows nothing about a track
    you don't own."""
    s = _store()
    items = [{"key": "a|x", "title": "A", "artist": "X", "genre": "Techno", "year": 2001},
             {"key": "b|y", "title": "B", "artist": "Y", "genre": "Jazz", "year": 1991}]

    out = recommend.theme_filter(s, items, {"genres": [recommend.genre_map.family("Jazz")]})

    assert out[0]["title"] == "B"          # the themed one leads
    assert len(out) == 2                    # and the rest still follow, so the card fills


def test_the_theme_chooses_the_cards_tracks_not_just_their_order():
    """The slice used to be taken BEFORE the theme was rolled, so the theme could only reorder twelve
    tracks chosen without it - every refresh returned the pool's dominant genre whatever rolled."""
    from yt_playlist.web.routes import home
    s = _store()
    # A pool dominated by one genre, with a real seam of another: the theme has to be able to reach it.
    pool = _pool([("Trance", 2011)] * 50 + [("Jazz", 1991)] * 10)

    card = home._carded(s, "explore", "From your catalog", pool, 1000.0)
    themed = card["recipe"]["facets"].get("genres", [None])[0]
    got = [t["genre"] for t in card["tracks"]]

    assert themed in (recommend.genre_map.family("Trance"), recommend.genre_map.family("Jazz"))
    want = "Jazz" if themed == recommend.genre_map.family("Jazz") else "Trance"
    assert got.count(want) >= len(got) // 2, f"theme {themed} didn't reach the card: {got}"


def test_refreshing_a_card_actually_changes_it():
    """Refresh advances the card's rotation epoch; the tracks have to follow."""
    from yt_playlist.rec.rec_dao import RecDao
    from yt_playlist.web.routes import home
    s = _store()
    pool = _pool([("Trance", 2011)] * 30 + [("House", 2001)] * 30)

    seen = []
    for _ in range(3):
        RecDao(s).refresh_card("explore", 3, 1000.0)
        card = home._carded(s, "explore", "From your catalog", pool, 1000.0)
        seen.append(tuple(t["key"] for t in card["tracks"]))

    assert len(set(seen)) == 3
