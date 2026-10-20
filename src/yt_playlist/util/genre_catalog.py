"""Shared genre identities, equivalent labels, and recommendation families.

MusicBrainz's vendored vocabulary is combined with the original curated vocabulary below.
An alias identifies the SAME genre; a family groups related, distinct genres. Neither fuzzy
matching nor a shared family is evidence for merging identities.
"""
import functools
import json
import unicodedata
from collections import defaultdict, deque
from pathlib import Path

_DATA = Path(__file__).parent.parent / "data"
REVISION = "2026-10-20.2"

_FAMILY_LABELS = {
    "dnb": "Drum & Bass", "electro-synth": "Electronic & Synth",
    "folk-country": "Folk & Country", "garage-bass": "Garage & Bass", "hiphop": "Hip Hop",
    "rock-classic": "Classic Rock", "rock-indie": "Indie & Alternative Rock",
    "rock-post": "Post-Rock", "soul-funk": "Soul & Funk", "world-latin": "World & Latin",
}

_GENRES = [
    # --- rock ---
    ("Rock", []),
    ("Classic Rock", []),
    ("Hard Rock", []),
    ("Soft Rock", []),
    ("Indie Rock", []),
    ("Indie", []),
    ("Alternative Rock", ["alternative", "alt rock"]),
    ("Punk", ["punk rock"]),
    ("Pop Punk", []),
    ("Post-Punk", []),
    ("Post-Rock", []),
    ("Psychedelic Rock", ["psychedelic", "psych rock"]),
    ("Progressive Rock", ["prog rock", "prog"]),
    ("Art Rock", []),
    ("Garage Rock", ["garage"]),
    ("Surf Rock", []),
    ("Glam Rock", ["glam"]),
    ("Grunge", []),
    ("Shoegaze", []),
    ("Math Rock", []),
    ("Noise Rock", []),
    ("Krautrock", []),
    ("Southern Rock", []),
    ("Folk Rock", []),
    ("Blues Rock", []),
    ("Stoner Rock", ["stoner"]),
    ("Emo", []),
    ("Rockabilly", []),
    # --- metal ---
    ("Metal", ["heavy metal"]),
    ("Death Metal", []),
    ("Black Metal", []),
    ("Doom Metal", ["doom"]),
    ("Thrash Metal", ["thrash"]),
    ("Power Metal", []),
    ("Progressive Metal", ["prog metal"]),
    ("Nu Metal", ["nu-metal"]),
    ("Metalcore", []),
    ("Deathcore", []),
    ("Folk Metal", []),
    ("Industrial Metal", []),
    ("Symphonic Metal", []),
    ("Sludge", ["sludge metal"]),
    ("Grindcore", []),
    ("Hardcore", ["hardcore punk"]),
    # --- pop ---
    ("Pop", []),
    ("Synth-Pop", ["synthpop"]),
    ("Electropop", []),
    ("Indie Pop", []),
    ("Dream Pop", []),
    ("Power Pop", []),
    ("K-Pop", ["kpop"]),
    ("J-Pop", ["jpop"]),
    ("Dance-Pop", ["dance pop"]),
    ("Art Pop", []),
    ("Bedroom Pop", []),
    ("Hyperpop", []),
    ("Baroque Pop", []),
    ("Teen Pop", []),
    # --- electronic ---
    ("Electronic", ["electronica", "electro"]),
    ("House", []),
    ("Deep House", []),
    ("Tech House", []),
    ("Progressive House", []),
    ("Electro House", []),
    ("Techno", []),
    ("Trance", []),
    ("Psytrance", ["psychedelic trance", "psy-trance"]),
    ("Drum & Bass", ["drum and bass", "drum n bass", "dnb", "d&b"]),
    ("Dubstep", []),
    ("Garage", ["uk garage"]),
    ("Breakbeat", ["breaks"]),
    ("Jungle", []),
    ("Ambient", []),
    ("Downtempo", []),
    ("Trip Hop", []),
    ("IDM", ["intelligent dance music"]),
    ("EDM", []),
    ("Big Room", []),
    ("Future Bass", []),
    ("Synthwave", ["retrowave", "outrun"]),
    ("Vaporwave", []),
    ("Chillwave", []),
    ("Chillout", ["chill"]),
    ("Lo-Fi", ["lofi"]),
    ("Glitch", []),
    ("Hardstyle", []),
    ("Gabber", []),
    ("Eurodance", []),
    ("Acid", ["acid house"]),
    ("Minimal", ["minimal techno"]),
    ("Disco", []),
    ("Nu-Disco", ["nu disco"]),
    # --- hip hop / r&b ---
    ("Hip Hop", ["hip-hop", "rap"]),
    ("Trap", []),
    ("Boom Bap", []),
    ("Conscious Hip Hop", []),
    ("Gangsta Rap", []),
    ("Cloud Rap", []),
    ("Drill", []),
    ("Grime", []),
    ("R&B", ["rnb", "r and b", "rhythm and blues"]),
    ("Contemporary R&B", []),
    ("Neo-Soul", ["neo soul"]),
    ("Soul", []),
    ("Funk", []),
    ("Motown", []),
    ("Gospel", []),
    ("New Jack Swing", []),
    # --- jazz / blues ---
    ("Jazz", []),
    ("Smooth Jazz", []),
    ("Bebop", []),
    ("Swing", []),
    ("Big Band", []),
    ("Cool Jazz", []),
    ("Jazz Fusion", ["fusion"]),
    ("Free Jazz", []),
    ("Acid Jazz", []),
    ("Bossa Nova", []),
    ("Blues", []),
    ("Delta Blues", []),
    ("Chicago Blues", []),
    # --- folk / country / acoustic ---
    ("Folk", []),
    ("Indie Folk", []),
    ("Folk Pop", []),
    ("Singer-Songwriter", ["singer songwriter"]),
    ("Americana", []),
    ("Country", []),
    ("Alt-Country", ["alt country"]),
    ("Bluegrass", []),
    ("Country Rock", []),
    ("Acoustic", []),
    ("Celtic", []),
    # --- reggae / caribbean / latin ---
    ("Reggae", []),
    ("Dub", []),
    ("Ska", []),
    ("Dancehall", []),
    ("Reggaeton", []),
    ("Latin", []),
    ("Salsa", []),
    ("Bachata", []),
    ("Cumbia", []),
    ("Bossa", []),
    ("Samba", []),
    ("Flamenco", []),
    ("Afrobeat", ["afrobeats"]),
    ("Soca", []),
    # --- world / other ---
    ("World", ["world music"]),
    ("Classical", []),
    ("Opera", []),
    ("Baroque", []),
    ("Soundtrack", ["score", "film score", "ost"]),
    ("New Age", []),
    ("Spoken Word", []),
    ("Industrial", []),
    ("EBM", []),
    ("Gothic", ["goth"]),
    ("Darkwave", []),
    ("Experimental", ["avant-garde", "avantgarde"]),
    ("Noise", []),
    ("Drone", []),
    ("Post-Hardcore", []),
    ("Math Pop", []),
    ("Christian", ["christian rock", "ccm"]),
    ("Holiday", ["christmas"]),
]


def normalize(text):
    """Ignore case/punctuation without erasing word boundaries or accents.

    MusicBrainz's 'hyper techno' and 'hypertechno', for example, are distinct styles.
    """
    return " ".join("".join(ch if ch.isalnum() else " "
                            for ch in unicodedata.normalize("NFKC", text or "").casefold()).split())


def legacy_names():
    """The old seed, retained as the migration baseline for users' removals."""
    return [name for name, _ in _GENRES]


def legacy_entries():
    return _GENRES


def family_labels():
    families = json.loads((_DATA / "genre_families.json").read_text())["families"]
    return {key: _FAMILY_LABELS.get(key, key.title()) for key in families}


# Reviewed equivalences where MusicBrainz uses a different primary name. In particular, electro,
# electronica, acid house, minimal techno, Christian rock and Afrobeats are NOT broad-name aliases.
_EQUIVALENTS = {
    "alternative country": "Alt-Country",
    "drum and bass": "Drum & Bass",
    "hardcore punk": "Hardcore",
    "intelligent dance music": "IDM",
    "psychedelic trance": "Psytrance",
    "rhythm and blues": "R&B",
    "sludge metal": "Sludge",
    "uk garage": "Garage",
    "world music": "World",
}

_ACRONYMS = {"aor", "ccm", "ebm", "edm", "idm", "mpb", "nwobhm", "nwothm", "r&b", "uk", "us"}


def _display(name):
    return " ".join(word.upper() if word in _ACRONYMS else word.title() for word in name.split())


@functools.lru_cache(maxsize=1)
def catalog():
    """Immutable bundled identities and unambiguous labels, independent of the enabled list."""
    snapshot = json.loads((_DATA / "musicbrainz_genres.json").read_text())
    family_data = json.loads((_DATA / "genre_families.json").read_text())
    records = {normalize(name): {"key": normalize(name), "name": name, "mbid": None,
                               "source": "curated", "family": None, "family_source": None}
               for name, _ in _GENRES}
    labels = {}
    for row in snapshot["genres"]:
        name = _EQUIVALENTS.get(row["name"], row["name"])
        key = normalize(name)
        if key not in records:
            records[key] = {"key": key, "name": _display(name), "mbid": None,
                            "source": "musicbrainz", "family": None, "family_source": None}
        # Distinct MusicBrainz identities must never be merged by a spelling heuristic.
        if records[key]["mbid"] not in (None, row["id"]):
            raise ValueError(f"Conflicting MusicBrainz genres: {row['name']}")
        records[key]["mbid"] = row["id"]
        records[key]["description"] = row.get("disambiguation", "")
        labels[normalize(row["name"])] = (key, row["name"], "musicbrainz")
    for key, record in records.items():
        labels[key] = (key, record["name"], record["source"])

    candidates = defaultdict(list)
    for name, aliases in _GENRES:
        for alias in aliases:
            candidates[normalize(alias)].append((normalize(name), alias, "curated"))
    for row in snapshot["genres"]:
        key = labels[normalize(row["name"])][0]
        for alias in row.get("aliases", []):
            candidates[normalize(alias)].append((key, alias, "musicbrainz"))
    for label_key, (key, label, source) in labels.items():
        candidates[label_key.replace(" ", "")].append((key, label.replace(" ", ""), "spelling"))
    for label_key, values in candidates.items():
        # A primary genre name always wins; an ambiguous alias never picks a last writer.
        if label_key not in labels and len({value[0] for value in values}) == 1:
            labels[label_key] = values[0]

    # The hand-reviewed two-level app hierarchy has priority over imported parent relationships.
    for family, members in family_data["families"].items():
        for member in members:
            label = labels.get(normalize(member))
            if label:
                record = records[label[0]]
                if record["family"] not in (None, family):
                    raise ValueError(f"Conflicting genre families: {member}")
                record.update(family=family, family_source="curated")

    parents = defaultdict(set)
    parent_data = json.loads((_DATA / "musicbrainz_genre_parents.json").read_text())
    for child, parent in parent_data["parents"]:
        child_label, parent_label = labels.get(normalize(child)), labels.get(normalize(parent))
        if child_label and parent_label and child_label[0] != parent_label[0]:
            parents[child_label[0]].add(parent_label[0])
    anchors = {key: r["family"] for key, r in records.items() if r["family"]}
    for key, record in records.items():
        if record["family"]:
            continue
        queue, visited, found = deque([(key, 0)]), {key}, {}
        while queue:
            node, depth = queue.popleft()
            if node in anchors:
                found.setdefault(depth, set()).add(anchors[node])
                continue
            for parent in sorted(parents[node] - visited):
                visited.add(parent)
                queue.append((parent, depth + 1))
        if found and len(nearest := found[min(found)]) == 1:
            record.update(family=next(iter(nearest)), family_source="musicbrainz-parent")
    return records, labels


def canonical_name(name):
    records, labels = catalog()
    label = labels.get(normalize(name))
    return records[label[0]]["name"] if label else (name or "").strip()


def builtin_names():
    return sorted((record["name"] for record in catalog()[0].values()), key=str.casefold)
