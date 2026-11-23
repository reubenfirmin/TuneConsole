# Runtime file ownership

Controllers coordinate database reservations, file access and completion.
See [storage boundaries](storage-boundaries.md) for source-family ownership,
and preparation commands.
Repositories own SQL; file ports receive detached identities and receipts. A new
receipt-owned body has a committed pending record before FAO preparation starts.
After publication, its repository verifies the receipt and marks it ready while
selecting the domain head. Readers exclude pending work. Failed publication
preserves the prior selected head; abandoned files receive ordinary owner expiry.

The database ID names receipt-owned files. Group documents are published in
bounded packs named by their SHA-256 digest. Group definitions, generations and
lineage rows own the exact ranges in the packs. See
[file packs](#file-packs) for limits and seeking.

## Bodies written during normal operation

`D` is the database filename, including its extension. `HH` is the positive
integer file ID modulo 256, encoded as two lowercase hexadecimal digits. Paths
are relative to the indicated root. Installed layouts can retain earlier paths;
their readers and explicit backup adapters remain supported.

| Domain and root | Owning SQL identity | New path |
| --- | --- | --- |
| Collection/workflow state, `D.collections` | `collection_file_writes.id`, bound to kind and domain owner | `by-id/HH/ID.json` |
| Retained action playlist backups, `D.collections` | `actions.backup_inventory` | `backups/HH/ID.json` |
| Recommendation/prepared output, `D.recommendations` | `collection_file_writes` recommendation receipt, bound to file ID, output owner, surface and chunk | `by-id/HH/ID.json` |
| Group documents, `D.groups` | `group_versions`, `group_generations` | `DIGEST[:2]/DIGEST.json` |
| Policy bodies, `D.policies` | `policy_file_writes.id` | `by-id/HH/ID.json` |
| Archived policy configurations, `D.policy-history` | `policy_history_files.id` | `by-id/HH/ID.json` |
| Musical profiles, `D.profiles` | Shared `profile` receipt with a recording or source owner | `KIND/by-id/HH/ID.json` |
| Recording/artist embeddings, `D.embeddings` | `recording_embedding_files` and `artist_embedding_files` | `KIND/HH/DIGEST.bin`, with the original numeric locator for previously selected packs |
| Shared Vibe models, `D.vibe-models` | `vibe_model_file_writes.id` | `by-id/HH/ID.json` |
| Prediction input detail, `D.vibe-provenance` | Shared `prediction-provenance` receipt referenced by the prediction | `by-id/HH/ID.json` |
| Audio measurements, adjacent `sample-attributes` | `sample_attributes.id` | `by-id/FIRST-UTF8-BYTE/ESCAPED-ID.json` |
| Captured metadata deliveries, `D.metadata` | Shared `metadata-delivery` receipt with the contract digest and selected delivery ID | `by-id/HH/ID.json` |
| Runtime metadata, `D.metadata-runtime` | Shared `runtime-metadata` receipt selected by kind, owner and revision | `by-id/HH/ID.json` |
| Listening evidence, `D.evidence` | Shared `listening-evidence` receipt selected by kind and record key | `KIND/HH/ID.json` |
| Listening retention inventory/intents, `D.evidence` | `collection_file_writes.evidence_maintenance` on row 0 | `maintenance/1/inventory.json`, `maintenance/1/retirements.json` |
| Served diagnostics, `D.served` | IDs reserved by `settings.next_served_impression_id` and published with `rec_served_impressions.id` | `DECIMAL-ID-MOD-100/ID.json` |
| Durable playlist snapshots, configured data `backups` directory | `collection_file_writes.owner_id` for `kind='playlist-backup'`, a stored 32-character hexadecimal capture ID | `ID.json` |

The seventeen collection kinds are road-trip drafts, radio sessions, library sync,
taste settings, taste checkpoints, taste comparisons, experiment decisions,
Spotify imports, playlist recipes, playlist canvases, actions, action drafts,
action steps, playlist garbage collection, Spotify transfer receipts, TV append
intents and discovery candidates. Their domain keys remain distinct from the
immutable file receipt ID. The finite mapping lives in
[`collection_owners.py`](collection_owners.py).

Shared receipts in these five file domains use `collection_file_writes.file_address`.
The address retains the original path ID for existing files and uses the shared
row ID for new files.

Recommendation output owners are `prepared_outputs`,
`listening_output_files`, `home_display_files`,
`recommendation_report_publications`, `radio_seed_output` and
`model_autotune_output`. Listening evidence has six kinds: opportunity, response,
forecast, artifact, report and checkpoint. Runtime metadata has four kinds:
alternatives, native inputs, choices and conflicts.

Playback captures, including their sequence and full bodies, live in a single
FAO-managed queue at `D.playback-feedback/inbox.json`. Acknowledgement removes a
capture after the library commits its projection and receipt. Startup publishes
the installed queue durably before retiring the SQLite sidecar. Legacy observation
files age out through ordinary orphan collection after cutover.

Story preparation owns its replaceable Wikipedia cache at `D.wiki/cards.json`.
The file holds hit and miss cards by subject and is bounded by the story cache's
eviction policy. It has no SQL owner and can be repopulated by story preparation.

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
repository writer reservation during the final ownership/deletion decision.

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
