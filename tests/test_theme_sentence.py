# tests/test_theme_sentence.py
"""Each generated card says what is actually in it.

A card is a lane AND a rolled theme, so two "More in your wheelhouse" cards can be different music
entirely - and the old fixed per-lane line ("Deeper into what you already love") described neither.
The sentence is built from the tracks that are there, and drops any clause it can't support.
"""
from collections import Counter

import pytest

from tests.repository_mock_support import no_database, transient_repository_store
from yt_playlist.rec import ordering, recipes
from yt_playlist.util import genre_map


@pytest.fixture
def description_store(no_database):
    return transient_repository_store(
        rec_query={'artist_genre_years_for': {}, 'genre_play_distribution': {},
                   'era_play_distribution': {}, 'track_last_played': {}, 'play_counts': {}},
        rec_ledger={'card_views': 0},
        musical_profiles={'by_keys': {}},
        genres={'feature_axes': {'Techno': ('techno', None), 'House': ('house', None),
                                 'Indie Rock': ('rock-indie', None), 'Jazz': ('jazz', None),
                                 'Soul': ('soul-funk', None), 'Trance': ('trance', None),
                                 'Likedis Auto': ('other:likedis auto', None)}},
    )


def test_mix_heading_separates_the_reported_long_title_into_name_and_era(description_store):
    store = description_store
    items = _tracks(store, [
        (str(i), f'Artist {i}', 'Drum & Bass' if i < 8 else 'Ambient',
         2014 if i < 5 else 2022 if i < 9 else 1994)
        for i in range(11)])
    assert recipes.theme_heading(store, 'explore', items) == {
        'name': 'Rediscover your drum & bass',
        'context': 'Mostly from the 2010s and 2020s'}


def test_mix_heading_does_not_name_a_sparse_catalog_after_its_known_minority(description_store):
    store = description_store
    items = _tracks(store, [
        (str(i), f'Artist {i}', 'Techno' if i < 3 else None, 2001 if i < 3 else None)
        for i in range(12)])
    assert recipes.theme_heading(store, 'explore', items) == {
        'name': 'Worth another listen', 'context': None}
    assert recipes.theme_heading(store, 'fresh', []) == {
        'name': 'Find your next favorite', 'context': None}


def _tracks(store, spec):
    """Repository metadata for cards whose prose is the behavior under test."""
    store.rec_query.track_genres.return_value = {
        f'{title.lower()}|{artist.lower()}': genre for title, artist, genre, _ in spec if genre}
    store.rec_query.track_decades.return_value = {
        f'{title.lower()}|{artist.lower()}': str(year // 10 * 10)
        for title, artist, _, year in spec if year}
    return ordering.attach_genres(store, [
        {'key': f'{title.lower()}|{artist.lower()}', 'title': title, 'artist': artist}
        for title, artist, _, _ in spec])


def _owned_tracks(store, spec):
    """spec: (title, artist, genre, year) tuples -> genre-attached items, as a card carries them."""
    iid = store.accounts.upsert_account("main", "cred", None, True)
    pid = store.playlists.upsert_playlist(iid, "PL1", "Mix", len(spec), "h", 1.0)
    ids = []
    for title, artist, genre, year in spec:
        tid = store.tracks.upsert_track(f"v{title}", title, artist, "Alb", 200)
        if genre:
            store.tracks.set_track_genre(tid, genre)
        if year:
            store.tracks.set_track_year(tid, str(year))
        ids.append(tid)
    store.playlists.set_playlist_tracks(pid, ids)
    return ordering.attach_genres(store, [
        {"key": f"{t.lower()}|{a.lower()}", "title": t, "artist": a} for t, a, _, _ in spec])


def test_it_names_the_genres_and_decades_actually_present(description_store):
    s = description_store
    items = _tracks(s, [("A", "X", "Indie Rock", 2014), ("B", "Y", "Indie Rock", 2016),
                        ("C", "Z", "Post-Rock", 2021), ("D", "W", "Post-Rock", 2022)])

    said = recipes.theme_sentence(s, "explore", items)

    assert said == ("Songs you own but hardly ever play, indie rock and post-rock "
                    "from the 2010s and 2020s.")


def test_one_dominant_genre_reads_as_mostly(description_store):
    s = description_store
    items = _tracks(s, [("A", "X", "Trance", 2011), ("B", "Y", "Trance", 2012),
                        ("C", "Z", "Trance", 2013), ("D", "W", "Ambient", 2014)])

    assert "mostly trance" in recipes.theme_sentence(s, "wheelhouse", items)


def test_the_lane_still_says_what_these_songs_are_to_you(description_store):
    s = description_store
    items = _tracks(s, [("A", "X", "Techno", 2001)] * 1)

    assert recipes.theme_sentence(s, "comfort", items).startswith(
        "Favorites you haven't reached for lately")
    assert recipes.theme_sentence(s, "fresh", items).startswith("Songs you don't own yet")
    assert recipes.theme_sentence(s, "somelane", items).startswith("Songs picked for you")


def test_a_thinly_tagged_card_claims_no_genre(description_store):
    """Three tagged tracks out of twelve are not "mostly techno", they're three tagged tracks."""
    s = description_store
    spec = [("A", "X", "Techno", 2001), ("B", "Y", "Techno", 2002), ("C", "Z", "Techno", 2003)]
    spec += [(f"U{i}", f"A{i}", None, None) for i in range(9)]
    items = _tracks(s, spec)

    said = recipes.theme_sentence(s, "explore", items)

    assert said == "Songs you own but hardly ever play."


def test_a_card_spread_across_many_genres_names_none_of_them(description_store):
    s = description_store
    items = _tracks(s, [("A", "X", "Techno", 2001), ("B", "Y", "Jazz", 2002),
                        ("C", "Z", "Punk", 2003), ("D", "W", "Folk", 2004),
                        ("E", "V", "Soul", 2005)])

    said = recipes.theme_sentence(s, "explore", items)

    assert "techno" not in said                      # no genre reaches the floor
    assert said.startswith("Songs you own but hardly ever play")


def test_an_empty_card_still_reads_as_a_sentence(description_store):
    assert recipes.theme_sentence(description_store, "wheelhouse", []) == "Songs close to what you play most."


def test_mixed_eras_and_adjacent_genres_do_not_overstate_the_theme(description_store):
    s = description_store
    items = _tracks(s, [("A", "A", "Funk", 2020), ("B", "B", "Soul", 2015),
                        ("C", "C", "Funk", 2018), ("D", "D", "Acid Jazz", 1995),
                        ("E", "E", "Acid Jazz", 2001), ("F", "F", "Soul", 1960)])

    assert recipes.theme_sentence(s, "wheelhouse", items) == (
        "Songs close to what you play most, with funk and soul.")


def test_unknown_metadata_does_not_turn_a_minority_into_mostly(description_store):
    s = description_store
    items = _tracks(s, [(f"T{i}", f"A{i}", "Funk" if i < 5 else None,
                         2012 if i < 5 else None) for i in range(12)])

    assert recipes.theme_sentence(s, "wheelhouse", items) == (
        "Songs close to what you play most, with funk.")


def test_the_fresh_card_describes_itself_from_what_the_track_carries(description_store):
    """Fresh proposals aren't in your library, so nothing downstream can look their genre or year
    up - they carry both, and the card would otherwise say only "songs you don't own yet"."""
    s = description_store
    items = [{"key": "a|x", "title": "A", "artist": "X", "genre": "Post-Punk", "year": 2019},
             {"key": "b|y", "title": "B", "artist": "Y", "genre": "Post-Punk", "year": 2021}]

    said = recipes.theme_sentence(s, "fresh", items)

    assert said == ("Songs you don't own yet that fit your taste, mostly post-punk "
                    "from the 2010s and 2020s.")


def test_an_untagged_track_borrows_its_artists_genre(store):
    """Enrichment lags the library and never reaches the discovery pool, so most Fresh tracks have no
    genre of their own - but they're usually by artists you already own."""
    s = store
    _owned_tracks(s, [("Owned", "Underworld", "Techno", 2000)])  # the library knows this artist
    items = [{"key": "new|underworld", "title": "New", "artist": "Underworld", "year": 2019},
             {"key": "new2|underworld", "title": "New2", "artist": "Underworld", "year": 2019}]

    assert "techno" in recipes.theme_sentence(s, "fresh", items)


def test_an_unknown_artist_borrows_nothing(description_store):
    s = description_store
    items = [{"key": "n|q", "title": "N", "artist": "Nobody At All", "year": 2019}]

    said = recipes.theme_sentence(s, "fresh", items)

    # No genre clause, so the era follows the lead directly.
    assert said == "Songs you don't own yet that fit your taste, from the 2010s."


# --- the row's themes: four cards should explore four corners, not one ---

def _seeded_row(store, pool, now=1000.0):
    """The four cards' themes, each rolled from the pool it would have to fill."""
    from yt_playlist.rec import home_cards as home
    return {lane: home._rolled_theme(store, lane, pool, now) for lane in home._ROW_LANES}


def _pool(spec):
    """Candidate items as a card sees them: they carry their own genre and year."""
    return [{"key": f"t{i}|a{i}", "title": f"T{i}", "artist": f"A{i}", "genre": g, "year": y}
            for i, (g, y) in enumerate(spec)]


def _library(store, spec):
    # Each original owned track had one play: mass two in each distribution.
    store.rec_query.genre_play_distribution.return_value = {
        genre: count * 2 for genre, count in Counter(g for g, _ in spec).items()}
    store.rec_query.era_play_distribution.return_value = {
        decade: count * 2 for decade, count in Counter(str(y // 10 * 10) for _, y in spec).items()}
    store.rec_query.track_genres.return_value = {f't{i}|a{i}': g for i, (g, _) in enumerate(spec)}
    store.rec_query.track_decades.return_value = {
        f't{i}|a{i}': str(y // 10 * 10) for i, (_, y) in enumerate(spec)}
    store.rec_query.track_artists.return_value = {f't{i}|a{i}': f'A{i}' for i in range(len(spec))}
    store.history.recent_plays_with_ts.return_value = [(f't{i}|a{i}', 1000.) for i in range(len(spec))]


def test_the_four_cards_do_not_all_roll_the_same_theme(description_store):
    """Seeded by the rotation epoch alone, every card drew the identical random sequence - four
    cards, one theme. Which card you reach for is only signal if they differ."""
    s = description_store
    _library(s, [("Techno", 2001)] * 6 + [("Indie Rock", 2011)] * 6 +
                [("Jazz", 1991)] * 6 + [("Soul", 1981)] * 6)

    pool = _pool([("Techno", 2001)] * 6 + [("Indie Rock", 2011)] * 6 +
                 [("Jazz", 1991)] * 6 + [("Soul", 1981)] * 6)
    themes = _seeded_row(s, pool)
    genres = [t["facets"].get("genres", [None])[0] for t in themes.values()]

    assert len(set(genres)) > 1, f"all four cards rolled {genres[0]}"


def test_a_theme_is_never_built_on_a_one_track_tag(description_store):
    """Avoiding the big families pushes the roll into the tail, which is mostly junk tags carried in
    by a single track ("other:dj"). A theme has to be something you actually have a body of."""
    s = description_store
    _library(s, [("Techno", 2001)] * 40 + [("House", 2002)] * 40 + [("Likedis Auto", 2003)])

    pool = _pool([("Techno", 2001)] * 40 + [("House", 2002)] * 40 + [("Likedis Auto", 2003)])
    for theme in _seeded_row(s, pool).values():
        for fam in theme["facets"].get("genres", []):
            assert "likedis" not in fam


def test_fresh_items_are_focused_on_the_rolled_theme(description_store):
    """Fresh rolled a theme and then ignored it: theme_filter read `.key` with getattr (always empty
    on the dicts Fresh uses) and looked genres up in the library, which knows nothing about a track
    you don't own."""
    s = description_store
    items = [{"key": "a|x", "title": "A", "artist": "X", "genre": "Techno", "year": 2001},
             {"key": "b|y", "title": "B", "artist": "Y", "genre": "Jazz", "year": 1991}]

    out = recipes.theme_filter(s, items, {"genres": [genre_map.family("Jazz")]})

    assert out[0]["title"] == "B"          # the themed one leads
    assert len(out) == 2                    # and the rest still follow, so the card fills


def test_the_theme_chooses_the_cards_tracks_not_just_their_order(description_store):
    """The slice used to be taken BEFORE the theme was rolled, so the theme could only reorder twelve
    tracks chosen without it - every refresh returned the pool's dominant genre whatever rolled."""
    from yt_playlist.rec import home_cards as home
    s = description_store
    # A pool dominated by one genre, with a real seam of another: the theme has to be able to reach it.
    pool = _pool([("Trance", 2011)] * 50 + [("Jazz", 1991)] * 10)

    card = home._carded(s, "explore", "From your catalog", pool, 1000.0)
    themed = card["recipe"]["facets"].get("genres", [None])[0]
    got = [t["genre"] for t in card["tracks"]]

    assert themed in (genre_map.family("Trance"), genre_map.family("Jazz"))
    want = "Jazz" if themed == genre_map.family("Jazz") else "Trance"
    assert got.count(want) >= len(got) // 2, f"theme {themed} didn't reach the card: {got}"


def test_refreshing_a_card_actually_changes_it(store):
    """Refresh advances the card's rotation epoch; the tracks have to follow."""
    from yt_playlist.rec import home_cards as home
    s = store
    pool = _pool([("Trance", 2011)] * 30 + [("House", 2001)] * 30)

    seen = []
    for _ in range(3):
        s.rec_ledger.refresh_card("explore", 3, 1000.0)
        card = home._carded(s, "explore", "From your catalog", pool, 1000.0)
        seen.append(tuple(t["key"] for t in card["tracks"]))

    assert len(set(seen)) == 3
