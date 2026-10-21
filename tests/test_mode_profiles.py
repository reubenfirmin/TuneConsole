import json

import numpy as np
import pytest

from yt_playlist.rec import embed, mode_profiles, taste_viz


def _groups(store):
    model = {"cat": {"fam:house": 0, "dec:2010": 1, "dec:2020": 2},
             "ncat": 3, "cont": [["bpm", 120, 10], ["energy", .5, .2]]}
    store.settings.set_setting("rec_content_model", json.dumps(model))
    space = embed.content_space_id(store)
    centers = np.eye(5, dtype=np.float32)[:2]
    vectors, modes = [], []
    for group in range(2):
        keys = []
        for i in range(10):
            title = f"Group {group} track {i}"
            tid = store.tracks.upsert_track(f"group-{group}-{i}", title, "Artist", None, None)
            genre = "House" if group or i < 4 else ("Funk" if i < 7 else "Drum And Bass")
            store.tracks.set_track_genre(tid, genre)
            store.tracks.set_track_year(tid, 2023 if group else 2013)
            if group and i < 8:
                store.tracks.set_track_audio(tid, bpm=128, energy=.9)
            key = f"{title.lower()}|artist"
            keys.append(key)
            vectors.append((key, centers[group].tobytes()))
        # Deliberately different discovery size: current descriptions must use current membership.
        modes.append({"mode_id": group + 1, "label": "house", "size": 20,
                      "families": [["house", 4]], "centroid": centers[group],
                      "rep_keys": keys[:2], "space": space})
    store.rec_model.replace_rec_content_vectors(vectors)
    store.modes.replace_modes(modes, [], 1)


def test_profiles_explain_same_label_groups_using_actual_membership(store):
    _groups(store)
    profiles = mode_profiles.describe_modes(store)
    mixed, house = profiles[1], profiles[2]
    assert mixed["current"] and house["current"]
    assert mixed["title"] == "2010s blend"
    assert house["title"] == "House · 2020s"
    assert mixed["size"] == house["size"] == 10
    assert [(g["name"], g["share"]) for g in mixed["genres"]] == [
        ("House", .4), ("Soul & Funk", .3), ("Drum & Bass", .3)]
    assert house["genres"][0]["share"] == 1
    assert mixed["eras"] == [{"name": "2010s", "count": 10, "share": 1}]
    assert mixed["sound"] == []  # No audio traits inferred from genre or representative tracks.
    assert house["sound"] == [
        {"text": "128 BPM median", "measured": 8, "total": 10},
        {"text": "Higher energy", "measured": 8, "total": 10}]


def test_profiles_preserve_missing_tags_and_dates_in_the_denominator(store):
    _groups(store)
    tid = store.conn.execute("SELECT id FROM tracks WHERE video_id='group-0-0'").fetchone()[0]
    store.tracks.set_track_genre(tid,'')
    store.tracks.set_track_year(tid,'')
    mixed = mode_profiles.describe_modes(store)[1]
    assert mixed["size"] == 10
    assert sum(g["share"] for g in mixed["genres"]) == pytest.approx(1)
    assert next(g for g in mixed["genres"] if g["name"] == "House")["share"] == .3
    assert mixed["dated"] == 9
    assert mixed["eras"][0]["share"] == .9


def test_stale_profiles_do_not_compare_incompatible_vectors_or_guess_traits(store):
    _groups(store)
    model = {"cat": {"fam:house": 0, "dec:2010": 1, "dec:2020": 2, "sub:house": 3},
             "ncat": 4, "cont": [["bpm", 120, 10], ["energy", .5, .2]]}
    store.settings.set_setting("rec_content_model", json.dumps(model))
    profile = mode_profiles.describe_modes(store)[1]
    assert not profile["current"]
    assert profile["size"] == 20
    assert profile["genres"][0]["share"] == .2
    assert profile["genres"][1]["share"] == .8
    assert profile["eras"] == profile["sound"] == []


def test_descriptions_leave_feedback_and_the_choice_model_unchanged(store):
    _groups(store)
    store.modes.log_impressions(1, [("fresh", 1), ("comfort", 2)], 1)
    store.modes.log_pick(42, 1, 1)
    from yt_playlist.rec.mode_eval import choice_evidence
    before = choice_evidence(store)
    enriched = taste_viz.mix_choices(store)
    assert enriched["rows"][0]["profile"]["title"] == "2010s blend"
    assert enriched["rows"][1]["profile"]["title"] == "House · 2020s"
    for row in enriched["rows"]:
        del row["profile"]
    assert enriched == before == choice_evidence(store)
