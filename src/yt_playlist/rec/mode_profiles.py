"""Read-only descriptions of the music groups used to assemble Home mixes.

Describe the actual nearest-centroid buckets, rather than inferring a group's contents from
its broad genre label or its six representative tracks. Never change grouping or feedback.
"""
from collections import Counter
import json

import numpy as np

from yt_playlist.rec import embed
from yt_playlist.rec.rec_dao import RecDao
from yt_playlist.util import genre_catalog, genre_map


def _breakdown(counts, total, labels, limit=3, *, complete=False):
    parts = [{"name": labels.get(name, name.removeprefix("other:").capitalize()),
              "count": count, "share": count / total}
             for name, count in counts.most_common(limit)] if total else []
    remainder = total - sum(p["count"] for p in parts)
    if remainder > 0:
        other = "Other genres" if complete and not counts.get("") else "Other / untagged"
        parts.append({"name": other, "count": remainder,
                      "share": remainder / total})
    return parts


def _sound(keys, audio, model):
    """Only summarize measured features covering at least half of a group, with sample counts."""
    traits = []
    stats = {name: (mu, sd) for name, mu, sd in model["cont"]}
    features = [("bpm", None, None), ("energy", "Higher energy", "Lower energy"),
                ("danceability", "More danceable", "Less danceable"),
                ("mood_relaxed", "More relaxed", "Less relaxed"),
                ("instrumental", "More instrumental", "Less instrumental")]
    for feature, high, low in features:
        values = [float(audio[k][feature]) for k in keys
                  if k in audio and audio[k].get(feature) is not None
                  and np.isfinite(float(audio[k][feature]))]
        if len(values) < max(3, len(keys) / 2):
            continue
        median = float(np.median(values))
        if feature == "bpm":
            text = f"{median:.0f} BPM median"
        else:
            mu, sd = stats.get(feature, (0, 0))
            if not sd or abs(median - mu) < .5 * sd:
                continue
            text = high if median > mu else low
        traits.append({"text": text, "measured": len(values), "total": len(keys)})
        if len(traits) == 2:
            break
    return traits


def describe_modes(store):
    """Profiles keyed by mode id, including honest fallbacks for an older content space.

    Current membership uses precisely the cosine assignment in mode_surfaces._nearest_mode.
    Discovery's stored sizes/histograms use unnormalized k-means centers and can differ slightly;
    never combine those counts with the current buckets' era or audio statistics.
    """
    modes = store.modes.list_modes(active_only=True)
    if not modes:
        return {}
    labels = {**genre_catalog.family_labels(), "": "Untagged"}
    profiles = {}
    for mode in modes:
        size = mode["size"]
        profiles[mode["mode_id"]] = {
            "title": mode["label"], "size": size, "current": False,
            "genres": _breakdown(Counter(dict(mode["families"])), size, labels),
            "eras": [], "dated": 0, "sound": [],
        }
    raw = store.get_setting("rec_content_model")
    if not raw:
        return profiles
    model = json.loads(raw)
    space = embed.content_model_fingerprint(model)
    current = [m for m in modes if m.get("space") == space]
    if not current:
        return profiles
    keys, vectors, _ = embed.load_content_vectors(store)
    dim = model["ncat"] + len(model["cont"])
    if vectors is None or vectors.shape[1] != dim or any(len(m["centroid"]) != dim for m in current):
        return profiles
    centers = np.stack([m["centroid"].astype(np.float64) for m in current])
    nearest = (vectors.astype(np.float64) @ centers.T).argmax(axis=1)
    dao = RecDao(store)
    content, audio = dao.track_content(), dao.track_audio_features()
    for index, mode in enumerate(current):
        members = [keys[i] for i in np.flatnonzero(nearest == index)]
        size = len(members)
        if not size:
            continue
        families, tags, decades = Counter(), Counter(), Counter()
        for key in members:
            genre, year = content.get(key, (None, None))
            families[genre_map.family(genre)] += 1
            if genre:
                tags[genre] += 1
            if year and str(year)[:4].isdigit():
                decades[f"{int(str(year)[:4]) // 10 * 10}s"] += 1
        dated = sum(decades.values())
        family, count = families.most_common(1)[0]
        era = decades.most_common(1)[0][0] if decades and decades.most_common(1)[0][1] >= size / 2 else ""
        if family and count >= size / 2:
            tag, tag_count = tags.most_common(1)[0]
            title = tag if tag_count >= size / 2 else labels.get(family, family.removeprefix("other:").capitalize())
            if era:
                title += f" · {era}"
        else:
            title = f"{era} blend" if era else "Mixed genres"
        profiles[mode["mode_id"]] = {
            "title": title, "size": size, "current": True,
            "genres": _breakdown(families, size, labels, complete=True),
            "eras": [{"name": name, "count": n, "share": n / size}
                     for name, n in decades.most_common(2) if n / size >= .05],
            "dated": dated, "sound": _sound(members, audio, model),
        }
    return profiles
