"""Shared inputs for Home preparation and its independent consumers."""
import numpy as np

from tests.musical_support import prepare_profiles
from tests.recommendation_support import publish_bundles
from yt_playlist.util.matching import identity_key


def seed(store, *, tracks_per_lane=8, publish=True):
    modes, bundles = [], {"all": {}, "_meta": {"comfort_pool": 100, "full_pools": True}}
    for mid, genre in enumerate(("Jazz", "Techno", "Classical", "Ambient", "Trance", "Indie Rock")):
        modes.append({"mode_id": mid, "label": genre, "families": [], "size": 50,
                      "centroid": np.eye(6)[mid], "rep_keys": []})
        bundles[str(mid)] = {}
        for lane in ("wheelhouse", "explore", "comfort", "fresh"):
            items = []
            for i in range(tracks_per_lane):
                title, artist, video = f"{lane} {mid} {i}", f"Artist {mid} {i}", f"v-{lane}-{mid}-{i}"
                key = identity_key(title, artist)
                if lane == "fresh":
                    store.discovery.upsert_discovered_track(key, video, title, artist, "", None, genre,
                                                            None, None, 1)
                else:
                    tid = store.tracks.upsert_track(video, title, artist, "", None)
                    store.tracks.set_track_genre(tid, genre)
                items.append({"key": key, "title": title, "artist": artist, "album": "", "video_id": video,
                              "thumbnail": None, "genre": genre, "plays": 0, "reason": "", "lane": lane})
            bundles[str(mid)][lane] = items
            bundles["all"].setdefault(lane, []).extend(items)
    store.modes.replace_modes(modes, [], 1)
    prepare_profiles(store)
    if publish:
        publish_bundles(store, bundles, vibes=True)
    return bundles
