"""Identity joins, provider consistency and non-destructive vocabulary upgrades."""
import json
import sqlite3

import pytest

from yt_playlist.providers import genres, musicbrainz
from yt_playlist.util import genre_catalog as catalog, genre_map


@pytest.fixture(autouse=True)
def isolated_matcher(monkeypatch):
    monkeypatch.setattr(genres, "_active", None)


def test_equivalent_spellings_share_identity():
    for tag in ["Hip-Hop", "hip hop", "hiphop", "rap", "Ｈｉｐ　Ｈｏｐ"]:
        assert genres.match_tag(tag) == "Hip Hop"
    assert genres.match_tag("drum and bass") == "Drum & Bass"
    assert genre_map.family("dnb") == "dnb"
    assert genre_map.subgenre("rap") == genre_map.subgenre("Hip-Hop") == "hip hop"


@pytest.mark.parametrize("left,right", [
    ("Afrobeat", "Afrobeats"), ("Hyper Techno", "Hypertechno"), ("Metal", "Heavy Metal"),
    ("Electronic", "Electro"), ("Electronic", "Electronica"), ("Acid", "Acid House"),
    ("Minimal", "Minimal Techno"), ("Christian", "Christian Rock"), ("Punk", "Punk Rock"),
])
def test_distinct_primary_names_survive_old_overbroad_aliases(left, right):
    assert genres.match_tag(left) == left
    assert genres.match_tag(right) == right
    assert catalog.normalize(left) != catalog.normalize(right)


def test_removing_genre_does_not_fall_back_to_old_broad_alias():
    genres.set_active(["Electronic", "Afrobeat", "Minimal", "Hip Hop"])
    assert genres.match_tag("electro") is None
    assert genres.match_tag("afrobeats") is None
    assert genres.match_tag("minimal techno") is None
    assert genres.match_tag("rap") == "Hip Hop"
    assert genres.build_canon(["Garage", "Garage Rock"]) == genres.build_canon(["Garage Rock", "Garage"])


def test_full_musicbrainz_snapshot_and_sourced_database_joins(store):
    genres.configure(store)
    raw = json.loads((catalog._DATA / "musicbrainz_genres.json").read_text())
    assert len(raw["genres"]) == raw["genre_count"] == 2216
    for row in raw["genres"]:
        assert genres.match_tag(row["name"]), row["name"]
    rows = store.conn.execute(
        "SELECT c.name,e.external_id,c.family_key,a.source FROM genre_catalog c "
        "JOIN genre_external_id e ON e.genre_key=c.key "
        "JOIN genre_alias a ON a.genre_key=c.key WHERE a.label_key='drum and bass'").fetchall()
    assert len(rows) == 1
    assert rows[0]["name"] == "Drum & Bass"
    assert rows[0]["family_key"] == "dnb"
    assert rows[0]["source"] == "musicbrainz"
    assert store.conn.execute("SELECT count(*) FROM genre_external_id").fetchone()[0] == 2216


def test_legacy_upgrade_preserves_additions_removals_and_splits_disabled_aliases(store):
    store.genres.set_genres([n for n in catalog.legacy_names() if n not in ("Rock", "Electronic")]
                     + ["Whack-a-doodle", "hip-hop"])
    store.settings.set_setting("genres_seeded", "1")
    track = store.tracks.upsert_track("v", "song", "artist", None, None)
    store.tracks.set_track_genre(track, "hip-hop")
    genres.configure(store)
    names = store.genres.get_genre_whitelist()
    assert "Rock" not in names and "Electronic" not in names
    assert "Electro" not in names and "Electronica" not in names
    assert "Phonk" in names and "Whack-a-doodle" in names
    assert names.count("Hip Hop") == 1 and "hip-hop" not in names
    assert store.conn.execute("SELECT genre FROM tracks WHERE id=?", (track,)).fetchone()[0] == "hip-hop"
    store.genres.remove_genre("Phonk")
    genres.configure(store)
    assert genres.match_tag("phonk") is None
    genres.reset(store)
    assert genres.match_tag("Rock") == "Rock"
    assert genres.match_tag("phonk") == "Phonk"
    assert genres.match_tag("Whack-a-doodle") is None


def test_explicitly_empty_legacy_list_stays_empty(store):
    store.settings.set_setting("genres_seeded", "1")
    genres.configure(store)
    assert store.genres.get_genre_whitelist() == []
    assert genres.match_tag("rock") is None


def test_later_snapshot_adds_only_new_defaults(store):
    genres.configure(store)
    store.genres.remove_genre("Rock")
    # Simulate an older baseline that did not include Phonk, with a user who removed Jazz.
    store.genres.remove_genre("Phonk")
    store.genres.remove_genre("Jazz")
    baseline = [n for n in catalog.builtin_names() if n != "Phonk"]
    store.settings.set_setting("genre_defaults", json.dumps(baseline))
    store.settings.set_setting("genre_catalog_revision", "older")
    genres.configure(store)
    assert "Phonk" in store.genres.get_genre_whitelist()
    assert "Rock" not in store.genres.get_genre_whitelist()
    assert "Jazz" not in store.genres.get_genre_whitelist()


def test_failed_upgrade_rolls_back_catalog_enabled_list_and_revision(store):
    store.genres.set_genres(["Jazz", "Whack-a-doodle"])
    store.settings.set_setting("genres_seeded", "1")
    store.conn.execute("CREATE TRIGGER reject_catalog BEFORE INSERT ON genre_alias "
                       "BEGIN SELECT RAISE(ABORT, 'injected failure'); END")
    with pytest.raises(sqlite3.IntegrityError, match="injected failure"):
        genres.configure(store)
    assert store.genres.get_genre_whitelist() == ["Jazz", "Whack-a-doodle"]
    assert store.settings.get_setting("genre_catalog_revision") is None
    assert store.conn.execute("SELECT count(*) FROM genre_catalog").fetchone()[0] == 0


def test_musicbrainz_skips_noisy_or_disabled_top_tags_and_keeps_relevance():
    genres.set_active(["Jazz", "Drum & Bass"])
    assert musicbrainz._top_label({"genres": [{"name": "rock", "count": 100},
                                             {"name": "drum and bass", "count": 3},
                                             {"name": "jazz", "count": 2}]}) == "Drum & Bass"
    assert musicbrainz._top_label({"tags": [{"name": "seen live", "count": 100},
                                           {"name": "jazz", "count": 5}]}) == "Jazz"
    assert musicbrainz._top_label({"tags": [{"name": "seen live", "count": 100}]}) is None


def test_musicbrainz_cached_artist_rechecks_edited_vocabulary(monkeypatch):
    calls = []
    monkeypatch.setattr(musicbrainz, "_artist_genre_cache", {})
    def lookup(*args):
        calls.append(args)
        return {"genres": [{"name": "hip-hop", "count": 10}, {"name": "jazz", "count": 5}]}
    monkeypatch.setattr(musicbrainz, "_get", lookup)
    genres.set_active(["Hip Hop", "Jazz"])
    assert musicbrainz._artist_genre("artist") == "Hip Hop"
    genres.set_active(["Jazz"])
    assert musicbrainz._artist_genre("artist") == "Jazz"
    assert len(calls) == 1


def test_imported_children_use_the_same_family_in_matching_and_recommendations():
    assert genres.match_tag("technical death metal") == "Technical Death Metal"
    assert genre_map.family("technical death metal") == "metal"
    assert genre_map.subgenre("technical death metal") == "technical death metal"
    record = catalog.catalog()[0][catalog.normalize("technical death metal")]
    assert record["family_source"] == "musicbrainz-parent"
    assert genre_map.family("Afrobeat") == genre_map.family("Afrobeats") == "world-latin"


def test_parent_projection_handles_depth_ties_cycles_and_order(tmp_path, monkeypatch):
    names = ["metal", "pop", "branch", "deep child", "mixed child", "pop branch",
             "nearest child", "cycle a", "cycle b"]
    rows = [{"id": str(i), "name": name, "aliases": []} for i, name in enumerate(names)]
    rows[0]["aliases"] = ["shared"]
    rows[1]["aliases"] = ["shared", "metal"]
    edges = [["branch", "metal"], ["deep child", "branch"],
             ["mixed child", "metal"], ["mixed child", "pop"],
             ["pop branch", "pop"], ["nearest child", "metal"], ["nearest child", "pop branch"],
             ["cycle a", "cycle b"], ["cycle b", "cycle a"]]
    (tmp_path / "genre_families.json").write_text(json.dumps({"families": {
        "metal": ["metal"], "pop": ["pop"]}}))
    monkeypatch.setattr(catalog, "_DATA", tmp_path)
    results = []
    try:
        for genres_rows, parent_edges in [(rows, edges), (list(reversed(rows)), list(reversed(edges)))]:
            (tmp_path / "musicbrainz_genres.json").write_text(json.dumps({"genres": genres_rows}))
            (tmp_path / "musicbrainz_genre_parents.json").write_text(json.dumps({"parents": parent_edges}))
            catalog.catalog.cache_clear()
            records, labels = catalog.catalog()
            assert "shared" not in labels                 # ambiguous equivalent name
            assert labels["metal"][0] == "metal"          # primary beats another genre's alias
            assignments = {name: records[name]["family"] for name in names}
            assert assignments["deep child"] == "metal"   # two parent edges
            assert assignments["mixed child"] is None     # equally near, different families
            assert assignments["nearest child"] == "metal"
            assert assignments["cycle a"] is None
            assert assignments["cycle b"] is None
            results.append(assignments)
        assert results[0] == results[1]
    finally:
        catalog.catalog.cache_clear()
