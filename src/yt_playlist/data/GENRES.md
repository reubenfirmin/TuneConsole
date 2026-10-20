Genre vocabulary and family mapping
===================================

`musicbrainz_genres.json` vendors all 2,216 genre names, IDs and disambiguations returned by
MusicBrainz's paginated `/ws/2/genre/all` endpoint on 2026-10-20. The ten English primary aliases
displayed on https://musicbrainz.org/genres were joined by MusicBrainz ID. These core metadata
are CC0: https://musicbrainz.org/doc/About/Data_License . Startup needs no network access.

`musicbrainz_genre_parents.json` contains 979 explicit subgenre relationships collected from
218 genre overview pages. `relationship_sources` lists those pages' MusicBrainz IDs. This is
a partial relationship snapshot around the app's existing family members, not a complete
MusicBrainz taxonomy. Fusion and influence relationships are deliberately excluded: influence
does not establish parentage or identity.

`genre_families.json` defines the app's 22 recommendation families, reviewed member assignments,
energy and adjacency. The runtime catalog preserves those assignments, then follows explicit
parent edges to the nearest reviewed ancestor. Equal-distance ancestors in different families
are ambiguous and remain unassigned. Cycles terminate; record order cannot affect the result.
The UI shows exactly this two-level projection, including a separate unassigned group.

`util/genre_catalog.py` preserves curated display names and merges reviewed equivalents.
Normalization handles case, Unicode presentation, punctuation and unambiguous compact spellings.
Word boundaries and accents survive: MusicBrainz's “hyper techno” and “hypertechno” have different
IDs and describe different styles. Primary genre names take precedence over old aliases; ambiguous
aliases are discarded. No substring or fuzzy matching merges identities. Afrobeat/Afrobeats,
Metal/Heavy Metal, Electronic/Electro/Electronica and Minimal/Minimal Techno remain distinct.

The SQLite tables separate `genre_catalog`, `genre_alias`, `genre_external_id` and `genre_family`.
Aliases and family assignments retain their sources. `genre_whitelist` holds enabled canonical
names for compatibility with existing callers. Disabling a genre disables all its labels without
allowing a fallback to an older broad alias. Provider artist caches retain raw metadata so edits
take effect without another network request. Track tags and learned taste keys are not migrated.
Derived recommendation content vectors refresh once after a catalog revision so both library
and discovered tracks use the updated families and aliases.

When updating a snapshot, retain provenance and IDs, validate the full count and identity joins,
review any primary-name or family conflicts, and bump `genre_catalog.REVISION`. Upgrades compare
the previous default baseline with the new defaults, adding genuinely new entries while respecting
removals. The first upgrade uses the original curated seed as its baseline. An explicitly empty
list stays empty. Reset explicitly restores the current complete defaults. All database changes
and the migration marker are committed in one transaction.
