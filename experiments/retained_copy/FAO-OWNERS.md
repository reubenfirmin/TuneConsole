# Managed files and cold runtime bodies

Implementation contract for #689, #693, #694, #696, #697 and #699. This work
does not change the native library. Explicit preparation and the #658 direct
destination are the only bulk conversion entry points. Applied migration
history stays intact; #626 owns subsequent compatibility retirement.

## File access

The logical path and original bytes stay stable. The physical file contains a
bzip2 stream and a fixed custody trailer: format, logical size, original mtime,
captured inode ctime, and SHA-256 of the original bytes. Plain installed files
remain readable. Conversion, relocation and recovery preserve both age clocks;
publishing changed content renews them. There is no SQL path/compression registry.

Readers bound logical bytes and cumulative decompression work. Real-path users
hold a temporary materialization lease; range delivery uses that same original
content and preserves logical content length, type, modification time and ETag.
Publication verifies the pending stream, fsyncs and atomically replaces its
logical name. Failure leaves the prior publication intact. Inode leases and
the existing domain snapshot fence protect active consumers from collection.

## Domain ownership and retention

| Owner | Runtime truth | File ownership | Expiry |
| --- | --- | --- | --- |
| Provider alternatives | typed current selection and retry/freshness SQL | recording/provider/capture | 7 days, capture mtime; manual selections and successful paid results survive independently |
| Served diagnostics | compact served facts, attribution and expiry SQL | immutable impression ID | 30 days, original served mtime, coordinated with #687 |
| Prediction provenance | predictions, current scores, original measured cohorts, one fingerprint/size/state per calculation | calculation ID in bounded 128-calculation chunks | superseded calculations after 30 days, saved calculation mtime; current references and snapshots protected |
| Musical profiles | existing recording/source head, digest, version, publication time; typed file head with publication identity and logical size | immutable publication chunk, at most 128 profiles | active heads durable; superseded publications 30 days from captured ctime, after snapshots release |
| Embeddings | canonical recording/artist subject, space/basis/dimensions/order and publication | immutable bounded binary chunk with exact float32 bytes | current and required model bases durable; unreferenced publications 30 days from ctime |
| Recovery and pending operations | typed operation/phase/target/availability | existing domain owner | durable while needed for recovery or pending work; age alone never authorizes removal |
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
Files are durable before marker/head publication. A rollback leaves a collectable
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

Restore stages every required backup member in a hidden private tree on the
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
byte limits. Annotation, paid-description, recommendation-output, collection,
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

Database replacement also has an explicit offline whole-root custody boundary
in `db_rebuild.files.stage` and `_move`. With application handles drained, these
operations preserve complete original directories for rollback, including lock
files and superseded publications; they do not decode or republish individual
assets. Copying referenced live bodies and validating a candidate use FAO.

Renewing an immutable artifact's retention lease uses `fao.touch`, which updates
its saved logical modification time while preserving exact content and captured
creation age. Changing the compressed inode's filesystem timestamp alone does
not renew that lease. Renewal verifies and replaces the bounded stream atomically;
a failed renewal leaves the previous artifact intact.

Explicit resumable preparation is available as:

```
uv run python -m yt_playlist.core.preparation.file_bodies DATABASE --domain profiles --batches 10
uv run python -m yt_playlist.core.preparation.file_bodies DATABASE --domain embeddings --batches 10
uv run python -m yt_playlist.core.preparation.file_bodies DATABASE --domain vibe_provenance --batches 10
uv run python -m yt_playlist.core.preparation.file_bodies DATABASE --domain legacy_journals --batches 10
uv run python -m yt_playlist.core.preparation.file_bodies DATABASE --domain policy_history --batches 10
uv run python -m yt_playlist.core.preparation.file_bodies DATABASE --domain compression --batches 10 --assets MANAGED_ASSET_TREE
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
private guarded snapshot. Compression, integrity verification and fsync complete
before publication. Only prepared rows and atomic file renames enter the SQL
publisher. Allocation/current/cohort revisions and the observed physical chunk
identity reject stale work. Abandoned staged files release their leases and
are removed on cancellation or a rejected publication.

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
