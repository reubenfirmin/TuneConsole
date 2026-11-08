def test_discovered_genre_round_trip(file_backed_store):
    s = file_backed_store
    s.discovery.upsert_discovered_artist("Aphex Twin", 0.9, ["bridge"], ["fits"], "th", 100.0, genre="IDM")
    s.discovery.upsert_discovered_album("bid1", "Aphex Twin", "SAW II", "1994", "th", 100.0, genre="IDM")
    assert s.discovery.get_discovered_artists()[0]["genre"] == "IDM"
    assert s.discovery.get_discovered_albums()[0]["genre"] == "IDM"


def test_discovered_genre_defaults_none(file_backed_store):
    s = file_backed_store
    s.discovery.upsert_discovered_artist("Boards of Canada", 0.5, [], [], None, 1.0)
    assert s.discovery.get_discovered_artists()[0]["genre"] is None


def test_artist_genre_counts_recordings_per_genre(file_backed_store):
    s = file_backed_store
    i1 = s.tracks.upsert_track("v1", "T1", "Aphex Twin", None, None)
    i2 = s.tracks.upsert_track("v2", "T2", "Aphex Twin", None, None)
    i3 = s.tracks.upsert_track("v3", "T3", "Aphex Twin", None, None)
    s.tracks.set_track_genre(i1, "IDM"); s.tracks.set_track_genre(i2, "IDM"); s.tracks.set_track_genre(i3, "Ambient")
    counts = s.rec_query.repository.artist_genre_counts()
    assert {(row['artist'], row['genre']): row['c'] for row in counts} == {
        ('Aphex Twin', 'IDM'): 2, ('Aphex Twin', 'Ambient'): 1}
