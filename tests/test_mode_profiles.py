import json
from tests.enrichment_support import seed_audio

import numpy as np

from yt_playlist.rec import embedding_vectors as embed, taste_viz


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
                seed_audio(store, tid, bpm=128, energy=.9)
            key = f"{title.lower()}|artist"
            keys.append(key)
            vectors.append((key, centers[group].tobytes()))
        # Deliberately different discovery size: current descriptions must use current membership.
        modes.append({"mode_id": group + 1, "label": "house", "size": 20,
                      "families": [["house", 4]], "centroid": centers[group],
                      "rep_keys": keys[:2], "space": space})
    store.rec_model.replace_rec_content_vectors(vectors)
    store.modes.replace_modes(modes, [], 1)


def test_descriptions_leave_feedback_and_the_choice_model_unchanged(store):
    _groups(store)
    store.modes.log_impressions(1, [("fresh", 1), ("comfort", 2)], 1)
    store.modes.log_pick(42, 1, 1)
    from yt_playlist.rec.mode_eval import choice_evidence
    before = choice_evidence(store)
    enriched = taste_viz.mix_choices(store)
    assert enriched["rows"][0]["profile"]["title"] == "2010s blend"
    assert enriched["rows"][1]["profile"]["title"] == "House · 2020s"
    assert [row['profile']['size'] for row in enriched['rows']] == [10, 10]
    assert enriched['rows'][1]['profile']['sound'] == [
        {'text': '128 BPM median', 'measured': 8, 'total': 10},
        {'text': 'Higher energy', 'measured': 8, 'total': 10}]
    for row in enriched["rows"]:
        del row["profile"]
    assert enriched == before == choice_evidence(store)
