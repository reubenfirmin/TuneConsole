# Managed files and cold runtime bodies

The [runtime file inventory](../../src/yt_playlist/core/runtime-file-owners.md)
owns current SQL receipt IDs, paths and publication order. The
[database maintenance guide](../../src/yt_playlist/core/README.md) defines the
supported release baseline, ordinary upgrades and older-installation resets.
Historical bulk converters and preparation commands have been retired; their
original contracts remain in the [v0.1.19 source](https://github.com/reubenfirmin/TuneConsole/blob/v0.1.19/experiments/retained_copy/FAO-OWNERS.md).

## File access

`core.fao` exposes `write(path, content)`, `read(path)`, `prepare(path, content)`
and `delete(path)`. Read results supply bounded `.bytes` or a streaming context,
logical metadata, temporary decoded paths and HTTP responses. Writing a read
result streams a verified copy with its logical clocks. Writing edited content
from `content.replacing(bytes)` also checks the observed source identity.
Prepared files publish by atomic rename and remove private temporary bytes when
closed. Compression, integrity checks and file-reader leases stay inside FAO.

The physical file contains a bzip2 stream and a fixed custody trailer: format,
logical size, original mtime, captured inode ctime and SHA-256 of the original
bytes. Plain installed files remain readable. Relocation and recovery preserve
both age clocks; publishing changed content renews them. Domain SQL receipts
establish logical ownership; the compression format carries physical metadata.

Readers bound logical bytes and cumulative decompression work. Real-path users
hold a temporary materialization lease; range delivery uses that same original
content and preserves logical content length, type, modification time and ETag.
Ordinary writes atomically replace the logical name without a decompression
reread or synchronous file/directory flushes. Failed compression or rename leaves
the prior publication intact. Playlist backups authorizing remote deletion use
`Content(bytes, durable=True)`; FAO completes their disk durability boundary
before returning. Cache and evidence writes do not inherit that protocol.

Inode leases and domain snapshot fences protect active consumers from
collection. Owners retain content validation, SQL publication guards, evidence
ID conflicts, storage budgets and retention eligibility. `file_collection`
provides bounded traversal and coordinates collectors with retained SQL views.
Policy relocation and generic fixture inventories live in
`tests/file_custody_support.py`. Ordinary local actual-library checks use the
immutable baseline and private snapshots in the [test guide](../../tests/README.md#local-actual-library-workspaces).

## Domain ownership and retention

| Owner | Retention |
| --- | --- |
| Provider alternatives | Seven days from capture mtime; manual selections and successful paid results survive independently. |
| Served diagnostics and acknowledged choices | Thirty days from original served mtime. |
| Prediction provenance | Superseded calculations after thirty days; current references and snapshots remain protected. |
| Musical profiles and embeddings | Active heads and required model bases remain durable; superseded publications expire after thirty days from captured ctime. |
| Samples and required models | Durable while required inputs; no startup refetch or retrain. |
| Recovery and pending operations | Durable while needed for recovery or pending work; age alone never authorizes removal. |
| Historical policy and archive diagnostics | Useful replay bodies expire after 180 days from original publication mtime; paid, manual and unknown-scope recovery remains durable. |
| Listening retention inventory | Reconstructible scan metadata beside evidence; it never independently authorizes deletion. |

Idle collection uses bounded turns, persists traversal progress and closes
iterators at shutdown. Snapshot leases, writer reservations and exact inode
checks fence deletion. Explicit failed-publication cleanup and removal of
obsolete derived outputs are owner dispositions, separate from expiry of
retained history.

Renewing an immutable artifact's retention lease uses `fao.touch`, which updates
its saved logical modification time while preserving exact content and captured
creation age. Changing the compressed inode's filesystem timestamp alone does
not renew that lease. Renewal verifies and replaces the bounded stream atomically;
a failed renewal leaves the previous artifact intact.

## Copy and recovery

Current database replacement keeps installed file roots in place. Private SQL
candidates and retained originals use `db_rebuild.files.borrow`, preserving file
identity and continuing publication through those same roots. Only SQL is staged.
The `_move` boundary remains for installation and rollback of persisted older
`file_roots` plans, with application handles drained. It preserves complete
directories, including locks and superseded publications. Required file
verification uses FAO. Build and install share SQLite verification in
`core/db_rebuild/sqlite_checks.py`.

Domain export/restore tests use `tests/file_custody_support.py` for disposable
snapshots. They verify destination bytes before publication and preserve the
verified source's logical clocks. Backup manifests have their own publication
age and do not renew the bodies they describe. Production playback capture,
replay, receipts and observation collection remain with their application owners.

## Source audit boundaries

`python -m experiments.retained_copy.fao_audit` inventories production file
opens, body reads/writes, JSON decoders and filesystem replacement/copy/delete.
Each remaining direct operation names its concrete boundary and function in
that inventory. `tests/core/test_fao_adoption.py` rejects unclassified operations
and demonstrates that a newly introduced direct body reader fails the audit.
Runtime asset bytes, including packaged catalogs, configured models, static
delivery and playlist backups, pass through FAO. SQLite custody and schema/code,
TOML credentials, external network/zip streams, OS diagnostics and coordination
files are the explicit non-asset boundaries.

`schema_definition._read_statements` reads the release layout and seed SQL;
`destination_schema.definition` reads the compiled current schema. These are
application code resources. Other file readers in those modules still require
their own classification or FAO access.
