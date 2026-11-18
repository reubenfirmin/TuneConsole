# Runtime file ownership

Controllers coordinate database reservations, file access and completion.
See [storage boundaries](storage-boundaries.md) for source-family ownership,
preparation commands and rebuild capabilities.
Repositories own SQL; file ports receive detached identities and receipts. A new
immutable body has a committed pending record before FAO preparation starts.
After publication, its repository verifies the receipt and marks it ready while
selecting the domain head. Readers exclude pending work. Failed publication
preserves the prior selected head; abandoned files receive ordinary owner expiry.

The database ID names the file. Digests verify bytes, model inputs or logical
content equality; they do not name newly written files. Several domain records
may share one bounded pack whose own SQL receipt supplies its file ID. See
[file packs](#file-packs) for limits and seeking.

## Bodies written during normal operation

`D` is the database filename, including its extension. `HH` is the positive
integer file ID modulo 256, encoded as two lowercase hexadecimal digits. Paths
are relative to the indicated root. Installed layouts can retain earlier paths;
their readers and explicit backup adapters remain supported.

| Domain and root | Owning SQL identity | New path |
| --- | --- | --- |
| Collection/workflow state, `D.collections` | `collection_file_writes.id`, bound to kind and domain owner | `by-id/HH/ID.json` |
| Retained action playlist backups, `D.collections` | `action_backup_writes.id` | `backups/HH/ID.json` |
| Recommendation/prepared output, `D.recommendations` | `recommendation_file_writes.id`, bound to output owner, surface and chunk | `by-id/HH/ID.json` |
| Group documents, `D.groups` | `group_file_writes.id` | `by-id/HH/ID.json` |
| Policy bodies, `D.policies` | `policy_file_writes.id` | `by-id/HH/ID.json` |
| Archived policy configurations, `D.policy-history` | `policy_history_files.id` | `by-id/HH/ID.json` |
| Musical profiles, `D.profiles` | `profile_file_writes.id`, bound to recording/source kind | `KIND/by-id/HH/ID.json` |
| Recording/artist embeddings, `D.embeddings` | `embedding_file_writes.id`, bound to kind and vector basis | `KIND/by-id/HH/ID.bin` |
| Shared Vibe models, `D.vibe-models` | `vibe_model_file_writes.id` | `by-id/HH/ID.json` |
| Prediction input detail, `D.vibe-provenance` | `provenance_file_writes.id`; calculation heads retain the file ID | `by-id/HH/ID.json` |
| Audio measurements, adjacent `sample-attributes` | `sample_attributes.id` | `by-id/FIRST-UTF8-BYTE/ESCAPED-ID.json` |
| Audio analyses, `D.annotations` | `annotation_file_writes.id` | `by-id/HH/ID.json` |
| Paid descriptions, `D.descriptions` | `description_file_writes.id` | `by-id/HH/ID.json` |
| Captured metadata deliveries, `D.metadata` | `delivery_file_writes.id`; delivery rows own retained ranges | `by-id/HH/ID.json` |
| Runtime metadata, `D.metadata-runtime` | `metadata_file_writes.id`; kind/owner/revision references select ready receipts | `by-id/HH/ID.json` |
| Listening evidence, `D.evidence` | `evidence_file_writes.id`; kind/key references select ready receipts | `KIND/HH/ID.json` |
| Listening retention inventory/intents, `D.evidence` | `evidence_file_maintenance.id = 1` | `maintenance/1/inventory.json`, `maintenance/1/retirements.json` |
| Served diagnostics, `D.served` | committed `impression_file_writes.impression_id`, then `rec_served_impressions.id` | `DECIMAL-ID-MOD-100/ID.json` |
| Playback captures, `D.playback-feedback.sqlite3.observations` | independent inbox `capture_id`, a stored 32-character hexadecimal ID | `LAST-TWO-ID-CHARACTERS/ID.json` |
| Durable playlist snapshots, configured data `backups` directory | `playlist_backup_writes.id`, a stored 32-character hexadecimal capture ID | `ID.json` |

The seventeen collection kinds are road-trip drafts, radio sessions, library sync,
taste settings, taste checkpoints, taste comparisons, experiment decisions,
Spotify imports, playlist recipes, playlist canvases, actions, action drafts,
action steps, playlist garbage collection, Spotify transfer receipts, TV append
intents and discovery candidates. Their domain keys remain distinct from the
immutable file receipt ID. The finite mapping lives in
[`collection_owners.py`](collection_owners.py).

The eight recommendation output owners are `prepared_outputs`,
`album_note_outputs`, `listening_output_files`, `home_display_files`,
`recommendation_report_files`, `model_pair_files`, `radio_seed_output` and
`model_autotune_output`. Listening evidence has six kinds: opportunity, response,
forecast, artifact, report and checkpoint. Runtime metadata has four kinds:
alternatives, native inputs, choices and conflicts.

## Existing formats and operational files

Existing hash-named packs, compound metadata revision paths, old inline records,
and model/provenance generations remain readable alongside ID-named files.
Explicit preparation and backup/recovery may read or reproduce these installed
formats. Their manifests and temporary staging trees belong to that explicit
operation, rather than a new runtime file owner. Bulk conversion does not run at
startup or inside regular migrations. Applied migration history is preserved.

Runtime reconciliation no longer writes legacy recovery-original archives or
rewrites legacy journals. See the [historical recovery decision](#historical-recovery-copies).
Current Vibe measurements, analyses, predictions, required provenance and models
remain retained by their existing owners. Home output uses the recommendation
and listening owners above.

Reader locks, FAO temporary stages and fixed bounded-scan checkpoints are
coordination state: they contain no domain bodies and cannot establish custody
or authorize deletion by themselves. Their names remain implementation details
under each owner root. FAO leases protect active stages; abandoned stages have a
seven-day grace. Cursor/inventory candidates are rechecked against current SQL
ownership and observed file identity before deletion.

SQLite databases and sidecars, configuration/credentials, logs, downloaded user
imports, installed code, packaged catalogs/static resources and configured model
resources are separate application or external boundaries. Read-only resources
still use FAO where required; they are not newly persisted domain records. The
[FAO source audit](../../../experiments/retained_copy/FAO-OWNERS.md#source-audit-boundaries-689)
defines these boundaries.

## Collection boundaries

The managed-file and collection retention controllers compose SQL ownership
queries with database-free directory/file ports. Directory work occurs before
serving-lock admission; bounded ownership pages resume after a deadline. Owner
and seek-key changes invalidate saved SQL scan progress. Reader fences protect
older snapshots and active publication. Installed writers retain their single
repository writer reservation during the final ownership/deletion decision;
inbox queries never hold the library's SQL transaction.

Required bodies do not expire merely because they are old. Owner-specific
retention rules are in [storage contracts](storage-contracts.md).
Reporting reads SQL facts without opening a reporting transaction. Controllers
do not hold a transaction across repositories, and file ports perform no SQL.

## Historical recovery copies

Runtime artist and album reconciliation, recording reassociation and unresolved
legacy provider attempts no longer capture historical recovery copies.
Reconciliation preserves current feedback, discovery contributions and queryable
identity guards. An expired explanation does not prevent reconciliation.

Historical archive formats remain available to explicit preparation and
backup/restore; background maintenance does not rewrite their journals. Current
Vibes analyses, predictions, annotations, required evidence and models remain
owned by their current stores. Removed recovery writers have no replacement
format or bulk conversion.

## File packs

[`file_pack.py`](file_pack.py) owns bounded batching and verified byte-range
reads. Domain owners retain their codecs, paths, SQL identities, statuses and
retention decisions. The helper has no database dependency and reads from a
caller-owned FAO stream.

New shared variable-length packs contain at most 128 records and 8 MiB including
framing. Small concatenated document packs target 256 KiB; one larger record may
occupy its own pack within the hard limit. Retained ranges have a byte offset,
length and SHA-256 digest, so records need no line delimiter. Ordered reads reuse
one pinned stream and check its header once. Installed encodings remain readable.

Profiles and prediction provenance retain bounded keyed JSON envelopes; reading
one currently decodes its pack. Embeddings retain their fixed-width binary codec.
FAO uses bzip2 compression, so offsets locate decompressed records and do not
promise physical random access; seeking consumes its decompression work budget.
