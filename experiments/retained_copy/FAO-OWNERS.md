# Managed files and cold runtime bodies

Implementation contract for #689, #693, #694, #696, #697 and #699. This work
does not change the native library. Explicit preparation and the #658 direct
destination are the only bulk conversion entry points. Applied migration
history stays intact; #626 owns subsequent compatibility retirement.

## File access

`core.fao` exposes `write(path, content)`, `read(path)`, `prepare(path, content)`
and `delete(path)`. Read results supply bounded `.bytes` or a streaming context,
logical metadata, temporary decoded paths and HTTP responses. Writing a read
result streams a verified copy with its logical clocks. Writing edited content
from `content.replacing(bytes)` also checks the observed source identity.
Prepared files publish by atomic rename and remove private temporary bytes when
closed. Compression, integrity checks and file-reader leases stay inside FAO.

The logical path and original bytes stay stable. The physical file contains a
bzip2 stream and a fixed custody trailer: format, logical size, original mtime,
captured inode ctime, and SHA-256 of the original bytes. Plain installed files
remain readable. Conversion, relocation and recovery preserve both age clocks;
publishing changed content renews them. There is no SQL path/compression registry.

Readers bound logical bytes and cumulative decompression work. Real-path users
hold a temporary materialization lease; range delivery uses that same original
content and preserves logical content length, type, modification time and ETag.
Ordinary writes atomically replace the logical name without a decompression
reread or synchronous file/directory flushes. Failed compression or rename leaves
the prior publication intact. Playlist backups authorizing remote deletion use
`Content(bytes, durable=True)`; FAO completes their disk durability boundary
before returning. Cache and evidence writes do not inherit that protocol.
Inode leases and the existing domain snapshot fence protect active consumers
from collection.

Owners retain their paths, content validation, SQL publication guards, evidence
ID conflicts, storage budgets and retention eligibility. `file_collection`
provides bounded traversal and coordinates collectors with retained SQL views.
Plain and managed bzip2 files support reads and continuing writes; explicit
bounded conversion stays in `core.preparation.file_bodies`, outside startup.
Policy relocation and generic fixture inventories live in
`tests/file_custody_support.py`. Ordinary local actual-library checks use the
immutable baseline and private snapshots in the [test guide](../../tests/README.md#local-actual-library-workspaces).

## Domain ownership and retention

| Owner | Runtime truth | File ownership | Expiry |
| --- | --- | --- | --- |
| Provider alternatives | typed current selection and retry/freshness SQL | recording/provider/capture | 7 days, capture mtime; manual selections and successful paid results survive independently |
| Served diagnostics | compact served facts, attribution and expiry SQL | immutable impression ID | 30 days, original served mtime, coordinated with #687 |
| Prediction provenance | predictions, current scores, original measured cohorts, one fingerprint/size/state per calculation | calculation ID in bounded 128-calculation chunks | superseded calculations after 30 days, saved calculation mtime; current references and snapshots protected |
| Musical profiles | existing recording/source head, digest, version, publication time; typed file head with publication identity and logical size | immutable publication chunk, at most 128 profiles | active heads durable; superseded publications 30 days from captured ctime, after snapshots release |
| Embeddings | canonical recording/artist subject, space/basis/dimensions/order and publication | immutable bounded binary chunk with exact float32 bytes | current and required model bases durable; unreferenced publications 30 days from ctime |
| Recovery and pending operations | typed operation/phase/target/availability | existing domain owner | durable while needed for recovery or pending work; age alone never authorizes removal |
| Listening retention inventory | reconstructible `.retention-inventory` metadata beside immutable evidence | listening evidence owner; FAO atomic cache, with file identities rechecked each turn | one entry per inventoried evidence file; completed scans remove stale entries; never contains model arrays or authorizes deletion independently |
| Samples and required models | sample/model version and exact identity | existing sample/model owner | durable while required inputs; no startup refetch/retrain |
| Historical policy configuration | compact attribution identity and shared-base closure | existing policy identity | useful replay bodies 180 days from original publication mtime; disposable unused configurations deleted |
| Cold legacy originals | domain manifests and exact kind/source identity; online ambiguity/redirect/scope guards stay SQL | domain-specific journal | useful diagnostics 180 days from original mtime; paid/user/unknown-scope recovery durable; obsolete originals require verified disposition |

The latter domain conversions remain in progress until their implementation,
isolated actual-library measurements and recovery checks are recorded on their
tickets. This table is a contract, not an acceptance claim.

## Musical-profile file DDL and publication (#693)

Reuse `rec_musical_profiles` and `rec_musical_source_profiles` for head
digest/version/time and invalidation. Add two typed, domain-specific file heads:

- `recording_profile_files(recording_id PK FK recordings, publication BLOB(32), byte_size INTEGER positive)`.
- `source_profile_files(source_key PK FK rec_musical_source_profiles ON DELETE CASCADE, publication BLOB(32), byte_size INTEGER positive)`.

Each publication is an immutable canonical document containing at most 128
exact original JSON strings indexed by owner. Its SHA-256 identifies the bounded
chunk, not a filesystem locator. File names derive from domain and publication.
The marker is required only when the compatibility payload is empty. The head's
digest and version remain the source of truth; reads verify both against the
chunk. Budget checks use file-head logical sizes before reading any bodies.
Files publish atomically before marker/head publication. A rollback leaves a collectable
orphan and the old SQL head usable. Old snapshots pin their prior publication.
Conversion clears a payload only after exact verification, in bounded resumable
prefixes. The old columns remain for supported installed reads under #622.

## Embedding file heads (#694)

Add `recording_embedding_files` and `artist_embedding_files`, each with a
canonical subject FK, constrained space, exact model basis, dimensions,
publication SHA-256, chunk position, original insertion order and built time.
The composite primary key is `(subject, space, model_version)`. The old vector
tables remain readable while preparation moves rows into these typed heads.
This avoids rebuilding populated tables or weakening their float32 constraints
during a regular migration. Domain reads combine the two representations;
publication and retraction update them together under the existing writer guard.

A binary chunk contains a versioned header and at most 128 equal-dimension
vectors. SQL position selects the exact float32 range after bounded FAO decoding;
the publication hash checks the complete immutable chunk. There is no JSON float
conversion, precision change or one-file-per-vector layout. Discovery order is
carried explicitly when a legacy row moves. Space/basis checks include both
representations and redirects retire both. Conversion verifies each vector
before deleting its legacy row. Required heads never expire; superseded chunks
use the same 30-day ctime policy and snapshot/writer fences as musical profiles.

Continuing installed SQL writes carry a small temporary insertion-order relation.
Its insert/update/delete triggers preserve converted owners' positions and append
new owners after both representations, including when SQLite recycles rowid after
complete conversion. These facts are removed with their legacy vector rows;
file heads retain the order after conversion. Indexed maxima bound allocation
without scanning vector bodies. Regular change 25 installs this compatibility
owner without rewriting installed vectors.

Profile and embedding collection visits at most sixteen directory entries or
empty-shard transitions per turn, including hidden entries. It streams one flat
hash shard without sorting or materializing the owner tree. A persisted shard
position survives restart; an interrupted shard is safely re-examined. At most
eight traversal cursors are retained, and application connection shutdown closes
its iterators. Eviction also closes an iterator and repeats only its shard.
Each bounded turn holds the snapshot fence and SQLite writer reservation while
rechecking current publication ownership and deleting the exact observed inode.

Policy-history, prediction-provenance and cold-archive collection charge shard discovery,
skipped entries and transitions against the same sixteen-entry turn allowance.
They retain lazy root/shard iterators and only two candidate shard names, without
listing or sorting the tree. Completed shard progress survives restart; a partial
shard repeats safely, and later turns wrap to newly superseded publications.
Cold archives use one cursor across their eight domains, so crossing domains
also consumes work without retaining a directory-sized list or eight cursors.

The test exporter stages every required backup member in a hidden private tree on the
destination filesystem, verifies its logical digest and size, then publishes
those same bytes after the whole inventory passes. It preserves both logical
clocks and uses a constant number of open streams. Corrupt or incomplete
inventories leave existing publications intact and remove private staging.
Export hashes the copied destination so the manifest describes its actual bytes
even when a producer replaces its source during copying.

Preparation backup and restore carry both clocks from the pinned, verified source
reader. Rewriting nested plan coordinates for a different database root preserves
those clocks as well; relocation does not become a new retention publication.
Sample and listening-evidence recovery preserve the verified logical bytes and
both saved clocks through their domain publishers too.
Packed policy, group, metadata and compatibility-evidence recovery retains the
verified reader's clocks per range. Repacking never combines different ages;
source-file ordering keeps existing compact packs together within the normal
byte limits. Recovered packs include a bounded custody-clock header suffix in
their content-addressed identity, so equal retained records from differently
aged packs cannot overwrite each other's clocks. Metadata's temporary recovery
map keys content by source file as well as digest; sharing within one source
pack remains intact. No persistent SQL file registry is added.
Annotation, paid-description, recommendation-output, collection,
action-backup and pending-feedback copies carry the same pinned source clocks.
New backup manifests have their own publication age and do not renew the bodies
they describe. No additional per-path metadata registry is introduced.

## Source audit boundaries (#689)

`python -m experiments.retained_copy.fao_audit` inventories production file
opens, body reads/writes, JSON decoders and filesystem replacement/copy/delete.
Each remaining direct operation names its concrete boundary and function in
that inventory. `tests/core/test_fao_adoption.py` rejects unclassified operations
and demonstrates that a newly introduced direct body reader fails the audit.
Runtime asset bytes, including packaged catalogs, configured models, static
delivery and playlist backups, pass through FAO. SQLite custody and schema/code,
TOML credentials, external network/zip streams, OS diagnostics and coordination
files are the explicit non-asset boundaries.

Bundled schema resources have exact reader boundaries: `schema_admission.reference`
loads the frozen `installed_v079.json` schema contract;
`schema_definition._statements` reads the packaged SQL definitions; and
`schema_transforms.saved_output_definitions` reads the packaged recommendation
settings schema. These are application code resources. Other file readers in
those modules still require their own classification or FAO access.

Current database replacement keeps installed file roots in place. Private SQL
candidates and retained originals use `db_rebuild.files.borrow`, preserving file
identity and continuing publication through those same roots. Only SQL is staged.
The `_move` boundary remains for installation and rollback of persisted older
`file_roots` plans, with application handles drained. It preserves complete
directories, including locks and superseded publications. Required file
verification uses FAO.

The current build and install share SQLite verification in
`core/db_rebuild/sqlite_checks.py`. The older policy-cleanup rehearsal and SQL-policy
repair worker have been removed. Domain export/restore tests use
`tests/file_custody_support.py` to create disposable snapshots without migration,
policy cleanup or installation. Playback-inbox backup machinery is research-only
under `experiments/retained_copy`; production capture, replay, receipts and
observation collection remain in their application owners.

Renewing an immutable artifact's retention lease uses `fao.touch`, which updates
its saved logical modification time while preserving exact content and captured
creation age. Changing the compressed inode's filesystem timestamp alone does
not renew that lease. Renewal verifies and replaces the bounded stream atomically;
a failed renewal leaves the previous artifact intact.

Explicit resumable preparation is available as:

```
uv run python -m yt_playlist.core.preparation DATABASE --domain profiles --batches 10
uv run python -m yt_playlist.core.preparation DATABASE --domain embeddings --batches 10
uv run python -m yt_playlist.core.preparation DATABASE --domain vibe_provenance --batches 10
uv run python -m yt_playlist.core.preparation DATABASE --domain legacy_journals --batches 10
uv run python -m yt_playlist.core.preparation DATABASE --domain policy_history --batches 10
uv run python -m yt_playlist.core.preparation DATABASE --domain compression --batches 10 --assets MANAGED_ASSET_TREE
```

Compression defaults to 32 MiB logical bytes per tick. Use `--logical-bytes`
explicitly for larger managed files, up to the accessor's 512 MiB limit.

Compression ticks inspect at most sixteen directory entries and admit at most
32 MiB of logical input. Directory traversal streams without collecting the
tree. A compressed file's verified custody record is its durable completion
marker: restart may rescan metadata, but never recompresses completed files.
Conversion retains one authoritative logical path. File-owned SQL heads and
journal progress are committed in bounded owner transactions, independently
of ordinary migrations and startup. Additional asset roots are explicit so a
conversion never guesses that SQLite, configuration or user imports are assets.

Idle retention also collects established policy, provider, sample, annotation,
paid-description and compatibility-archive orphans in sixteen-entry turns.
Only bodies with no required relational owner qualify, thirty days from saved
ctime. Required samples and successful paid/manual results remain durable.
Snapshot leases, writer reservations and exact inode checks fence deletion.
The worker closes directory iterators at shutdown. Provider alternatives keep
their seven-day owner expiry; acknowledged choice and served diagnostics keep
their thirty-day contracts. Superseded profile/vector/journal pages use thirty
days, and useful historical policy/archive diagnostics use 180 days. Explicit
failed publication cleanup and removal of obsolete derived outputs are owner
dispositions, separate from expiry of retained history.

Prediction preparation chooses exact original IDs and contributor sets on its
private guarded snapshot. Compression and source integrity verification complete
before publication. Only prepared rows and atomic file renames enter the SQL
publisher. Allocation/current/cohort revisions and the observed physical chunk
identity reject stale work. Abandoned staged files release their leases and
are removed on cancellation or a rejected publication.

## Shared vibe model candidate (#819)

`repos/vibe_models.py` owns all new SQL, including a small typed
`prediction_model_generations(prediction_id PK, generation BLOB(32))` reference
and its generation index. Parameters and input edges never enter that table.
The `core/vibe_model_files.py` adapter performs physical FAO operations only.
Regular change 31 creates bounded DDL and invalidation triggers, without fitting
or converting populated data. Installed `album_tracks` and current occurrence
owners have separate trigger/query paths; compatibility remains in place.

Each immutable `vibe-models/HH/SHA256.json` contains one original-only training
manifest, exact measurement/configuration identities, canonical credits and
specific genres, unambiguous album identities, source/version groups, fitted
variance components, diagnostics, and a packed conditional Gaussian posterior.
Formats 2 and 3 store sparse effect precision and conditional effect means instead of
a dense recording Cholesky triangle. Format 1 remains readable during continuing
preparation. Artifacts remain bounded at 16 MiB; input capture is byte-bounded
and has a 32,768-recording sentinel instead of the old dense 1,024-row ceiling.
Fitting still admits 128 MiB of accounted working data and a ten-second
cooperative work budget. Symbolic minimum-degree elimination bounds graph and
factor fill before numeric factorization, using the same ordering throughout
optimization. These limits do not establish measured RSS or a hard interruption
deadline inside BLAS. Oversized or nonconvergent fits defer without sampling or
deleting useful heads.

The candidate jointly conditions baseline, individual artists, genre,
artist/genre interactions, album and source effects. Variance components use
marginal maximum likelihood with the existing numeric bounds; central finite
differences evaluate the same Gaussian marginal likelihood through the sparse
mixed-model equations. The optimizer permits at most 100 iterations within the
unchanged ten-second cooperative budget. Fewer than four originals
use an explicit prior. One measurement per recording cannot identify classifier
noise separately from real track variation. The residual retains both; known
replicate noise can be supplied separately. Source/version grouping does not
establish provider-specific reliability or remove shared classifier bias.
Hyperparameter uncertainty is omitted by the conditional plug-in posterior.
Suitability is a continuous mean, not an independent vote or calibrated
probability. Returned scores are clipped to [0,1].

Format 3 predictions average the fitted source/version effects over their
canonical-original recording frequencies. The unspecified target source is
not treated as a new, zero-effect classifier. This finite empirical mixture
does not identify a source-free human suitability scale or remove shared bias.
Latent-mean variance includes covariance of the weighted source mean;
new-track variance also includes the mixture's conditional source variance
and between-source mean variation. Neither target measurement nor target
source selects the mixture. Formats 1 and 2 retain their original readers and
prediction semantics. A supplied but unseen artist/genre identity uses its
new-group prior, with an interval; missing genre/album groups retain their
prior variance. If both artist and genre are absent, the existing explicit
heuristic fallback remains. No new SQL source kind or migration is introduced.

For this candidate, `support` means mean input coverage multiplied by the
fractional reduction in latent-mean variance. It is neither suitability nor a
new-track confidence interval. A compact calculation receipt stores artists,
generation, target genre/album conditioning, latent-mean variance and new-track variance; new-track variance adds
the fitted between-track/noise residual and source-mixture variation. The existing SQL `variance` column
remains unset for joint predictions. `recordings` counts the whole shared fit,
not an independent effective sample size. Existing source kinds describe
available artist/genre conditioning, rather than a selected exclusive cohort.
Existing receipts without target conditioning remain readable and writable
during compatibility preparation. New runtime receipts include it, so changing
the target album creates a distinct calculation even when scores and variances
are unchanged. This metadata stays in FAO rather than new SQL contributor rows.

Fit artifacts are staged and verified off the request path on a consistent
snapshot. Files publish atomically before guarded SQL heads. Bounded score batches
retain their actual generation and exact calculation identity; unchanged means
with changed originals still receive new receipts. A capture loads one posterior
per bounded worker turn, retaining only target estimates and one guarded snapshot
between turns. Its pending queue is acknowledged only on final publication.
Shutdown, storage pauses and batch expiry close idle snapshots and file-reader
leases; cancellation and stale publication leave the durable queue for retry.
Product score readers use SQL and do not read these files or fit.
Missing/broken current heads queue repair while retaining useful old scores;
ordinary startup and failed fitting do not demand a full successful refit.

Current heads and ready calculation receipts protect model generations.
Deliberately expired history returns unavailable before reading its model;
missing required bytes are corruption. Collection examines at most sixteen raw
entries per normal turn (hard maximum 128), retains unreferenced generations
for thirty days from saved ctime, and uses reader/build leases plus an SQL
writer reservation. Failed publications may leave collectible durable orphans.
The shared file-owner inventory includes models in backup, restore and rebuild
fences. Cold provenance expansion resolves the shared manifest when requested;
rebuild publication preserves compact receipts instead of per-track graphs.

Production qualification is intentionally empty in
`rec/vibe_joint_preparation.py`. Controlled test fixtures explicitly enable the
candidate; they do not qualify it. Populate its configuration-to-report mapping
only after the frozen #819 statistical, lifecycle and resource gates have
independent passing evidence. Until then the existing policy remains active.
This is a candidate source handoff, not an accepted model or measured saving.

## Cold archive journal (#697)

`legacy_archive_heads(domain PK, publication BLOB(32))` is one transactional
head for each of the eight explicit archive domains, not a per-original receipt.
The immutable FAO hash trie stores exact `(kind,source_id)` keys, original text,
reason, capture time and historical subject bindings. Leaves hold at most 128
records and 8 MiB. Branches have at most sixteen children. A lookup follows
only its hash path. Updates copy changed pages before the SQL head moves;
rollback preserves the old head and leaves ordinary collectable orphan pages.
Insert-once behavior includes expired tombstones so retries never recapture a
different original. Backup follows the committed domain roots and needs no old
receipt rows or producer directory. The old tables remain compatibility sources
until explicitly verified retirement under #626.

Known conversion/reconciliation originals have a 180-day diagnostic/recovery
window from `archived_at`, independent of `resolution_status`. Actions, recipes,
canvases, feedback and playlist scope recovery retain durable user decisions.
Explicit paid/user or unknown-scope obligations are durable, and an unknown kind
requires classification before any age deletion. Typed ambiguity, redirect,
scope and current catalog relations remain unchanged.

All managed JSON/assets, including packaged model/catalog/static resources,
must use FAO. SQL/schema source text, native SQLite custody, TOML/secrets,
network response parsing, OS diagnostics, developer logs and interprocess lock
or fixed counter files are implementation/third-party boundaries. These do
not represent managed asset bodies. Historical frozen migration definitions
remain source contracts; their domain publishers/readers use FAO.

Acceptance requires an AST inventory of file opens, JSON load/dump and copy/
delete calls, with explicit boundary entries. Passing FAO unit tests alone does
not establish complete application adoption or native-library acceptance.
