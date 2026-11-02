# Runtime history contracts (#691 and #698)

These are explicit preparation jobs, never startup conversions. The installed
tables and applied migration history stay readable while preparation runs.
Physical page reclamation belongs to #658; obsolete adapters belong to #626.

## Artist credits

Retain immutable `artist_credits` and ordered `artist_credit_members`, current
`artist_credit_capture_heads`, `entry_artist_selections` and `entry_artists`.
Current selection/head observations retain their original IDs and source/credit
agreement. Do not infer a current collaborator from an unresolved source label.

Add a domain-owned `artist_credit_replays` WITHOUT ROWID table:

```sql
entry_id INTEGER NOT NULL REFERENCES catalog_entries(id),
source TEXT NOT NULL,
delivery_key TEXT NOT NULL,
observation_id INTEGER NOT NULL UNIQUE,
credit_id INTEGER NOT NULL REFERENCES artist_credits(id),
metadata_bound INTEGER NOT NULL CHECK(metadata_bound IN (0,1)),
PRIMARY KEY(entry_id,source,delivery_key)
```

The immutable receipt records a delivery's original outcome, not a reference to
a live observation or metadata-delivery graph. A one-bit proof distinguishes a
verified metadata binding from an arbitrary key with the same spelling. Replaying it checks exact ordered
credit content, allowing canonical artist redirects, returns the original ID,
and cannot reselect an expired capture. Receipts do not expire: their continuing
purpose is preventing stale delivery replay. Distinct deliveries remain distinct.

Add singleton `artist_credit_retention` with a resumable scan cursor and explicit
activation/completion state and next daily scan time. The preparation job first verifies that all SQL
references to observations are the audited selection/head owners. Each bounded
batch rechecks those owners in its write transaction before deleting anything.

Disposition: unreferenced `legacy_capture` and `selection_snapshot` rows are
obsolete conversion history and are retired by the explicit job, including
unknown-time rows. No production historical reader was found: `select` requires
a live observation, ingestion uses a current head or delivery key, and modelling
reads the selected projection. Other unreferenced captures have a **30-day
last-seen policy**, using the recorded processing time (mtime semantics); replay
does not renew an already retired observation. Known delivery keys receive a
receipt before deletion. Explicit identity-reconciliation and unknown-time new
evidence are protected. No useful cold document was identified, so this job
creates no historical file store and has no dependency on #689. It does not
delete normalized credit content, selected user evidence or identity guards.

Regular installation creates only empty owners/guards; no history scan. New
deliveries publish receipts transactionally. Continuing bounded idle maintenance
is enabled only by explicit preparation; completed scans wait a day. Old formats without these owners keep
their existing read/write path. Backup/restore copies these relational facts.

## Taste modes

Keep every mode's ID, label, size, original times, space and active flag. Keep
active modes' centroid, family histogram and representatives exactly. Inactive
payloads have no production reader except the repeated legacy group import;
ordinary grouping stops importing those definitions. Existing legacy group
files/menus remain readable under their existing ownership contracts. Historical
menu choice does not justify recreating a centroid or track list.

Add `taste_mode_retired_payloads(mode_id PRIMARY KEY REFERENCES
taste_modes(mode_id), retired_at REAL NOT NULL)` as an explicit tombstone, and
singleton `taste_mode_retention(id, enabled, cursor, high_water, next_scan_at)`. The high-water
mark advances on publication/deletion and preparation, including installed
preexisting maxima, and never decreases. Inactive payload retirement writes
`centroid=X''`, `families='[]'`, `rep_keys='[]'`; their typed identity row stays for
FKs, historical picks/labels and unknown-old-ID resolution. APIs distinguish a
retired payload from a genuine empty/missing active vector. Historical summary
listing does not load vectors. Reactivation requires a complete newly published
mode payload and removes its tombstone; setting `active=1` alone is rejected.

Model comparisons capture their own immutable centroids (`model_pair_modes` or
installed comparison envelopes); current fitting reconciles active modes only.
Neither a retired ID nor the mutable old row is a model rollback snapshot. Held
menus capture their own content. Those owners stay untouched. There is no useful
additional cold snapshot to archive or expire. A live SQL reader retains its
SQLite snapshot across retirement; a late fitter is revision-fenced by its
normal publication contract. Retire inactive payloads only in bounded explicit
preparation and subsequent enabled idle maintenance, with active state rechecked
under the writer. New regular changes contain no bulk UPDATE.

Accounting must report SQL/index allocation and retained counts on an isolated
actual-library copy, exact current projections/vectors, continuing writes,
replay after expiry, restart and FK integrity. No native DB is changed.

## Actual-library measurement

Reproducer:

```sh
.venv/bin/python experiments/retained_copy/runtime_history.py \
  backups/658-restore-experiment-20261031/candidate/state.db \
  backups/691-698-runtime-history-report.json
```

Source SHA-256 stayed
`3e2b6a5e31d0d6bd57630b87fc34d367518e9ee9895ff6b88ad9661dc41dfa51`.
The job used a disposable SQLite backup; no native database or source asset tree
was changed. Scratch was removed after completion. These are owner allocations,
not a proposed replacement for the whole application database.

| Owner | Original SQL/index bytes | After 31-day expiry and compaction |
| --- | ---: | ---: |
| Artist observations plus new replay/state owners | 69,120,000 | 2,027,520 |
| Taste modes plus new retirement/state owners | 5,509,120 | 237,568 |

The total reduction is 72,364,032 bytes. New external logical/compressed/allocated
bytes are all zero. The report's `before_sql_bytes` includes the newly installed
empty owners (12,288 credit bytes and 8,192 mode bytes); the original-owner
baseline above excludes those installation allocations. This is independent of
the locator savings in #682 and does not recount #688's group definitions.

Initial preparation retained 17,861 of 715,213 observations; after the simulated
31 days, 14,343 observations remained, with 3,564 permanent replay receipts.
The initial prefix scan took 81.95 seconds over 5,588 ordinary 128-row batches;
the longest measured batch was 178 ms, including writer admission and commit.
Retiring 2,172 inactive mode payloads took 0.194 seconds over 18 batches (maximum
25 ms). Fourteen active modes stayed byte-identical; next mode ID was 2,187.
These are isolated timings, not a live UI latency acceptance claim.

Every selected ordered artist projection, selection, source head, normalized
credit/member row, active mode, mode summary and historical pick/impression
relation had the same hash before/after. Actual PPR rankings stayed identical
for all 14 modes and 2,800 ranked items. Every known delivery key replayed after
expiry without capture growth or selection/head changes; 96 repeated ordinary
capture writes also added no observation. FK checks and SQL backup/restart checks
passed. Current-source observations and recent unreferenced captures are retained
until their own eligibility permits retirement; the compacted figure is the
31-day lifecycle measurement, not an immediate startup conversion promise.

Focused contract coverage includes interrupted transaction/restart, a newly
introduced SQL owner refusing activation, current/recent protection, legacy
unknown-time disposal, replay after metadata graph deletion, fake metadata-key
binding rejection, artist redirects, immutable replay credit content, historical
FKs, ID high-water behavior, invalid reactivation payloads, active reader fencing,
stale model publication and idle scan throttling. Regular changes were checked
against the supported v79 installed prefix with its migration history preserved.
