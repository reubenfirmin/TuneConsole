"""store.search.omni_search: navbar omnisearch over the user's library.

LinkedIn-style: an artist match pivots into that artist's tracks/albums/playlists;
direct title matches on songs and playlists get their own sections.
"""


def _seed(store):
    iid = store.identities.upsert_identity("main", "cred", None, True)
    # Ritmo: 3 tracks across 2 albums; Mogwai: 1 track. One track titled "Ritmo Caliente".
    r1 = store.tracks.upsert_track("r1", "Spektrum", "Ritmo", "Ritmo LP", None,
                            album_browse_id="MPREb_ritmolp", thumbnail="http://t/r1.jpg")
    r2 = store.tracks.upsert_track("r2", "Pranava", "Ritmo", "Ritmo LP", None,
                            album_browse_id="MPREb_ritmolp")
    r3 = store.tracks.upsert_track("r3", "Cumbre", "Ritmo", "Sierra", None,
                            album_browse_id="MPREb_sierra")
    m1 = store.tracks.upsert_track("m1", "Ritmo Caliente", "Mogwai", "Hardcore", None)
    pl = store.playlists.upsert_playlist(iid, "PL1", "Late Night Drive", 2, "h", 0.0)
    named = store.playlists.upsert_playlist(iid, "PL2", "Ritmo Mix", 1, "h2", 0.0)
    store.playlists.set_playlist_tracks(pl, [r1, m1])
    store.playlists.set_playlist_tracks(named, [r3])
    return iid


def test_short_query_is_empty(store):
    _seed(store)
    res = store.search.omni_search("r")
    assert res == {"query": "r", "primary_artist": None, "sections": []}


def test_artist_pivot_sets_primary_and_sections(store):
    _seed(store)
    res = store.search.omni_search("ritmo")
    assert res["primary_artist"]["name"] == "Ritmo"          # most tracks => primary
    kinds = [s["kind"] for s in res["sections"]]
    assert "artist" in kinds
    assert "tracks_by" in kinds
    assert "albums_by" in kinds
    assert "playlists_featuring" in kinds


def test_tracks_by_artist_link_to_track_details(store):
    _seed(store)
    res = store.search.omni_search("ritmo")
    tracks = next(s for s in res["sections"] if s["kind"] == "tracks_by")
    assert tracks["title"] == "Tracks by Ritmo"
    row = tracks["rows"][0]
    assert row["external"] is False
    assert row["href"].startswith("/track/")


def test_albums_by_artist_link_to_album_page(store):
    _seed(store)
    res = store.search.omni_search("ritmo")
    albums = next(s for s in res["sections"] if s["kind"] == "albums_by")
    hrefs = {r["href"] for r in albums["rows"]}
    assert "/album?browse=MPREb_ritmolp" in hrefs
    assert "/album?browse=MPREb_sierra" in hrefs


def test_playlists_featuring_artist_link_to_playlist(store):
    _seed(store)
    res = store.search.omni_search("ritmo")
    pls = next(s for s in res["sections"] if s["kind"] == "playlists_featuring")
    assert pls["rows"][0]["href"].startswith("/playlist/")


def test_direct_song_title_match_appears_in_songs(store):
    _seed(store)
    # "caliente" matches only the Mogwai track title, not any artist.
    res = store.search.omni_search("caliente")
    assert res["primary_artist"] is None
    songs = next(s for s in res["sections"] if s["kind"] == "songs")
    assert songs["rows"][0]["label"] == "Ritmo Caliente"
    assert songs["rows"][0]["href"] == "/track/m1"
    assert songs["rows"][0]["external"] is False


def test_direct_playlist_title_match_appears_in_playlists(store):
    _seed(store)
    res = store.search.omni_search("mix")        # matches playlist "Ritmo Mix" by title, no artist "mix"
    pls = next(s for s in res["sections"] if s["kind"] == "playlists")
    assert pls["rows"][0]["label"] == "Ritmo Mix"


def test_artist_row_links_to_artist_page(store):
    _seed(store)
    res = store.search.omni_search("ritmo")
    artist = next(s for s in res["sections"] if s["kind"] == "artist")
    artist_id = store.artists.resolve("Ritmo")
    assert artist["rows"][0]["href"] == f"/artist?artist_id={artist_id}"
    assert artist["rows"][0]["artist_id"] == artist_id
    assert res["primary_artist"]["artist_id"] == artist_id


def _section(result, kind):
    return next((s["rows"] for s in result["sections"] if s["kind"] == kind), [])


def test_same_name_artists_keep_distinct_links_and_primary_pivots(store):
    iid = store.identities.upsert_identity("main", "cred", None, True)
    entries = []
    for video, title, provider in [("one", "First song", "channel1"),
                                   ("two", "Second song", "channel2"),
                                   ("extra", "First artist encore", "channel1")]:
        tid = store.tracks.upsert_track(video, title, "Homonym", provider, 200,
            album_browse_id=provider, credits=[{"name": "Homonym", "id": provider}])
        entries.append(tid)
    first, second = [store.artists.track_credits(t)[0]["artist_id"] for t in entries[:2]]
    playlists = []
    for title, tid in [("First playlist", entries[0]), ("Second playlist", entries[1])]:
        pid = store.playlists.upsert_playlist(iid, title, title, 1, "h", 0)
        store.playlists.set_playlist_tracks(pid, [tid])
        playlists.append(pid)

    result = store.search.omni_search("homonym")
    artists = _section(result, "artist")
    assert [(r["label"], r["artist_id"], r["href"]) for r in artists] == [
        ("Homonym", first, f"/artist?artist_id={first}"),
        ("Homonym", second, f"/artist?artist_id={second}")]
    assert store.artists.resolve("Homonym") is None
    for row, expected in zip(artists, ["First song", "Second song"]):
        assert store.artists.get_artist(row["artist_id"])["name"] == "Homonym"
        assert expected in {s["title"] for s in store.charts.artist_songs(row["artist_id"])}
    assert result["primary_artist"]["artist_id"] == first
    assert {r["label"] for r in _section(result, "tracks_by")} == {"First song", "First artist encore"}
    assert [r["href"] for r in _section(result, "albums_by")] == ["/album?browse=channel1"]
    assert [r["href"] for r in _section(result, "playlists_featuring")] == [f"/playlist/{playlists[0]}"]


def test_collaboration_is_found_under_each_ordered_artist_credit(store):
    iid = store.identities.upsert_identity("main", "cred", None, True)
    tid = store.tracks.upsert_track("duet", "Together", "Alpha & Beta", "Joint album", 200,
        album_browse_id="joint", credits=[{"name": "Alpha", "id": "a"}, {"name": "Beta", "id": "b"}])
    pid = store.playlists.upsert_playlist(iid, "joint-playlist", "Duets", 1, "h", 0)
    store.playlists.set_playlist_tracks(pid, [tid])
    for name in ["Alpha", "Beta"]:
        result = store.search.omni_search(name)
        assert [r["label"] for r in _section(result, "artist")] == [name]
        assert [r["label"] for r in _section(result, "tracks_by")] == ["Together"]
        assert _section(result, "tracks_by")[0]["sub"] == "Alpha & Beta"
        assert [r["href"] for r in _section(result, "albums_by")] == ["/album?browse=joint"]
        assert [r["href"] for r in _section(result, "playlists_featuring")] == [f"/playlist/{pid}"]
    assert not _section(store.search.omni_search("Alpha & Beta"), "artist")


def test_artist_alias_matches_preserve_canonical_identity_and_label(store):
    tid = store.tracks.upsert_track("v", "Song", "Canonical", None, 200,
        credits=[{"name": "Canonical", "id": "channel"}])
    artist_id = store.artists.track_credits(tid)[0]["artist_id"]
    store.artists.ensure_artist("Alias 100%", provider="youtube", provider_id="channel")
    result = store.search.omni_search("Alias 100%")
    assert result["primary_artist"]["artist_id"] == artist_id
    assert [r["label"] for r in _section(result, "artist")] == ["Canonical"]
    assert [r["label"] for r in _section(result, "tracks_by")] == ["Song"]
    assert not _section(store.search.omni_search("Alias 100_"), "artist")


def test_saved_album_credits_are_searchable_without_library_tracks(store):
    store.collection.add_saved_album({"browse": "saved", "title": "Saved duet", "artist": "Alpha & Beta",
        "credits": [{"name": "Alpha", "id": "a"}, {"name": "Beta", "id": "b"}]})
    store.artists.ensure_artist("Uncollected artist", provider="youtube", provider_id="uncollected")
    for name in ["Alpha", "Beta"]:
        result = store.search.omni_search(name)
        assert result["primary_artist"]["name"] == name
        assert [r["href"] for r in _section(result, "albums_by")] == ["/album?browse=saved"]
        assert not _section(result, "tracks_by")
    assert not _section(store.search.omni_search("Uncollected"), "artist")


def test_reconciled_artist_search_uses_surviving_identity_only(store):
    tid = store.tracks.upsert_track("old", "Old song", "Alias", None, 200)
    old = store.artists.track_credits(tid)[0]["artist_id"]
    known = store.tracks.upsert_track("known", "Known song", "Canonical", None, 200,
        credits=[{"name": "Canonical", "id": "channel"}])
    target = store.artists.track_credits(known)[0]["artist_id"]
    store.tracks.upsert_track("old", "Old song", "Alias", None, 200,
        credits=[{"name": "Alias", "id": "channel"}])
    assert store.artists.resolve(old) == target
    result = store.search.omni_search("Alias")
    assert [r["artist_id"] for r in _section(result, "artist")] == [target]
    assert {r["label"] for r in _section(result, "tracks_by")} == {"Old song", "Known song"}


def test_sampling_is_stable_per_query(store):
    _seed(store)
    # Add extra Ritmo tracks so the "tracks_by" section has > 5 candidates,
    # forcing _sample to take the seeded random.Random(...).sample() branch.
    for i in range(8):
        store.tracks.upsert_track(
            f"extra{i}", f"Extra Track {i}", "Ritmo", "Extra LP", None,
            album_browse_id="MPREb_extralp",
        )
    # Two identical queries must return identical results (seeded sampling).
    a = store.search.omni_search("ritmo")
    b = store.search.omni_search("ritmo")
    assert a == b                          # same query => identical result, no reshuffle

    # Verify sampling actually happened: tracks_by section has exactly per_section=5
    # rows even though more than 5 candidates exist (3 from _seed + 8 added = 11).
    tracks_by = next(s for s in a["sections"] if s["kind"] == "tracks_by")
    assert len(tracks_by["rows"]) == 5, (
        "Expected exactly 5 rows (per_section limit); sampling branch was not exercised"
    )


def test_generated_playlists_excluded(store):
    iid = _seed(store)
    gen = store.playlists.upsert_playlist(iid, "PLGEN", "Ritmo Generated", 1, "h3", 0.0)
    r = store.tracks.upsert_track("g1", "Auto", "Ritmo", "Sierra", None)
    store.playlists.set_playlist_tracks(gen, [r])
    store.playlists.set_playlist_group("PLGEN", "Generated")
    store.playlists.mark_generated_playlist('PLGEN')
    res = store.search.omni_search("ritmo")
    labels = [row["label"] for s in res["sections"]
              for row in s["rows"] if s["kind"] in ("playlists", "playlists_featuring")]
    assert "Ritmo Generated" not in labels


def test_like_wildcards_are_escaped(store):
    iid = store.identities.upsert_identity("main", "cred", None, True)
    store.tracks.upsert_track("x1", "100%", "Sleaford Mods", "", None)
    store.tracks.upsert_track("x2", "anything", "Other", "", None)
    res = store.search.omni_search("100%")        # the % must be literal, not "match-all"
    songs = next((s for s in res["sections"] if s["kind"] == "songs"), None)
    labels = [r["label"] for r in songs["rows"]] if songs else []
    assert labels == ["100%"]


def test_cross_section_dedup(store):
    """Tracks and playlists shown under the artist pivot must not reappear in direct-match sections."""
    iid = store.identities.upsert_identity("main", "cred", None, True)

    # "Zola" is the primary artist (most tracks => pivot artist).
    # Track "zola anthem" by Zola: matches the query "zola" both as artist AND by title.
    t_overlap = store.tracks.upsert_track("z1", "zola anthem", "Zola", "LP", None,
                                   album_browse_id="MPREb_zola")
    # A second Zola track (no title match) just to make Zola the clear primary.
    store.tracks.upsert_track("z2", "side b", "Zola", "LP", None,
                       album_browse_id="MPREb_zola")
    # Playlist titled "Zola Sessions" that features Zola (contains t_overlap):
    # matches "zola" both under "Playlists featuring" AND by title.
    pl_overlap = store.playlists.upsert_playlist(iid, "PLZ", "Zola Sessions", 1, "hz", 0.0)
    store.playlists.set_playlist_tracks(pl_overlap, [t_overlap])

    res = store.search.omni_search("zola")
    assert res["primary_artist"]["name"] == "Zola"

    # Collect identity_keys in "tracks_by" and "songs" sections.
    tracks_by_section = next((s for s in res["sections"] if s["kind"] == "tracks_by"), None)
    songs_section = next((s for s in res["sections"] if s["kind"] == "songs"), None)

    # The overlapping track must appear in tracks_by (it is by Zola).
    assert tracks_by_section is not None
    tracks_by_hrefs = {r["href"] for r in tracks_by_section["rows"]}
    assert any("z1" in href for href in tracks_by_hrefs), (
        "Overlapping track should be in tracks_by section"
    )

    # The same track must NOT appear in the direct songs section.
    if songs_section:
        songs_hrefs = {r["href"] for r in songs_section["rows"]}
        assert not any("z1" in href for href in songs_hrefs), (
            "Overlapping track should be de-duped out of songs section"
        )

    # Collect pids in "playlists_featuring" and "playlists" sections.
    featuring_section = next((s for s in res["sections"] if s["kind"] == "playlists_featuring"), None)
    playlists_section = next((s for s in res["sections"] if s["kind"] == "playlists"), None)

    # The overlapping playlist must appear under playlists_featuring.
    assert featuring_section is not None
    featuring_pids = {r["href"].rsplit("/", 1)[1] for r in featuring_section["rows"]}
    assert str(pl_overlap) in featuring_pids, (
        "Overlapping playlist should be in playlists_featuring section"
    )

    # The same playlist must NOT appear in the direct playlists section.
    if playlists_section:
        playlists_pids = {r["href"].rsplit("/", 1)[1] for r in playlists_section["rows"]}
        assert str(pl_overlap) not in playlists_pids, (
            "Overlapping playlist should be de-duped out of direct playlists section"
        )
