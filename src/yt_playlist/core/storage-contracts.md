# Retained storage contracts

This reference collects the retention and compatibility rules established by the
[retained-copy studies](../../../experiments/retained_copy/README.md). See
[database maintenance](README.md) for operational commands and
[managed file ownership](../../../experiments/retained_copy/FAO-OWNERS.md) for
publication, reader lifetime, expiry, backup and recovery protocols. The current
[runtime file inventory](runtime-file-owners.md) names ID-derived
paths and the corresponding SQL receipts. Exact DDL
belongs to each owner's implementation.

## Preparation and verification

Regular migrations apply to the installed SQLite database and preserve applied
history. The [database maintenance guide](README.md) defines the supported release
cutoff, reset policy and explicit audit. Domain retention must preserve catalog,
accounts, listening evidence, training inputs, published output, provider completion
state, pending work and owned files unless the domain has an explicit expiry rule.

## Runtime metadata

SQL retains selected catalog facts plus compact current owners: provider and genre
dictionaries; current genre and per-field provider attribution; one typed manual
choice per entry/field; conflict status/revision; provider completion/backoff state;
and activation/collection cursors. Choice kinds distinguish null, blank, withdrawn,
text, number and canonical genre. Provider cycle, completion time, failures and
retry deadline preserve scheduling; its compatibility error column contains no
error body. Field codes are append-only and genre redirects preserve identity.

Runtime SQL contains no response body or candidate list. The shared
`collection_file_writes` store records file addresses, integrity digests,
sizes and pending/ready status. Kind, owner and revision select ready receipts. Existing pack offsets and
historical adapters remain supported until their own cutover. Model/input
identity and invalidation digests have distinct purposes.

New bodies in `<database filename>.metadata-runtime` use
`by-id/<ID modulo 256 in hex>/<ID>.json`. Envelopes identify format, owner,
revision and checksum. The controller commits the pending receipt, prepares and
publishes its file outside SQL, then completes the receipt and selects the head.
Installed compound owner/revision paths remain readable. Recovery verifies
complete required membership in private staging
before replacing live bytes, preserves age clocks and rejects incomplete custody
without changing existing documents.

Provider alternatives and conflict candidate details expire after seven days.
Expiry neither clears selected facts nor resets completion/backoff or triggers a
fetch. Current native inputs and explicit manual decisions remain durable.
Expired optional alternatives yield an empty list; missing or corrupt required
documents fail verification. Collection respects reader and backup snapshots.

## Artist credits and taste modes

Artist credit observations identify the most recent capture for each source and
the selected capture for each track source. They point to immutable ordered
credit content. Permanent `artist_credit_replays`
receipts identify `(entry_id, source, delivery_key)`, original observation ID,
credit ID and verified metadata binding. Replay checks ordered credit content
through artist redirects, returns the original ID and cannot reselect an expired
capture. A matching key spelling alone is not metadata-binding proof.

Preparation only deletes observations marked as neither the current capture nor
the selected credit. Unreferenced `legacy_capture` and
`selection_snapshot` conversion history can retire even with unknown times.
Other unreferenced captures expire after 30 days from last-seen processing time;
replay does not renew a retired capture. Protect current selections, explicit
identity reconciliation and unknown-time new evidence. Create receipts before
deletion; normalized credit content stays. No additional cold archive is needed.
Explicit activation enables resumable daily maintenance. The checkpoint is stored
in `settings`.

Taste-mode retention keeps every ID, label, size, original time, space and active
flag, and preserves active vectors/families/representatives exactly. Inactive
payload retirement writes an empty blob and empty family/representative arrays,
with an explicit `taste_mode_retired_payloads` tombstone. IDs are never reused.
Readers distinguish retirement from a genuine empty vector. Reactivation requires
a complete new payload and removal of the tombstone; setting `active=1` alone
fails. Comparisons and held menus retain their own immutable content. Grouping
does not reconstruct retired definitions. Bounded explicit preparation and enabled
daily scans recheck active state under the writer; late model publication remains
revision-fenced and existing SQL readers retain their snapshots.

## Mix choices and group lifetime

A mix descriptor freezes the displayed lane/theme, label, description and set of
raw genre labels, including the card's artist fallback. Equal sets share storage
only after membership verification. Today's taxonomy cannot replace captured
labels. Keep menu/option identities, ordering, visibility, actions, choice clocks,
withdrawals, score versions and assignment receipts. An explicitly displayed Vibe
marker may support earned access; older menus do not invent one.

Broad choice evidence owns no recording, recipe, vector centroid, track features
or group generation. It cannot create a centroid from today's matching tracks or
provide child-specific wins where only a common ancestor is distinguishable.
Current musical evidence still owns group formation and splitting. Legacy menus
without captured rendered genres are marked reconstructed; retain known partial
labels, but incomplete sets do not support preference learning.

Current groups, operational
predecessors, builds, readers and actual outputs retain their roots. Album/Radio
evidence remains independent. Old JSON stays readable until separate cleanup.
An unacknowledged event projection is reconciled under publication admission.
Recovery includes all six listening-evidence file kinds in verified paged
manifests; SQL snapshots exclude menus whose ownership postdates the snapshot.

## Served recommendations

SQL retains impression identity, request/surface/rank uniqueness, lane/mode/score,
policy, captured/resolved recording and offer time. Owned reporting facts retain
ranker, video/artist/genre, ownership, last-play and coverage with presence flags,
comparison/cluster/cohort, assignment flag/probability and legacy-policy flag.
Explicit primary/pool policy links remain authoritative even when unresolved.
Reports use captured facts, without diagnostic files or today's catalog metadata.

Diagnostics are owner-checked files addressed by monotonic impression IDs. They
expire after 30 days under the file policy, independently of reporting retention
and metadata's seven-day alternatives. SQL distinguishes deliberate expiry from
unexpected absence/corruption. Orphan collection uses a durable bounded shard
cursor, a 30-day file-creation grace and reader/writer protection. Recovery checks
complete membership and envelopes before replacement. Explicit resumable
preparation captures concurrent legacy writes before guarded cutover; old JSON,
indexes and compatibility triggers await separate retirement.

## Vibe calculations

Keep all original measurements, raw samples and measurement/sample links, including
inputs to superseded predictions. The normalized format interns only measurement
model, prediction policy and current-result input versions in immutable
`vibe_versions`; APIs resolve the exact original text. Measurement uniqueness
includes version identity, and equal scores do not imply equivalent versions.
The release upgrade interns existing version strings in the installed database.

Superseded prediction summaries expire 180 days after known `predicted_at`;
current predictions and unknown-age imports remain. Contributor detail files have
their independent 30-day policy. Retired calculation history is unavailable,
distinct from missing required input or invented empty data. Bounded collection
rechecks current membership under the provenance reader fence and writer
reservation, persists its cursor and preserves the prediction ID high-water mark.
Pending work still reads retained measured cohorts; publication rejects stale
prediction reuse.
