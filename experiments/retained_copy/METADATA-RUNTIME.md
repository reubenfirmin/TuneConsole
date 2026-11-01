# Runtime metadata storage (#681, #682)

This is an explicit preparation/cutover format. Opening an installed database
only installs empty owners; it does not convert history. Existing installed
readers and writers remain supported until a verified preparation is activated.

SQL keeps the selected recording/item facts already consumed by catalog,
recommendation training and vibe prediction. The additional owners contain:

| Owner | Runtime use |
| --- | --- |
| providers | One integer/name mapping per provider, shared by current facts |
| genres | One integer/canonical genre key mapping; aliases and redirects keep their identity |
| recording_genres | Current integer genre reference or unresolved label |
| recording_sources, entry_sources | One current provider code per subject/field, for provider priority |
| choices | One typed current manual choice per entry/field, including null, blank and withdrawn; revision identifies its JSON history |
| conflicts | One subject/field/status/revision summary, for badges and review queries |
| enrichment_provider_state | One current completion/retry summary per recording/provider; cycle, completion time, failures and deadline control scheduling |
| state | Cutover flag and bounded file collection cursor |

The SQL fields and indexes have these callers:

* `providers(id,name)` and `genres(id,genre_key)` have primary and unique keys
  for current attribution and canonical identity lookup. They contain no lookup
  history. Genre redirects resolve retired keys at read time.
* `recording_genres(recording_id,genre_id,unresolved)` has one primary-key lookup
  per recording. `recording_sources(recording_id,field,provider_id)` and
  `entry_sources(entry_id,field,provider_id)` have only their composite primary
  keys. Provider priority uses these current field suppliers.
* `choices(id,entry_id,field,provider_id,revision,kind,text_value,number_value,genre_id)`
  has a stable primary key and unique `(entry_id,field)` lookup. The typed value
  serves the effective catalog view; kind distinguishes null, blank, withdrawal,
  text, number and canonical genre. Revision locates explicit decision history.
* `conflicts(id,recording_id,entry_id,field,status,revision,updated_at)` has its
  primary key and one unique partial index per subject scope/field. They serve
  unresolved badges, review pages and resolution; no candidate values enter SQL.
* Existing `enrichment_provider_state` retains `recording_id,provider,cycle,at,status,failures,retry_at,error`.
  Its recording/provider key supports scheduling and coverage. Cycle separates
  stale sweeps, time rejects older completions and locates optional alternatives,
  status prevents refetching successful/no-match work, and failures/deadline
  implement backoff. `error` is an empty compatibility column after cutover;
  error bodies move to JSON. Provider/status strings here remain part of the
  installed scheduling contract; no historical attempt rows are added.
* `state(id,active,gc_cursor,gc_after,gc_shard)` has one row. Workers visit a
  bounded set of current subjects and filesystem entries, including orphaned
  alternatives whose provider or recording no longer exists. Shard progress
  survives restart; collection skips pinned readers.

Field codes are fixed in code and append-only. The selected genre text key in
the existing catalog owner remains a compatibility projection until the broader
genre owner cleanup. Other than current selected values, SQL has no provider
response, alternative list, error body, filename, path or content checksum in
this format. Existing historical owners remain frozen until separately verified
retirement (#626); they are not copied into the runtime-only experiment.

Files live under `<database filename>.metadata-runtime`, beside the database.
Their relative names are computed from existing owner primary keys and revisions:

* `alternatives/<recording shard>/<recording id>/<provider code>/<completion time in hex>.json`
* `native/<source shard>/<deterministic encoding of source key>/<source revision>.json`
* `choices/<choice shard>/<choice id>/<revision>.json`
* `conflicts/<conflict shard>/<conflict id>/<revision>.json`

The source key is an existing compound string primary key. SHA-256 encodes it
into a bounded filesystem component; the encoding is never repeated in SQL.
No pack offsets are needed for these new files. Existing packed delivery
responses and unrelated owners retain their existing supported adapters until
their separate cutover. This change does not rewrite artwork, vibe, group,
recommendation-policy or taste-artifact storage.

The remaining external-file inventory is outside this metadata cutover:
album description/access/request/identity/note owners; artist and discovery
identity evidence; annotations and sample attributes; group definitions,
generations and lineage; prepared recommendation outputs, policy bodies,
reports and home displays; listening outputs; and the domain-specific legacy
evidence owners. Their locator/hash columns retain their installed adapters.
Input, profile, model and comparison digests used for invalidation or identity
are also distinct from redundant file-integrity bookkeeping. General retirement
of these supported formats remains separately tracked in #626.

The old metadata `contract_digest` served delivery identity/idempotency;
`raw_digest` served response deduplication and integrity. New runtime writes
use captured provider/run/input identity and current completion time for replay
checks, rather than a SQL digest index. Integrity lives in the envelope and
backup manifest. No SQL content-dedup lookup is needed for the small retained
current documents. Installed packs still require their existing `raw_offset`
and `raw_byte_size`; those adapters and frozen rows remain compatible until
#626. The isolated SQL copy has zero rows in those historical owners and zero
payload bytes in their filename/checksum indexes; it preserves their empty DDL
and migration history rather than editing an applied migration.

Each JSON envelope carries its owner, format and integrity digest. Backup
manifests carry sizes and digests. SQL does not query them and stores no copy.
Publication writes and fsyncs a temporary sibling, atomically renames it, then
publishes its revision in the owning SQL transaction. Committed revisions are
immutable. A rolled-back prospective revision may be replaced under the SQLite
writer reservation, since no committed snapshot can refer to it.

Provider alternatives and candidate details expire after seven days. Expiry
does not remove selected facts, reset completion/retry state, or schedule a new
request. Current native model inputs and explicit manual decisions do not share
that expiry. Missing optional alternatives produce an empty dialog list;
missing/corrupt required model or manual documents fail verification explicitly.
File collection respects readers and backups pinned to older SQL snapshots.

Preparation exports a bounded batch at a time, with a durable cursor and source
revision checks. Cutover verifies the prepared source snapshot and current
values before switching readers. Continuing writes during preparation either
invalidate the affected prepared batch or cause final verification to refuse
activation. Regular migration history is preserved. Live/native databases are
not used as experiment destinations.

Run preparation only against an isolated copy with its installed external files
available. Repeat this command until `complete` is true, then pass `--activate`:

```sh
.venv/bin/python -m yt_playlist.core.preparation.metadata_runtime COPY.db --progress PROGRESS.json --batches 20
```

If source writes invalidate final verification, `--restart` discards inactive
progress and re-exports the source. It cannot reset an already active format.
There is no automatic startup conversion or default activation. Destructive
retirement of installed history and compatibility code remains #626, after
verification of this experiment.

The actual-library reproducer creates and verifies its own disposable backup,
then leaves a separate runtime-only candidate and JSON directory:

```sh
.venv/bin/python -m experiments.retained_copy.runtime_metadata READ_ONLY_BASELINE.db NEW_OUTPUT_DIRECTORY
```

`exercise(candidate)` in the same module tests 100 continued provider results,
manual override and reset after reopening, and SQL growth using another
TemporaryDirectory. It leaves the retained candidate unchanged. Schema opening
and work-readiness comparisons are measured separately from starting the web
application; the experiment does not launch the native app.
