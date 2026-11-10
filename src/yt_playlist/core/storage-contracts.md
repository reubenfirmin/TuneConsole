# Retained storage contracts

This reference collects the retention and compatibility rules established by the
[retained-copy studies](../../../experiments/retained_copy/README.md). See
[database maintenance](README.md) for operational commands and
[managed file ownership](../../../experiments/retained_copy/FAO-OWNERS.md) for
publication, reader lifetime, expiry, backup and recovery protocols. Exact DDL
belongs to each owner's implementation.

## Preparation and verification

Regular installation creates empty owners and bounded guards. Library-sized
conversion is explicit, bounded and resumable; supported installed readers and
writers continue until verified activation. Continuing writes invalidate stale
preparation. Verify complete source-derived values before a short publication
transaction checks the captured revision. Preserve applied migration history;
retiring old owners, adapters and files requires separate verification.

A retained copy preserves installed schema, indexes, views, triggers, IDs,
implicit row IDs, sequence high-water marks and SQLite application/user versions.
Its plan names every table, predicate and retained key set. Retention includes
all FK parents and complete owned child relations. Preserve catalog, accounts,
personal evidence, training inputs, published output, provider completion state,
pending work and recovery state unless an explicit domain rule permits omission.
Preserve exact external input bytes and the evidence ledger and playback inbox
when present. A smaller SQL file does not establish correctness.

Verify retained values and types, relationships, current selections, model inputs,
file custody and recovery independently. Report SQL/index allocation, file logical
and allocated bytes, recovery copies and phase timings separately. Capture queues
before startup to distinguish existing backlog from conversion-induced work.
Experiments use isolated private copies and retain a pristine comparison baseline.
Historical experiment omissions do not authorize production deletion.

## Runtime metadata

SQL retains selected catalog facts plus compact current owners: provider and genre
dictionaries; current genre and per-field provider attribution; one typed manual
choice per entry/field; conflict status/revision; provider completion/backoff state;
and activation/collection cursors. Choice kinds distinguish null, blank, withdrawn,
text, number and canonical genre. Provider cycle, completion time, failures and
retry deadline preserve scheduling; its compatibility error column contains no
error body. Field codes are append-only and genre redirects preserve identity.

New runtime SQL contains no response body, candidate list or file locator/checksum.
Existing pack offsets and historical adapters remain supported until their own
cutover. Model/input identity and invalidation digests have distinct purposes and
are not redundant file-integrity columns.

`<database filename>.metadata-runtime` owns alternatives by recording/provider/time,
native inputs by encoded source key/revision, and choices/conflicts by integer
owner/revision. Envelopes identify format, owner, revision and checksum. Committed
revisions are immutable; replacing an uncommitted revision requires the writer
reservation. Recovery verifies complete required membership in private staging
before replacing live bytes, preserves age clocks and rejects incomplete custody
without changing existing documents.

Provider alternatives and conflict candidate details expire after seven days.
Expiry neither clears selected facts nor resets completion/backoff or triggers a
fetch. Current native inputs and explicit manual decisions remain durable.
Expired optional alternatives yield an empty list; missing or corrupt required
documents fail verification. Collection respects reader and backup snapshots.

Preparation compares native inputs/provenance, decisions and their subjects/times,
current choices, attribution, genres, alternatives and conflicts. Unknown historical
decision times remain null. Repeat bounded work until complete, then activate:

```sh
uv run python -m yt_playlist.core.preparation.metadata_runtime COPY.db --progress PROGRESS.json --batches 20
uv run python -m yt_playlist.core.preparation.metadata_runtime COPY.db --progress PROGRESS.json --activate
```

`--restart` discards inactive progress only. Locator retirement additionally
requires compaction to have emptied historical owners and an independently
exported runtime-file recovery manifest:

```sh
uv run python -m yt_playlist.core.preparation.metadata_locators VERIFIED_COPY.db --recovery-manifest RECOVERY/manifest.json
```

Retirement verifies custody, FKs, stored SQL dependencies and the current data
version before atomic DDL. Inactive preparation, populated history, unfinished
rebuilds, incomplete custody or concurrent writes refuse it. Interrupted DDL rolls
back; repetition is safe. It preserves migration histories and required empty
compatibility relations, rejects writes to retired history and deletes no files.

## Artist credits and taste modes

Artist retention preserves immutable ordered credit content, current capture heads,
entry selections and their source agreement. Permanent `artist_credit_replays`
receipts identify `(entry_id, source, delivery_key)`, original observation ID,
credit ID and verified metadata binding. Replay checks ordered credit content
through artist redirects, returns the original ID and cannot reselect an expired
capture. A matching key spelling alone is not metadata-binding proof.

Explicit preparation audits all SQL references to observations and rechecks current
heads/selections before each deletion. Unreferenced `legacy_capture` and
`selection_snapshot` conversion history can retire even with unknown times.
Other unreferenced captures expire after 30 days from last-seen processing time;
replay does not renew a retired capture. Protect current selections, explicit
identity reconciliation and unknown-time new evidence. Create receipts before
deletion; normalized credit content stays. No additional cold archive is needed.
Only explicit activation enables resumable daily maintenance; older formats keep
their existing read/write path.

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

`core.preparation.mix_choices` publishes compact projections before releasing old
choice roots, 128 version references per transaction. Current groups, operational
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
Populated installed text owners keep read/write support until an explicit bounded
rebuild verifies every decoded value/type and FK before installation.

Superseded prediction summaries expire 180 days after known `predicted_at`;
current predictions and unknown-age imports remain. Contributor detail files have
their independent 30-day policy. Retired calculation history is unavailable,
distinct from missing required input or invented empty data. Bounded collection
rechecks current membership under the provenance reader fence and writer
reservation, persists its cursor and preserves the prediction ID high-water mark.
Pending work still reads retained measured cohorts; publication rejects stale
prediction reuse.
