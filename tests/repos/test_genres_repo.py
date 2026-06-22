"""DAO suite for GenreRepo (genre whitelist + collected genres)."""


def test_whitelist_add_remove_set(store):
    store.genres.add_genre("Jazz"); store.genres.add_genre("Rock")
    assert store.genres.get_genre_whitelist() == ["Jazz", "Rock"]
    store.genres.remove_genre("Jazz")
    assert store.genres.get_genre_whitelist() == ["Rock"]
    store.genres.set_genres(["Pop", "ambient"])
    assert store.genres.get_genre_whitelist() == ["ambient", "Pop"]   # case-insensitive sort


def test_add_is_case_insensitively_idempotent(store):
    store.genres.add_genre("Jazz"); store.genres.add_genre("jazz")    # COLLATE NOCASE primary key
    assert store.genres.get_genre_whitelist() == ["Jazz"]


def test_all_genres_empty_without_track_genres(store):
    assert store.genres.all_genres() == []


def test_facade_delegates(store):
    store.add_genre("Jazz")                                           # legacy store.x() call site
    assert store.get_genre_whitelist() == ["Jazz"]
