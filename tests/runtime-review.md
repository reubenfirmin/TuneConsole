# Test runtime review: #534

Fixture direction clarified by the user: ordinary consumers use one genuinely
built empty current-schema template and private copies. The existing
`_pristine_current_schema`, `store`, `file_backed_store` and `initialize_consumer`
already implement that path; custom helpers must adopt it instead of constructing
new schemas. Historical consumers use the existing genuine versioned templates;
actual bootstrap, admission, reopen and subject migrations remain exercised.
`schema-initialization-sites.json` inventories the remaining calls for review;
its static candidates include intentional startup checks and are not blanket edits.

Final inventory, 2026-11-03 22:47 UTC: **13,299 unique cases ended: 13,170 passed,
85 failed and 44 skipped.** The original 13,242-node manifest gained 58 nodes.
One original growth-poll node was explicitly retired and replaced by three
passing Home-poll variants; no other cases are unaccounted. Final accounting and
failure-owner assertions pass. All 94 historical failed phases and 100 JUnit
diagnostics have owners; historical failures are not automatically current ones.

Four instrumented segments took **4,523.21s, 4,489.51s, 3,700.11s and 961.77s**,
total **13,674.60s (3h47m55s)**, including 311 repeated completed attempts.
First launch to completion was about 4h33m with recovery/investigation gaps.
This is not a clean-revision full-suite timing or ordinary 900-second acceptance.
Selected worker phase sums: 12,174.69s setup, 38,734.25s call, 538.76s teardown.

The coordinator reused the active third-segment coverage destination for a
focused check, causing a reporting collision and internal abort (#970). A safe
snapshot survived; the final segment reran all 242 affected ended nodes and the
unfinished work with a new destination. Renewal and final coverage integrity
pass. The colliding focused data file is excluded from the healthy global union.

Final reports are in `build/test-reports/534-full-analysis/`: per-test/per-file
CSV, complete collection accounting, timing summary, failure ledger, merged
coverage, separate body/setup/all overlap groups, semantic dispositions, scope
and collision audits, commands and final environment/source hashes. Validation:
`.venv/bin/python build/test-reports/534-full-analysis/export_inventory.py --final`.
All four disposable run workspaces were already removed after process exit.

Latest measured owners include #972 candidate input (543.48s call), #973 calendar
and current-copy empty-schema setup, #974 Vibe reader input and #980 daily-selection
input. #918 also covers its importing build-time Vibe browser (152.67s setup,
50.54s passing body). New failure owners #967–#971 and #975–#978 preserve exact
diagnostics and the distinct behavior that must remain. #534 stays In progress
for integration, original recommendation dispositions and ordinary acceptance.

## Historical checkpoints

The remaining notes record earlier snapshots and investigations. Their partial
counts and active-run statements are historical; the final inventory above is
the current execution/accounting status.

The second segment ended with exit 3 after a checkpoint test globally replaced
SQLite connection creation and broke coverage saving. #946 now scopes that
injector to its module and asserts the intended checkpoint was attempted. Its
focused ctrace renewal passed all 12 cases in 172.66s; all 12 also passed in the
third global segment. Retained global contexts are in
`build/test-reports/534-checkpoint-injection/coverage-global-renewal.sqlite3`.
The ticket explains the overwritten focused data file and replacement provenance.

The third segment runs with four workers and the normal machine lock, skipping
403 fully completed files. Planned selection: 3,663 cases, including 24 repeats
from unfinished files; actual collection: 3,679. Exact collection drift and
original-node completeness are audited separately. Its reports are under
`build/test-reports/534-full-analysis-tail/`. There is no pace cutoff during this
explicitly requested complete inventory. The timings describe an instrumented
multi-segment inventory, not ordinary 900-second acceptance or a clean revision.

Additional measured work: #951 covers background-writer/checkpoint private schema
inputs, #952 artist/album browser setup and closure, and #953 event timestamp
consumer setup. New failure tickets #954/#955 cover stale playback-deferral
fixtures and the real extension's missing initial setup tab. Existing #867 was
reopened for an intermittent short-desktop launcher failure, and #894 for two
additional cluster-route consumers of the stale Saved receipt. Each includes
focused diagnostics and preserved behavior. #945–#950 retain the earlier failure
groups. #534 remains In progress. Earlier snapshots below remain historical
evidence, not current status.

Further Ready findings: #956 skip-read schema consumers; #957 genuine v79
metadata prefixes (210.36s measured setup, including all importing consumers in
validation); #959 four private file-backed consumer fixtures; #961 an explicit
duplicate 20-case group-learning wrapper matrix; #962 Move, Cleanup and QA
schema consumers (334.42s setup) and reliable browser cleanup. #958 independently
reproduces a Home budget test rejecting legitimate slow-publication logs. Existing
#727 also owns the independently reproduced saved-playlist TV return without an
iframe. Every ticket retains its distinct behavior and real subject operations.

Later Ready failure owners: #963 current startup facet read probe (whole-file
renewal: 2 failed, 6 passed in 17.88s); #964 pending-cleanup/startup ownership;
#965 persisted temporal cuts through typed publication (combined reproduction
of the latter two groups: 3 failed in 40.07s); #966 immutable historical group
progress observation (1 failed in 9.12s). #904's fixture validation includes its
19 direct importing files; #937 includes its lockup-browser consumer. Original
receipts and recovery validation are retained under
`build/test-reports/534-full-analysis-original/`. Zero package coverage in browser
JavaScript, subprocess and outside-package tooling checks is recorded as a scope
limit and is not treated as duplication.

Current coordinator run, 2026-11-03: the unexcluded full suite is running with
four whole-file workers and pytest-cov test contexts. Exact collection: 13,242
cases in 1,048 files. Reports and the collected-node manifest are under
`/tmp/tuneconsole-534-full-analysis/`; `overlap-live.json` is explicitly partial.
No completed whole-suite timing or coverage claim is made until its final
collection audit succeeds. This run includes earlier interrupted tails.

The user authorized distributing measured work through Ready while the
coordinator retains global-suite ownership. New #534 subtasks #890–#898 cover
Home durability construction, comparison setup/evidence, repeated progress
rebuilds, private analytics setup, cluster response assertions, Spotify import
bootstrap, the live Home request double, Radio owner/genre fixtures, and metadata
provenance. Each contains measured evidence, a focused command and retained
behavior. Related live failure-detail persistence evidence is on existing #814.
Additional Ready subtasks: #901 owns the nine observed generated-launch browser
failures, with a focused popup-closure reproduction and retained trace; #902
targets the repeated rebuild prefix in eight captured-evidence verifier cases
(253.50s completed call time), retaining every damage and restart contract.
Further findings: #903 covers repeated bootstrap in 25 passing live-play cases
(315.68s call sum); #904 covers 400.71s of repeated predecessor setup across 103
passing migration cases; #905 tracks eight failing Taste ownership probes with
two independent focused reproductions. Actual concurrency contracts remain.
Ready #906/#907 cover repeated current-schema construction in lookup ownership
and metadata-runtime preparation. Initial completed call sums were 302.88s for
21 lookup cases and 270.60s for 20 metadata cases; both retain their full 24-case
files and distinct real writer, corruption, preparation and recovery checks.
Those two files subsequently completed: 24 passed each, with 347.33s and 334.24s
call sums respectively. #912 covers repeated real evaluation baselines while
retaining chronology/evidence thresholds; #913 covers repeated empty schema
construction before staged-publication checks. All 30 failures observed at the
5,068-case snapshot have ticket mappings in the live failure report.
#914 covers repeated prepared-input construction before browser Home polling,
hover and control checks; real cold/incomplete preparation and recovery remain.
#915 covers analytics reader lifetime setup (23 passed, 212.24s setup), retaining
both memory/file paths and actual WAL, deadline, closure and concurrent writers.
#913 and #912's experiment ledger also completed 24 cases each; their ticket
bodies now contain complete phase totals.
#916/#917 cover repeated private schema setup in enrichment queue concurrency
and listening observation consumers, preserving their full behavior matrices
and the observation file's three genuine historical-layout/upgrade controls.
#918 covers repeated measured-sound input in Home Vibes (23 passed, 408.47s
call); all four expensive consumer cases and their distinct assertions remain.
#914 browser Home and both #916/#917 files completed 23 cases each; complete
phase totals are recorded in their ticket bodies.
Radio intensity already uses a genuine module-level prepared template with
private restores, so its initial heavy calls did not justify another bootstrap
reuse ticket. At 4,346s, completed phase sums account for about 99% of four-worker
capacity; this snapshot does not show a large scheduling/lock-wait gap.

The first segment subsequently aborted at 4,523.17s with SQLITE_FULL and a
coverage worker internal error: 5,594 cases ended, four were unfinished and
7,644 never started. New Ready/P1 #919 owns temporary-storage lifetime and the
two TV call failures caused by disk exhaustion. The workspace was automatically
removed; original reports and corrupt worker data remain retained. SQLite
recovery into separate files preserves all setup/body line counts in the prior
5,301-ended-test snapshot; the sole orphaned context is an unfinished case.
The continuation skips 159 complete files and repeats the unfinished files
(38 already ended cases), retaining normal global-suite locking and four workers.
It uses pytest's passing-case cleanup policy and keeps reports on the workspace
filesystem at `build/test-reports/534-full-analysis-continuation/`, independently
of disposable /tmp fixtures. Planned selection was 7,686; actual collection is
7,727 with 41 concurrent additions and no missing planned original cases. The
original 13,242-case completeness audit remains required, with additions tracked
separately. #901 now has all nine original browser failure diagnostics from JUnit.
The continuation independently reproduced a missing On air artwork asset in the
desktop Setup chooser. Existing #539 was reopened to Ready/P2 with the exact
case, focused diagnostics and the original pictorial-chooser requirement. Both
TV calls affected by SQLITE_FULL passed in the healthy-storage continuation;
#919 retains the durable storage-lifetime work and now records those renewals.
New Ready/P2 #920 covers repeated genuine seed construction in three passing
Home refill consumers (114.54s call sum). It preserves all retry/signal/Fresh
assertions and explicitly distinguishes their eight-track input from the
existing four-track template. Existing #784 was reopened to Ready/P2 after its
SQL-head/admission case independently failed in 9.57s. Its new handoff requires
renewing atomic reader lifetime while preserving SQL-only serving and #688's
current Mix evidence boundaries, rather than restoring unrelated inventory waits.
Ready/P1 #921 covers Setup browser private schema reuse and Store closure:
21 desktop cases completed with 251.55s setup; the missing-artwork defect stays
on reopened #539. All Runtime setup and browser interaction requirements remain.
Ready/P2 #922 covers the repeated genuine empty v93 prefix in 20 passing Radio
cutover cases (80.94s setup); all actual v94/v100 cutovers remain. Ready/P1 #923
covers 160.10s of fresh current-schema construction inside the 12 recording
read-guard variants; genuine historical upgrades remain separate. Ready/P1 #924
covers a genuine published work-state prefix repeated by 14 invalid SQL-write
variants: 160.43s call and identical 3,606-line body sets. All invalid-write,
reader, interrupt and historical-upgrade controls remain required.
Ready/P2 #925 renews the choice-capture late-response race: its obsolete
`ledger.records` hook never admits the intended response under the current
inventory implementation. The focused case fails in 10.90s; acceptance requires
proving admission at the actual boundary and retaining the deletion guard.
Existing #521 was reopened to Ready/P2 for both independently reproduced
directory-sync retry variants (10.37s and 10.81s). Their old exact sync counts
must be renewed without weakening failure/retry ordering or FAO durability.
Ready/P1 #926–#929 cover repeated schema setup in choice capture (284.72s),
deletion recovery (280.71s), evidence responsiveness (309.38s) and playlist
browser checks (235.19s). All behavior cases, actual recovery/reopens and private
resources remain; the browser fixture also needs complete Store/server teardown.
Two Road Trip popup-closure failures independently reproduce in 54.85s and now
have detailed ownership under #901. The success case already waits five seconds
for closure, so its failure cannot be dismissed as an immediate state assertion.
Ready/P2 #930 owns two different Road Trip loader failures: page-wide alert
selection matches local and global recovery feedback (2 failed in 18.82s).
Ready/P1 #931/#932 cover ordinary theme/recap consumers repeating schema startup
inside their calls (18 passed each, 192.19s and 286.86s call respectively).
Ready/P2 #933 renews a model rollback injector that never reaches current
file-backed embedding publication (1 failed in 27.72s). Ready/P2 #934 owns the
independently reproduced evidence ledger/inventory/checkpoint import cycle
(1 failed in 6.34s), retaining the static check and exact persisted hashes.
Ready/P2 #935 renews the idle-shutdown Fresh fixture's deferred-versus-success
result (1 failed in 1.41s), preserving real owned-work draining. Ready/P1 #936
targets 57 repeated full installed-version scans in one regular migration call:
67,201 SQL executes and about 92% of its profiled cost. The focused diagnostic
passed in 48.86s including profiling overhead; the same inventory call was
9.447s. Its temporary plugin was removed. All invalid-input/DDL independence
cases remain required; this is distinct from #704's DDL generation work.
Ready #937–#940 cover genuine private inputs in read-recovery browser checks
(17 passed, 261.53s setup), bundle isolation (17 passed, 247.66s setup), Vibe
readiness (11 repeated preparation consumers, 255.28s call including checks)
and mode-refresh workers (nine library consumers, 96.09s setup). Each preserves
its actual dataset, every behavior case and genuine external artifact ownership.
Existing #749 was reopened to Ready/P2 for the exact slow Home verification
failure in the four-worker inventory (11.6578s call). Independent serial checks
passed without coverage (14.92s total) and with coverage (21.16s); the final
inventory traceback remains outstanding. Neither a deterministic lock defect
nor permission to relax its concurrency deadlines is inferred from that result.
Ready/P2 #941 owns two rebuild-admission variants that now bypass the expected
guard; transaction and explicit-maintenance controls pass. Its handoff separates
routine pending cleanup from real live-owner safety and requires current fences.
Existing #727 was reopened to Ready/P2: tools navigation launches TV correctly
but lacks cluster vectors after seeding moved into a module-local Store fixture.
The combined independent run reproduced all three failures, with two controls
passing, in 32.28s. Original navigation/player continuity assertions remain.
The current coordinator comment is
https://github.com/reubenfirmin/TuneConsole/issues/534#issuecomment-6612719218.

Implementation progress and disposition of the recommendations in issue #534. The source
review described an older snapshot; this ledger reconciles it with the current
shared checkout. This records focused implementation evidence, not independent
acceptance or a claim that the current test suite passes.

Coverage-first continuation, 2026-11-03 (reports 07 through 44):

Reports 30–44 continue the same campaign; no complete full-suite coverage or
15-minute runtime is established. All covered commands use `uv run pytest`,
`--cov --cov-context=test --cov-report=`, per-report coverage configuration,
live JSONL progress, JUnit and disposable `--basetemp`. Focused checks also make
resource/unraisable warnings errors. Broad selections keep four whole-file
workers, the machine lock, `--test-lock-wait=60`, scaled 900-second target and
90-second lag. Reports live outside disposable fixtures in
`/tmp/tuneconsole-534-timing-{30..38}/`. Concurrent shared-checkout work and
overlapping focused checks prevent treating these as controlled speed ratios.

- Report 29 finished: all three prepared-Home comparison consumers passed in
  80.99s; 2,369/2,630 body lines overlap, with no identical-line groups.
- Report 27 stopped with 123 passed at 103.74s including finalization; its pace
  receipt was 99.00s with 90.59s lag. Body overlap is 12,013/14,274 (18 groups).
- Report 30 passed 39 cases and failed one in 91.61s. The rebuild's legitimate
  destination-code fingerprint detected concurrent production-source edits;
  this was not suppressed. Report 33 reran the exact failed case and the two
  unrun tails: all three passed in 40.13s. All 42 selected metadata-delivery,
  released-layout and destination checks have therefore passed after renewal.
  Metadata delivery consumers clone a genuine v69 prefix but still execute and
  validate both actual migrations. Released-v64 consumers clone one genuinely
  built populated release database. Development-v93 DDL runs in one transaction
  instead of one disk commit per statement; its SQL and populated data remain.
- Report 31 passed both favicon/player checks in 19.08s. The polling helper now
  jumps unasserted intermediate frames and waits for the actual bridge response.
  Every player, favicon and radio-tab state assertion remains.
- Report 32 stopped with 224 passed at 112.55s including shutdown; pace was
  106.00s with 90.69s lag, 224/10,321 cases. Body overlap is 10,195/16,455
  (19 groups). Its configured target was 705.204s, 0.479s below the exact scaled
  705.683s; this small arithmetic discrepancy did not drive the result.
- Report 34 passed all 33 album-return cases plus three audio/GitHub restart
  checks in 135.61s. Body overlap is 3,775/9,881 (four groups). All 32 album
  combinations remain; eight genuine save/alias-order baselines are privately
  cloned across the four listening-day distributions. Actual listening events,
  reconciliation, both model spaces and immutable raw evidence remain per case.
  Restart cases reuse initial private current files or a genuine v55 prefix;
  actual close/reopen/admission and migration assertions remain.
- Report 36 passed all 31 playlist browser cases in 136.72s. Body overlap is
  5,200/5,650 (five groups). Each app now uses its private empty-schema clone,
  retaining its real catalog seed, clients, live server and browser assertions.
- Report 35 selected 10,031 cases in 973 files with 68 already-started files
  excluded for exploration only. It stopped with **one failed, 55 passed** at
  100.46s including shutdown; pace was 94.00s, 90.17s lag, target 685.854s.
  Body overlap is 6,372/8,097 (seven groups). Its song-menu failure set the
  routine evidence-cleanup flag and expected a global write pause, contrary to
  the current #866 requirement. The test now checks cleanup leaves writes
  enabled, then uses actual storage maintenance for its retained dynamic
  disable/re-enable and keyboard behavior. No product gating was weakened.
- The stopped report-35 segment also exposed a 42.31s expiry call and a 17.32s
  pinned-handle call. Expiry now publishes 129 real files (crossing its 128-entry
  traversal boundary) through the real bounded batch writer, rather than 180
  serial publications. Its partial-progress/completion/callback checks remain,
  with an explicit 128 bound. Pinned-handle testing retains real conflicting
  file locks and the actual 15-second timeout loop, advancing only its private
  clock and asserting the 25ms retry schedule. Radio browser consumers privately
  clone one genuinely published mix before starting their own app and routes.
  Report 37 passed all five core cases in 38.27s: expiry call 4.12s and
  pinned-handle call 0.79s. Report 38 passed all 31 Radio cases plus the menu
  recovery case in 212.75s. Their body overlaps are 2,301/2,838 (zero groups)
  and 2,673/3,417 (seven groups), respectively. Radio setup still includes
  actual server/page startup, routes and per-case remote state.
- Profiling eight private consumer initializations attributed 4.92/5.15s to
  rebuilding the identical revision-trigger definitions. `PristineSchema` now
  retains the SQL and connection-local metadata generated by the real fresh
  bootstrap, then installs actual private TEMP triggers/counters in each clone.
  It shares no live connection or mutable counter state. The independent
  fresh-bootstrap parity cases additionally compare all TEMP schema objects,
  revision fields and private state. Eight alternating uninstrumented pairs
  averaged 0.16648s for original guard construction versus 0.09233s for copying
  its genuine SQL; every pair checked exact TEMP SQL, foreign keys, empty
  transactions and counters advancing after a real private write. This measures
  only guard construction, not a whole-suite ratio.
- Report 39 passed all 84 parity/snapshot/revision cases in 271.51s. The serial
  revision file exposed 11–14s fresh bootstraps per case. This focused run
  mistakenly omitted its in-process stop monitor, and sandbox process isolation
  prevented stopping it without the rejected outside-terminal input path.
  It finished; future diagnostics carry explicit monitoring. The serial file
  now uses private current-schema memory/WAL consumers for its revision tests.
  Real external writers, schema retirement/rebind, rollback/savepoint counters,
  memo invalidation and stale-reader publication checks remain. Report 40
  passed all 19 in 33.53s; warm setup is about 0.11s memory / 0.2–0.4s WAL.
  Reports 39/40 also exposed two test-owned raw SQLite connections left open;
  both are now explicitly closed, and the external-write case avoids a final
  redundant bootstrap. Both final revised cases passed in 13.53s as report 41,
  with strict resource/unraisable warnings and no remaining leak warning.

Report 42 is the unexcluded ordinary-runtime check: `uv run pytest -m '' -n 4
--test-lock-wait=60 --test-runtime-target=900 --test-runtime-slack=90
--test-progress=/tmp/tuneconsole-534-timing-42/progress.jsonl
--junitxml=/tmp/tuneconsole-534-timing-42/junit.xml --no-cov --durations=30
--maxfail=1 --tb=short -o faulthandler_timeout=30
--basetemp=/tmp/tuneconsole-534-timing-42-workspace`. It collected 13,175 cases
in 1,042 files. It stopped with **1,499 passed, no failures** at 196.99s including
shutdown. Its 193-second pace receipt was 90.6s behind the target line.
The stopped segment includes genuine historical replay and preparation recovery;
the latter still constructed an unused restored target in its nested-plan
snapshot branch. That redundant restore is now removed, retaining the actual
snapshot recovery, independent file namespace, completion and custody checks.
This timing run does not replace the required full per-test
coverage analysis; instrumented overlap and ordinary runtime are kept distinct.

Report 43 checks all historical predecessor suffixes plus the populated regular
admission and both ranked nested-plan recovery paths under strict pytest-cov.
The matrix retains every starting version and SQL/data/idempotence assertion,
with real disk transactions at versions 1, 24 and 72 and private memory Stores
for the other SQL replay cases. Its first attempt stopped with six passed and
one resource-warning failure after 54.67s: v018's cached reference-schema builder
used a connection transaction context without closing the SQLite connection.
That actual leak is now fixed with an explicit closing context; renewal remains.

Report 44 continues coverage exploration through 9,901 cases in 969 files,
excluding the 73 previously started/independently covered files for exploration
only. Its target is exactly 900 * 9,901 / 13,175 = 676.349146s, with 90s allowed
lag, four workers and the normal machine lock. Final integration must include
all exclusions and interrupted tails. Reports are under their numbered /tmp
directories, separate from disposable fixtures.

Line overlap identifies candidates for inspection; it does not establish
assertion equivalence. Browser JavaScript behavior is outside Python coverage.
Interrupted file tails remain deferred, and final whole-suite verification must
include every file. Temporary run workspaces are removed at pytest teardown;
machine/worker lock files remain.

Report 24 stopped dynamically at 102.00s with 172/10,767 complete and 90.24s
lag; shutdown finished at 112.39s, exit 2. Its body contexts share
10,112/11,921 observed lines (23 identical-line groups), and setup shares
2,949/3,276 (five groups). Three files drained; four more were interrupted.
The disposable workspace no longer exists. Its stopped segment led to:

- Generated-playlist GC recovery now starts from a private file-backed current
  schema, retaining real `Store.init_schema()` on every explicit reopen. Warm
  setup is 0.4–0.8s versus 14–16s in the preceding covered cohort. Source custody,
  backups, unknown remote states, durable deadlines and no-replay assertions
  remain. Fixture teardown also closes a Store replaced by a restart case.
- The shared radio fixture's 32 real catalog subjects are built once per worker
  and privately restored before downstream fixtures mutate the Store. Each
  consumer gets its own mutable pool, controller, bridge and remote state.
  The larger intensity fixture still replaces its baseline and adds the radio
  pool through real writes, preserving its intentional genre seed. Warm ordinary
  radio setup is around 0.3s versus 2.8–3.8s in the stopped cohort.
- Two comparison consumers share one genuine 28-day, three-format evidence
  history, with independent SQL, files and report payloads. Three read-only
  album-readiness cases share genuine journey predecessors in private WAL files.
  Three paired-menu/rebuilt-opportunity cases reuse the genuinely prepared Home
  baseline; their actual serving, replacement and evidence capture remain.
  Single-format trials keep two episodes per day: both trial arms need twenty
  observed outcomes, so reducing that repetition would weaken the required input.

Report 26 passed all 116 GC recovery, radio handoff/session and two real-candidate
live-retune cases in 302.49s with pytest-cov and resource/unraisable warnings as
errors. Only Starlette deprecation remained. Body contexts share 7,170/8,224 lines
with eleven identical-line groups. The live-retune checks retain distinct theme
behavior and take 41.35s cold setup plus 39.62/34.08s calls; no duplicate-behavior
claim is inferred from their substantial line overlap.

Report 28 passed all five changed comparison-history/read-only readiness cases
in 73.10s under the same strict covered invocation. Its genuine shared history
took 63.40s cold setup; the second consumer took 0.47s setup and 1.14s call.
The separately changed three prepared-Home consumers passed as report 29 above.
These focused cohorts are not controlled whole-suite timing comparisons.

Report 27 selected 10,520 cases in 988 files, excluding the 53 files
already started or completely checked. Three additional selected cases appeared
since collection 24. Its scaled target is `900 * 10520 / 13163 = 719.289`, with
90s allowed lag and the normal four-worker machine/lifetime locks. Interrupted
tails remain deferred; final whole-suite verification must include all files.
No completed full-suite coverage result or whole-suite runtime is established.

Report 21 stopped at 117.00s with 393/11,250 cases complete and 90.09s
lag. Finalization finished at 120.45s, exit 2, 393 passed, 60 framework warnings.
Body contexts share 12,469/14,049 observed lines with 51 identical-line groups;
setup shares 5,231/5,258 with seven groups. The workspace was removed. Eight
more files drained completely and four were interrupted. Its stopped segment
led to these changes:

- The three format-picker cases repeated a full journey, including unrelated
  later TV/Vibe/story preparation. Their independent Stores now clone one real
  early journey with four published mixes and an eligible radio offer. Each
  still begins with an empty-account HTTP 409, then checks its actual preference,
  redirect, setup status and Home panel. Real later-destination journeys remain.
  The final shared builder takes 2.95s; calls take 2.08/2.23/2.29s. The earlier
  stopped calls were 28.35/30.71s, with the third interrupted. These are different
  cohorts, not a controlled whole-suite ratio.
- All 39 cluster browser cases clone a genuinely seeded 36-song/vector baseline
  before their live app starts. Each owns private SQL, bytes, context and page;
  seed edits, failures, retry, persisted canvas state, stale responses and actual
  browser interactions/assertions remain. Warm curated setup is typically
  1.6–2.0s versus around 5.5s in the stopped cohort.
- Deletion-safety cases retain the real Undo validation/retry loop with an
  explicit recording sleeper. Each uncertain-destination case now also checks
  the actual 0.5/1.0 delay schedule (or no capture retry for duplicate deletion).
  This removes repeated wall-clock waiting while preserving source custody,
  partial writes, backups, uncertain responses and no-replay assertions.
- Prepared-clone isolation reuses the shared genuinely prepared Home baseline,
  then still closes its independent source and checks both consumers after that
  source's files are removed. The call is 2.93s; cold shared preparation remains
  a real 45.75s setup in this cohort. Six raw test-owned audio snapshot connections
  now close explicitly; read-only byte-equality assertions remain.

Report 22 caught a clone-helper assumption that every seed publishes recommendation
bundles. The helper now accepts a plain catalog/vector seed with no bundles.
Report 23 passed all 86 cluster, format-picker, audio-model and real-file isolation
cases in 212.15s under pytest-cov with resource/unraisable warnings as errors.
Body contexts share 7,331/11,096 lines with eight identical-line groups. JavaScript
behavior is outside Python coverage; browser cases with no Python body lines
still retain distinct client-side contracts. The final narrower format baseline
and recording sleeper then passed all 51 deletion-safety, Undo-capture and picker
cases in 59.40s (report 25, same strict warnings, only Starlette deprecation).

Report 24 selected the next 10,767 cases in 995 files. It excludes the 43 files
already started in the preceding broad cohorts and three complete focused files
(Store parity, prepared-clone isolation and Undo capture). Full collection 22 was
13,163 cases in 1,041 files; continuing shared-checkout edits added three further
selected cases by collection 24. The scaled target uses the 13,163-case reference:
`900 * 10767 / 13163 = 736.177`, with 90s lag and normal four-worker machine locks.
`inventory.json` records broad-cohort completion separately from focused passes;
incomplete files remain deferred. Its stop and resulting changes are above. Final verification must
include all files, and full-suite coverage/timing remain unestablished.

Report 19 stopped at 118.00s with 408/11,768 cases complete and 90.06s
lag; finalization finished at 122.00s, exit 2, 408 passed. Body contexts share
10,873/14,194 observed lines, with 53 identical-line groups. Eight further files
were fully drained; four more files remain deferred. The 15 warnings include
unclosed test-owned SQLite snapshot connections in `test_audio_vibe_model.py`.
The disposable workspace was removed. This was another incomplete cohort.

That stopped segment led to three further removals of repeated setup:

- Ordinary memory, file and browser consumer fixtures now copy the immutable
  genuinely admitted current schema and bind their own repositories, repair,
  revision guards and WAL. They avoid repeating schema admission against the
  identical SQL baseline. Actual bootstrap, admission, upgrade and reopen tests
  still execute production admission. Eight alternating, uninstrumented private
  clone pairs averaged 0.30158s with admission and 0.17693s with consumer binding
  (41.3% less for this initialization operation). Every pair checked foreign
  keys and transaction state. This is not a whole-suite speed ratio.
- Policy-file migration's 41 cases share one genuine v69 prefix. Each private
  copy retains its own original rows and executes its real v70 conversion,
  corruption, rollback, retention, foreign-key, locking and resource assertions.
- Six TV journey cases share genuinely prepared early-journey inputs. Each
  publishes its own mixes/radio and exercises actual readiness and HTTP access,
  including separately missing fourth mix, twelfth video and predecessor.
  Tests specifically exercising preparation continue to build the journey.

All 112 changed consumer-parity, policy migration, TV and snapshot-revision cases
passed in 126.39s with per-test pytest-cov and resource/unraisable warnings as
errors (report 20). Only Starlette deprecation remained. Body contexts share
7,594/8,236 observed lines with 15 identical-line groups. The parity cases check
all schema objects, logical rows and startup flags against an independent genuine
fresh bootstrap; mutation cases exercise real stale/fresh publication guards.
Their duplicated independent fresh reference is now built once per module and
made read-only. A subsequent 10-case check passed in 67.99s, including every
Store field's presence and real prepared memory/WAL clone isolation after the
source's files are removed. The clone-isolation call took 46.91s; it remains a
candidate for reuse of the shared genuinely prepared baseline.

Report 21 continues coverage through 11,250 cases in 1,009 files, excluding the
31 already-started files in reports 17 and 19. Eight incomplete files remain
explicitly deferred. It uses the same command form below with report directory
21 and target `900 * 11250 / 13142 = 770.431`, 90s lag and normal machine/worker
locks. Its collection is complete; execution results are pending. These
exploration exclusions must not appear in final full-selection verification.

Latest extension, reports 17–19: full selection 17 collected 13,142 cases in
1,037 files. It stopped at 173.00s with 1,207 complete and 90.34s lag; finalization
finished at 178.06s (console: 178.03s), exit 2, 1,207 passed, five warnings.
The whole domain module passed all 86 final cases, including its SQL-only
coordinate guard and ordinary constrained-fixture clone. All nine extended
historical foreign-key cases passed (0.078–0.820s calls after one 6.496s template
setup), and genuine populated predecessors through v29 passed. Body coverage
shared 16,258/19,621 observed lines, with 122 identical-line groups. The remaining
most costly identical-line groups were 4.32s across 35 different specialist-error
inputs, 3.56s across three actual DJ resource sizes, and 3.11s across the five
distinct invalid domain owners. Their differing behavior/resource inputs remain.
An unclosed connection from the genuine fresh/current no-historical-callback
control was fixed with `closing`; its strict-warning renewal passed below.
The full-selection workspace was removed and lock files retained.

Its stopped segment led to two additional consolidations. Four Home dependency,
failure and retry cases now advance only Home's menu-slice clock after a real
menu, avoiding twelve alternative enumerations where the requirement is useful
output after each attempt. Actual setup, selection, publication and work-budget
clocks remain real. The separate cross-menu summary-sharing case still runs its
multiple-menu path, and the twelve-attempt accumulation case remains unchanged.
The first mix and both continuing source updates still each execute preparation.
Snapshot recovery previously constructed an unused fully restored database before
restoring the actual snapshot-path target. `snapshot_library(..., restore=False)`
now avoids that unused second target in those cases; default restored-path cases
still run the complete independent restore. All ten selected-owner cases, both
native-operation cases, four changed Home cases and the fresh/current control
passed with per-test coverage and resource/unraisable warnings as errors: 17
cases in 180.52s, exit 0, only Starlette deprecation (report 18). Home calls were
4.666/3.858/4.458s and 10.220s for the three-attempt continuing-update case.
Cold shared setup varied to 64.461s; no controlled whole-suite ratio is inferred.

Coverage exploration now continues through unmeasured files instead of repeatedly
running the same passed prefix. Report 17 completely drained 15 files (1,094
cases); four other files were interrupted. Report 19's explicit
`selection.txt` excludes those 19 files: the four incomplete files remain deferred,
not accepted. Collection selected 11,768 cases in 1,018 files. This remains one
coordinator with four local workers under the normal machine/worker locks.
The dynamic target is scaled to `900 * 11768 / 13142 = 805.905` seconds with the
same 90s lag, preserving the original per-case target. Command: `uv run pytest
-m '' @/tmp/tuneconsole-534-timing-19/selection.txt -n 4 --test-lock-wait=60
--test-runtime-target=805.905 --test-runtime-slack=90 --test-progress=.../progress.jsonl
--junitxml=.../junit.xml --cov --cov-context=test --cov-config=.../coverage.ini
--cov-report= --durations=30 --maxfail=1 -o faulthandler_timeout=30
--basetemp=/tmp/tuneconsole-534-timing-19-workspace` (all ellipses denote that
report directory). This is coverage exploration across cohorts; final complete
runtime verification must use the entire selection without these exclusions.

The user clarified that redundancy analysis must precede repeated runtime runs,
and every early stop must lead to optimization in its stopped segment. There is
still no completed full-suite coverage result. Full-selection report 07 selected
13,118 cases in 1,035 files; it stopped dynamically at 157.00s with 967 completed
and 90.66s lag against the 900-second target. Finalization finished at 161.45s,
exit 2, 967 passed, eight warnings, no failures observed. Its disposable workspace
was removed. Per-test pytest-cov contexts retained 16,648 executed body lines,
13,323 shared, and 100 identical-line groups. Equal lines are candidate evidence;
distinct inputs and assertions were reviewed before changing execution.

- Five invalid-domain predecessor cases shared the same body coverage and spent
  74.46s collectively replaying regular admission before their distinct failed
  numbered conversions. An immutable, genuinely admitted v103 SQL baseline now
  supplies private files. Every conversion/error/full retained-dump assertion
  remains. A separate invalid-before-first-admission control preserves that
  sequencing boundary. All 86 domain checks passed with per-test coverage in
  98.66s; the five calls were 0.668–0.692s after one 13.90s setup, and the separate
  initial-admission control took 14.38s. Subsequent changes to clone the ordinary
  constrained fixture and assert the SQL-only template has no file coordinates
  await renewed verification in report 17.
- Two external-writer snapshot cases no longer repeat fresh bootstrap. Each
  clones a private file-backed Store, opens a genuinely separate SQLite writer,
  commits real row/schema changes, and preserves conservative publication guards.
  Both connections now close explicitly. Their final covered checks passed in
  report 16: 0.753s and 0.870s including setup/call/teardown, with resource and
  unraisable warnings treated as errors. Earlier full-selection totals were
  14.690s and 15.346s; these are different cohorts, not a controlled suite ratio.
- All 45 populated historical predecessor replays remain real. Their repeated
  regular DDL admission is now checked once by a separate populated v1 control,
  with an independent all-row/program preservation oracle and real restart.
  Every matrix member still executes its numbered upgrades, validates the full
  numbered schema contract, checks retained catalog/listening/settings rows,
  repeats the numbered runner for idempotence, and checks integrity. Report 14
  passed 40 covered checks in 72.47s, including v1/v24/v72 and the new populated
  control. Remaining matrix versions await renewed verification. Nine historical
  foreign-key validation cases now use private copies of the same genuine
  predecessor chain instead of replaying nine overlapping prefixes; each still
  injects and rejects its own orphan, and traces exactly one foreign-key check.
  The extended chain rejects any SQL-only snapshot with retained file coordinates;
  that final change awaits report 17.
- Home catalog and prepared SQL/file templates now have session lifetime, with
  independent SQL backups, byte copies, relocated coordinates and deep-copied
  payloads per consumer. A neutral seed helper avoids cross-module fixture import
  coupling. Shared inputs use 96 rows instead of 192 while retaining six modes
  and four lanes. Explicit larger-input tests retain their own sizes. Imported
  prepared fixtures restore their actual dependencies. The PPR HTTP wiring case
  creates the real app directly, avoiding a second Home preparation hidden in a
  general client helper; its rendering/assignment/exposure assertions remain.
- The full twelve-menu completeness test and the incremental-batch test shared
  3,248 executed production lines (3,258 and 3,275 total). All completeness,
  six-mode, recipe experiment-ID and zero-exposure assertions moved into the
  incremental test before removing the separate full preparation. The merged
  case passed with coverage in report 16 (38.14s call). It still makes twelve
  separately budgeted preparation attempts and checks accumulated size each time.
- The stopped Home partial-refresh case republished all Vibes preset pools during
  its ordinary complete recovery. That publication now needs only the genuine
  six-mode/four-lane inputs; separate Vibes integration checks remain. All four
  retention cases passed, including feedback/unavailable invalidation and initial
  partial publication. The positive recovery call took 19.13s after the preceding
  WAL experiment exceeded 70.60s. WAL consumer/source experiments did not improve
  this fixture and were reverted. Report 16 passed all seven selected checks in
  149.68s with coverage, including cold shared preparation (55.48s), the merged
  twelve-menu case, and both external-writer cases. Only the existing Starlette
  deprecation warning remained; resource/unraisable warnings were errors.

Diagnostics now finalize pytest-cov after `--maxfail=1` as well as pace stops.
A pace interruption inside a native SQLite callback is restored to exit 2 even
when SQLite converts KeyboardInterrupt to OperationalError. Eighteen covered
diagnostic subprocess checks passed, plus the new first-failure coverage check
passed separately (19 changed checks total). They exercise actual serial and
two-worker processes, cleanup, retained contexts, and SQLite callback interruption.
Report 15 demonstrated the corrected exit 2 with no product failure recorded.

Coverage commands use `uv run pytest ... --cov --cov-context=test
--cov-config=/tmp/tuneconsole-534-timing-NN/coverage.ini --cov-report=`. Overlap
commands use `.venv/bin/python scripts/test-coverage-overlap.py COVERAGE
--progress PROGRESS --phase run --output REPORT` (and `--phase setup` separately).
All full selections use `-m '' -n 4 --test-lock-wait=60
--test-runtime-target=900 --maxfail=1 -o faulthandler_timeout=30`, explicit report
paths, and a disposable `--basetemp` outside the report directory. Focused report
16 used the four retention cases, the merged incremental case and the two
external-writer cases, `--test-runtime-target=240 --test-runtime-slack=90
-W error::ResourceWarning -W error::pytest.PytestUnraisableExceptionWarning`.
Report 17 is the next full-selection coverage run after these changes. No
completed full-suite timing, full-suite overlap conclusion, hosted 15-minute
acceptance, commit or push is claimed.

Scope correction, 2026-11-01: the user explicitly excluded mobile support and
testing. Phone viewports and mobile-device emulation have been removed. Unique
functional checks formerly coupled to phone widths now use desktop windows;
desktop, short desktop windows and TV coverage remain. Older results below are
historical evidence, and their phone variants no longer count toward acceptance.

Independent runtime investigation, 2026-11-01: fresh `Store.init_schema()` took
9.386–12.843 seconds across two memory and two file-backed initializations. Each
warm-reference initialization performed 53 complete schema validations, 18,709
table signatures and 117,567 SQL tokenizations; the actual 32-track Radio fixture
seeding took 0.543–0.727 seconds. Pure SQL tokenization now has a bounded
4,096-entry cache of immutable tuples. Database introspection, admission checks,
each regular migration transaction and applied history remain unchanged.

The same low-overhead diagnostic with the final cache measured memory setup at
6.351/5.801 seconds and file setup at 6.860/6.880 seconds; repeated admission took
0.271–0.322 seconds. Both diagnostic cases passed in 31.402 seconds. Seventeen
malformed-schema, constraint, foreign-key ownership/order and concurrent-startup
checks also passed against this final cache. A separate capacity experiment found
1,522 distinct SQL definitions: a 1,024-entry cache churned, while 4,096 entries
retained the working set. This is an entry bound, not a general byte-size bound.
Reports are `/tmp/tuneconsole-review-token-cache-timing-final-534-20261101/`,
`/tmp/tuneconsole-review-token-cache-final-534-20261101/` and
`/tmp/tuneconsole-review-token-working-set-534-20261101/`. The first final-cache
attempt's two timing failures were an evidence-plugin import assumption; the
seventeen product checks passed, and the corrected timing-only run above passed.

Ordinary `store` consumers now clone a session-owned, read-only in-memory SQL
template built by genuine `Store.init_schema()`. Each consumer creates its own
Store, connection and external-file namespace, then runs normal admission on its
private clone. No mutable Store or prepared user data is shared. Fresh-bootstrap
and historical-upgrade tests continue to construct their own real databases.
The template is closed at session teardown; every consumer retains its existing
connection/file cleanup. This changes migration receipt timestamps to template
creation time for ordinary consumers only.

An independent experiment measured one genuine template bootstrap at 5.851
seconds and eight private clone/admission operations at 0.357–0.364 seconds.
All 403 tables, their SQL definitions and logical rows matched a separately
bootstrapped Store, allowing only the two migration histories' `applied_at`
fields to differ. Template writes were rejected and consumer mutations stayed
private. That experiment passed nine checks in 20.649 seconds. After adopting
the fixture, 55 storage, preference, Home and progression checks passed in
86.328 seconds, including new full-state/isolation tests and genuine fresh,
rollback and concurrent-bootstrap controls. The desktop TV playback/shared
controls journey also passed. Three desktop Vibe cases initially stopped at a
post-unlock fixture omission owned by #583. With explicit earned listener setup,
an in-memory renewal hung during the first pointer case and reached the owner's
1,200-second timeout; zero browser cases completed. Later file-backed WAL checks
passed both original pointer and keyboard journeys (separate runs: 1 passed in
135.586 seconds, then 1 passed / 1 failed in 199.860 seconds). Their screenshots
were inspected; artwork requests were deliberately blocked, so these establish
control behavior rather than artwork quality or native playback.

The recovery failure in that second run was a contradictory Playwright `check()`
postcondition: the injected HTTP 500 correctly restores the previously selected
Relax radio. The action now uses `click()` and checks the actual Energize request
and 500 response, retaining all recovery/reset assertions. Renewal reached a
separate incomplete baseline: three ready mixes and a Fresh placeholder cannot
justify the expected four-mix ready message with background preparation disabled.
Fresh fixture publication is being reconciled; recovery/reset is not accepted.
An independent connection-ownership check also confirms that the real Mix
availability query runs before its Store lock (1 failure in 7.241 seconds).
This does not prove the cause of the earlier concurrent in-memory hang.

Vibe reports are `/tmp/tuneconsole-review-vibe-earned-access-583-20261101/`,
`/tmp/tuneconsole-review-vibe-file-backed-583-20261102/`,
`/tmp/tuneconsole-review-vibe-file-renewal-583-20261102/`,
`/tmp/tuneconsole-review-vibe-recovery-583-20261102/` and
`/tmp/tuneconsole-review-mix-serving-guard-583-20261102/`. Other reports are
`/tmp/tuneconsole-review-pristine-store-534-20261101/`,
`/tmp/tuneconsole-review-pristine-store-validation-534-20261101/` and
`/tmp/tuneconsole-review-pristine-store-browser-534-20261101/`.

The complete current-schema and independent-change modules passed 121 checks
in 996.653 seconds against the earlier 1,024-entry token cache and unchanged
frozen source. The final 4,096-entry cache has the seventeen negative/ownership
checks above and three genuine bootstrap controls within the 55-check run;
the full 121-check matrix is not claimed for that later capacity. Its report is
`/tmp/tuneconsole-review-schema-token-cache-534-20261101/`.

These measurements were taken during concurrent focused work, not controlled CI
benchmarks. A separate cProfile diagnostic substantially inflated wall time and
is not a normal-runtime measurement. The required complete intended desktop/TV
CI result remains 15 minutes or less. Current focused passes and mobile scope
removal do not establish that result; see the current hosted investigation below.

## Live progress and per-test coverage, 2026-11-03

#534 is In progress at the user's request. The user also authorized a full local
run with early interruption when excessive runtime became apparent.

`pytest_progress.py` supplies opt-in, flushed console and JSONL receipts for test
starts and setup/call/teardown outcomes, including worker identity. Both existing
PR CI test commands enable it and retain the receipts with their evidence.
Diagnostic coverage uses pytest-cov's test/phase contexts and an explicit C tracer.
Completed worker tests checkpoint coverage; interrupted sessions save/combine it.
Coverage without explicit `-n` remains serial because pytest-cov chooses its
controller before automatic local-worker selection. Explicit `-n 4` enables both.
`scripts/test-coverage-overlap.py` reports observed per-test times, outcomes,
shared/exclusive Python lines and identical line sets, separately for run/setup/
teardown. It reads `.coverage` directly: the sample's 4.6 MB database expanded to
a 3.6 GB context JSON export, which was removed after verifying direct-database
report parity. No regular CI coverage instrumentation was added.

Full diagnostic command:

```sh
uv run pytest -m '' -n 4 --test-lock-wait=60 \
  --test-progress=/tmp/tuneconsole-534-full/progress.jsonl \
  --cov --cov-context=test --cov-report= \
  --junitxml=/tmp/tuneconsole-534-full/junit.xml \
  --durations=30 --maxfail=5 -o faulthandler_timeout=30
```

Actual result: **1,452 passed, five setup errors, exit 2 after 793.99 seconds**
(13m13.99s). There were 1,459 observed starts, 1,457 ends and two active tests
without completion records. The stop was later than intended; this is an
instrumented partial run, not a passing suite or a CI duration qualification.
The unfinished cases were current-schema predecessor replay `[70]` and
`test_unknown_or_legacy_curation_never_creates_account_evidence[False-feedback]`;
both had successful setup but no recorded call outcome.
The checkout was dirty and changing concurrently at HEAD `bc5746b0e`; runtime
was Python 3.14.8, pytest 9.1.1, xdist 3.8.0, pytest-cov 7.1.0, coverage 7.17.0,
application `0.1.19.dev8+gbc5746b0e.d20261103`. Fixtures were disposable generated
libraries, not the user's installed library. No phone variants were selected.

The observed test bodies shared 22,514 of 25,387 covered Python lines, with 153
identical line groups. Largest aggregate observed phase costs were current-schema
replay (767.66s), Home preparation (479.02s), preparation-file recovery (411.26s)
and domain contracts (365.33s). These sum parallel work, not wall time. The
69-case direct domain-rejection group and 73-case DJ-order group illustrate why
equal covered lines do not prove equal assertions: their inputs and expected
outcomes differ. Review fixture reuse and retained behavior before consolidation.
The Home retry path still contributed five exclusive lines; slow execution alone
does not justify removing it.

All five full-run setup errors used the then-current shared Home template and
failed with `ValueError: Sample export requires a pinned database snapshot`.
That fixture was changed concurrently afterward. A focused check of its new
clone-isolation test instead failed at `PreparedTemplate.restore`'s
`assert source != destination` (1 failed in 32.54s), so the five selected Home
rechecks following it did not execute. This is separate evidence from the
earlier snapshot-export failure; the changing fixture is not accepted here.
An independent focused invocation of
`tests/rec/test_home_prepared.py::test_preparation_is_bounded_complete_and_records_no_exposure`
then passed in 56.11s (43.55s setup, 11.80s call), with report
`/tmp/tuneconsole-534-home-current.xml`. This renews that one case only; it does
not establish clone isolation or the other four earlier setup errors.

Focused diagnostic evidence: `uv run pytest tests/test_pytest_diagnostics.py
tests/test_local_test_workers.py --tb=short --maxfail=1
--junitxml=/tmp/tuneconsole-534-context-workers-check.xml` passed 29 checks in
7.00s. This covers live serial/xdist receipts, actual interrupted worker coverage,
parameter/phase identity, failed calls, unstarted tails and safe controller
selection. After adding direct-database input and rejection of context-free
exports and phase-specific timing order, `uv run pytest
tests/test_pytest_diagnostics.py --tb=short --maxfail=1
--junitxml=/tmp/tuneconsole-534-diagnostics-final.xml` passed six checks in 2.51s.
The earlier focused worker/shard checks passed 82 cases in 4.52s; unchanged passing
checks were not repeated. `git diff --check` passed.

Retained reports: `/tmp/tuneconsole-534-full/` contains `progress.jsonl`,
`junit.xml`, `coverage.sqlite3`, `overlap-run.json`, `overlap-setup.json` and
`html/index.html`. Reports describe only observed work. Required uninstrumented,
complete hosted desktop/TV CI at most 15 minutes remains outstanding; this pass
does not authorize a commit or push.

## Installed-layout reuse and admission, 2026-11-03

The continuation replaces repeated empty-history construction in the shared
`core/test_predecessor_writes.py::installed` fixture, imported by 97 modules.
`conftest.py` builds each requested historical SQL layout once per worker and
retains a read-only memory template. A new version continues real migrations
from the closest cached older layout, never from a newer one. Non-null filename
coordinates are rejected: these are empty SQL templates, not prepared libraries.
Every consumer still owns a new file database, real reopen/admission, WAL,
foreign keys, separate file namespaces, unchanged-history assertions and a
read-only final audit. Populated upgrade and fault-injection builders remain
explicit. The audit reader now closes explicitly; SQLite's transaction context
alone did not close it.

Independent template parity checks cover versions 79, 93, 102, 107 and then 97.
They compare schema objects and rows with genuine replay before and after
ordinary admission, retain applied-history values except nondeterministic
timestamps, and check private SQL/file roots, foreign keys, WAL and immutable
source state. All five passed in **45.48s** after expanding parity to include
indexes, triggers and views as well as tables. This includes requesting an older
version after a newer one and continuing upgrades from an older template.
Command: `uv run pytest tests/test_installed_fixture.py --tb=short --maxfail=1
--durations=10`; final report `installed-fixture-objects.xml`.

Same four recovery cases, unchanged bodies and assertions:

```sh
uv run pytest tests/core/test_preparation_file_recovery.py \
  -k 'selected_files and (home or documents)' --tb=short --maxfail=1 --durations=10
```

Before: **4 passed / 51 deselected, 27.16s**, aggregate setup 13.803s.
After template reuse: **4 passed / 51 deselected, 12.15s**, aggregate setup
4.805s. Repeated setups fell from roughly 3.0–3.4s to 0.2–0.3s. Call time also
varied, so the whole difference is not attributed solely to setup. Concurrent
machine work and changing source prevent extrapolating this to CI speedup.
The broader installed-write/listening/discovery/trends/recovery selection then
passed **89 cases / 4 deselected in 234.43s**. It used the initial per-version
template implementation; the subsequent parity run renews the older-template
continuation, and the instrumented cohort below exercises the final factory.

Profiling five warm v93 admissions identified repeated expected-contract
construction. `migrations/runner.py` now keeps a bounded cache of shipped DDL
fingerprints for installed layouts. Each database's actual structure and data
are still inspected on every validation. Three alternating paired measurements
of five warm admissions on the same disposable v93 database took
1.2355/1.2384/1.2388s without reference caching and 0.5542/0.5531/0.5491s with it.
The full SQL dump remained unchanged. This measures warm local admission, not
suite or CI time; the original cProfile times include substantial profiler cost.

```sh
uv run pytest tests/core/test_installed_schema_validation.py \
  tests/core/test_current_schema.py::test_predecessor_validation_checks_all_foreign_keys_once \
  tests/core/test_deferred_schema_validation.py --tb=short --maxfail=2 --durations=15
```

**36 passed in 133.40s**. New checks damage both a previously validated database
and an independent database at versions 85/93/103/106. Full and deferred
validation reject missing indexes; full validation rejects wrong index
definitions and dangling foreign keys after a successful validation. Expanded
foreground checks retain real writer contention, avoid full-data scans and
leave migration history unchanged. Existing private-audit, interrupted-audit,
saved-genre and continuing-write checks passed too.

```sh
uv run pytest tests/core/test_predecessor_writes.py \
  tests/core/test_predecessor_listening_writes.py \
  tests/core/test_predecessor_discovery_writes.py tests/core/test_predecessor_trends.py \
  --cov --cov-context=test --cov-report= \
  --test-progress=/tmp/tuneconsole-534-continue/installed-contexts-final.jsonl \
  --junitxml=/tmp/tuneconsole-534-continue/installed-contexts-final.xml \
  --tb=short --maxfail=2 --durations=10 \
  -W error::ResourceWarning -W error::pytest.PytestUnraisableExceptionWarning
```

The first instrumented cohort passed 38 cases in 45.31s but reported 37 unclosed
SQLite connection warnings from the final audit reader. After closing it, the
command above passed **38 cases in 34.95s with no warnings**, treating resource
and unraisable warnings as errors. This is the final installed-consumer receipt.
Per-test coverage retains all 38 starts, ends and phase outcomes; setup cost can
be separated from the retained account, catalog, listening, discovery and trends
behaviors. The two runs are not an isolated timing comparison of reader cleanup.
Final body contexts share 3,318 of 3,480 Python lines; setup contexts share 2,459
of 14,774. The only identical body group is account reconciliation at versions
79/93/102. Those cases retain different installed layouts and remain separate.
Cold historical construction appears in the first consuming setup context,
which must not be mistaken for uniquely required behavior of that test body.

The prepared-file fixture changed in another session after the earlier failures.
The current clone-isolation check and all five previously selected Home cases
passed together: **6 passed in 119.17s**. They establish private real profile
files plus bounded preparation, frozen menu probability, missing dependencies,
new skip filtering and ordinary reads without selection/analytics. This renews
those six checks; it does not accept every prepared fixture consumer.

Reports are under `/tmp/tuneconsole-534-continue/`: `home.xml`,
`installed-fixture-objects.xml`, `recovery-before.xml`, `recovery-after.xml`,
`installed-consumers.xml`, `schema-validation.xml`, `installed-contexts-final.xml`,
their progress JSONL files, `coverage-final.sqlite3`,
`installed-overlap-{run,setup}-final.json`, `admission-profile.txt` and
`admission-comparison.json`. The same runtime versions
and disposable generated data described in the diagnostic section apply. These
are focused checks, without the global-suite lock or another full-suite run.
Complete hosted desktop/TV CI at most 15 minutes remains outstanding.

## Bounded timing continuation, 2026-11-03

The user asked for ongoing runtime reduction, early cancellation of long runs,
and no further terminal-input cancellation prompts. #534 remains **In progress**.
The previous uninstrumented four-worker full selection ended after **1074.54s
(17m54s)** with 1,779 passes and four failures. Most of its 13,027 cases were
unrun. A blocked terminal-input approval prevented the intended early stop;
this is not a complete-suite timing or a passing integration result. Reports:
`/tmp/tuneconsole-534-timing-01/{progress.jsonl,junit.xml}`. Its disposable
workspace was removed. No retained lock file was deleted.

`pytest_time_limit.py` adds opt-in `--test-time-limit=SECONDS`. Coordinator and
workers share a monotonic deadline, including collection, and interrupt their
own processes with POSIX interval timers. Pytest still tears down fixtures and
writes reports, so cleanup can extend beyond the budget. Existing timers are
not replaced. Progress now records the selected test/file counts once. Real
subprocess checks cover serial/two-worker blocked calls and blocked fixture
setup, completed passes/failures, unstarted tails, live console/JSONL, cleanup
and saved/combined coverage contexts. Interrupted setup also revealed pytest
9.1's missing `tmp_path` outcome stash; the bounded-run plugin initializes that
mapping before setup so cleanup can finish without a KeyError. The first
paid-description check hit its 60s budget during setup and exposed that bug;
it is superseded by the corrected checks below.

New fixture changes retain the actual test bodies and assertions:

- Current-restart owner rejection and malformed-current cases clone one genuine
  empty raw bootstrap. Each has its own connection; the read-only template's
  full dump must remain unchanged. Genuine fresh bootstrap, historical replay,
  bootstrap faults and transaction rollback controls still construct their own
  databases. Before: 28 passes at the 90s limit, 87.668s aggregate call work.
  After: all 36 pass in **6.54s**, including cold template setup; aggregate setup
  4.007s, call 1.979s and teardown 0.213s.
- Player ingestion uses the ordinary private Store fixture and creates its main
  account per case, instead of rebuilding the schema per event parameter. All
  **45 pass in 27.67s**, treating ResourceWarning and unraisable warnings as
  errors. Aggregate setup/call/teardown: 23.965/2.985/0.447s. The earlier full
  selection spent 265.08s aggregate phase work on these same 45 cases, under
  parallel machine contention; that is not a controlled serial speed ratio.
- Paid-description tests clone the genuine empty historical v69 template before
  executing their own policy upgrade, file publication and paid-owner migration.
  Current repository consumers retain private file-backed Stores and real
  admission. All **42 pass in 18.83s**, with aggregate setup/call/teardown
  14.793/3.476/0.186s. Earlier full-selection phase work was 183.84s, including
  168.38s setup. Interrupted migration, retained original bytes/quota evidence,
  concurrent admission, recovery and reopen checks remain in the module.

The full-selection failures were reconciled with their actual contracts. The
Last.fm outage/recovery cases pass alone (4 passes in 8.54s) but fail after the
TV launch case: the preceding app configures the process-global genre matcher.
The provider boundary now requests the existing canonical-matcher fixture.
The TV case had expected an established listener to become locked after muting
the entire eligible pool. #649 and the later #608 retained-access acceptance
require earned access to survive temporary missing output. It now expects the
existing HTTP202 preparation response and verifies that no second playlist was
created. Fresh-user locked-TV assertions remain. Running that TV case followed
by all Last.fm boundary tests passes **43 cases in 22.93s**, one existing
framework deprecation. No product behavior was changed to obtain these passes.

Focused commands, all with the plain allowlisted `uv run pytest` form:

```sh
uv run pytest tests/core/test_current_schema.py \
  -k 'current_restart or malformed_current' --test-time-limit=90 \
  --test-progress=/tmp/tuneconsole-534-timing-01/current-after.jsonl \
  --junitxml=/tmp/tuneconsole-534-timing-01/current-after.xml --tb=short --maxfail=1 --durations=5
uv run pytest tests/test_player_events_ingest.py --test-time-limit=45 \
  --test-progress=/tmp/tuneconsole-534-timing-01/player-after.jsonl \
  --junitxml=/tmp/tuneconsole-534-timing-01/player-after.xml --tb=short --maxfail=1 --durations=5 \
  -W error::ResourceWarning -W error::pytest.PytestUnraisableExceptionWarning
uv run pytest tests/core/test_album_description_owners_migration.py --test-time-limit=60 \
  --test-progress=/tmp/tuneconsole-534-timing-01/paid-final.jsonl \
  --junitxml=/tmp/tuneconsole-534-timing-01/paid-final.xml --tb=short --maxfail=1 --durations=5
uv run pytest tests/test_tv.py::test_precalculated_serving_parameters_generation_and_sidebar_launch \
  tests/providers/test_lastfm_iao.py --test-time-limit=60 --tb=short --maxfail=1 \
  --junitxml=/tmp/tuneconsole-534-timing-01/tv-lastfm-after.xml --durations=5
uv run pytest tests/test_pytest_diagnostics.py --test-time-limit=40 --tb=short --maxfail=1 \
  --junitxml=/tmp/tuneconsole-534-timing-01/deadline-setup.xml --durations=5
```

The final diagnostic command passes **10 cases in 13.86s**. Before adding the
setup-interruption regression, diagnostics plus local worker tests passed
**34 cases in 11.50s**; worker selection/ownership code did not change afterward.
Dataset: generated private fixtures; Python3.14.8, SQLite3.50.2, pytest9.1.1,
xdist3.8.0, pytest-cov7.1.0, coverage7.17.0. This is the shared changing checkout;
no complete hosted desktop/TV runtime claim follows from focused timings.

The next uninstrumented global selection used the new automatic deadline:

```sh
uv run pytest -m '' -n 4 --test-lock-wait=60 --test-time-limit=120 \
  --test-progress=/tmp/tuneconsole-534-timing-02/progress.jsonl \
  --junitxml=/tmp/tuneconsole-534-timing-02/junit.xml --durations=30 --maxfail=3 \
  -o faulthandler_timeout=30 --basetemp=/tmp/tuneconsole-534-timing-02-workspace
```

It selected 13,057 tests in 1,027 files and interrupted automatically at two
minutes. Teardown and reports finished in **127.37s (2m07s)**: **964 passed**,
four framework warnings, no failures observed, exit status 2. Four cases were
active at interruption; most selected tests never started. The disposable
workspace no longer exists. This establishes bounded cancellation and partial
progress, not a complete-suite duration or passing tail. Genuine historical
replay, preparation-file work and Home construction still dominated this sample.

### Prepared Home output reuse

The earlier seed-only pass below still executed full initial Home preparation
for every consumer. Current `prepared` consumers also clone the real initial
Home publication, including all retained file domains and a deep copy of the
returned payload. The seed and prepared-output templates remain read-only;
each consumer owns its SQL connection and byte-copied files. Tests of generation
changes, budgets, cancellation, tuning and publication still run the next real
preparation after their own mutations. Seven initial-preparation checks use
`home_inputs` to clone only catalog/profile inputs and execute their own initial
preparation. Initial import, unpublished metadata choices, changed-size seeds
and file-backed construction/restart controls keep their original builders.

```sh
uv run pytest tests/rec/test_home_prepared.py \
  -k 'bounded_complete or missing_candidate_dependencies or new_skip or ordinary_home_reads or generation_race or setup_sql_obeys' \
  --test-time-limit=120 --test-progress=/tmp/tuneconsole-534-timing-03/home-consumers.jsonl \
  --junitxml=/tmp/tuneconsole-534-timing-03/home-consumers.xml --tb=short --maxfail=1 --durations=8
uv run pytest tests/test_prepared_fixture_support.py --test-time-limit=60 \
  --test-progress=/tmp/tuneconsole-534-timing-03/clone-isolation.jsonl \
  --junitxml=/tmp/tuneconsole-534-timing-03/clone-isolation.xml --tb=short --maxfail=1 --durations=5
```

The six Home controls pass in **70.78s**. Cold seed plus prepared-output setup
takes 34.339s; subsequent private setup takes 0.614–0.632s. The complete-menu
check still performs real continuation to twelve menus (11.416s call), ordinary
reads retain their genuine journey prerequisites (11.790s call), and the
generation-race case executes the next preparation (6.667s call). These figures
are local receipts under concurrent machine work, not a controlled whole-suite
speed ratio.

The strengthened clone-isolation case passes in **34.21s**. It builds actual
four-card Home output and 24 wheelhouse profiles, restores both memory and WAL
consumers, mutates one consumer's nested Home payload and bundles, then closes
the source and removes its files. Both consumers still load their own unchanged
Home publication, serve four cards, read all profiles, and satisfy foreign-key
integrity. This extends the earlier profile-only isolation evidence to the
prepared output now shared by the fixture.

Per-test coverage of the remaining read/eligibility/exposure consumer cohort:

```sh
uv run pytest tests/rec/test_home_prepared.py \
  -k 'served_whole or corrupt_prepared_payload or saving_all_wheelhouse or cached_playlist or previews_and_repeated or hold_window or presync_previews or visible_prepared or malformed_menu or current_exclusions or ineligible_held or lost_card or startup_cards' \
  --test-time-limit=120 --test-progress=/tmp/tuneconsole-534-timing-03/home-coverage.jsonl \
  --cov --cov-context=test --cov-config=/tmp/tuneconsole-534-timing-03/coverage.ini --cov-report= \
  --junitxml=/tmp/tuneconsole-534-timing-03/home-coverage.xml --tb=short --maxfail=1 --durations=8
.venv/bin/python scripts/test-coverage-overlap.py /tmp/tuneconsole-534-timing-03/coverage.sqlite3 \
  --progress=/tmp/tuneconsole-534-timing-03/home-coverage.jsonl \
  --output=/tmp/tuneconsole-534-timing-03/overlap-run.json
.venv/bin/python scripts/test-coverage-overlap.py /tmp/tuneconsole-534-timing-03/coverage.sqlite3 \
  --progress=/tmp/tuneconsole-534-timing-03/home-coverage.jsonl --phase=setup \
  --output=/tmp/tuneconsole-534-timing-03/overlap-setup.json
```

The cohort passes **22 cases in 104.95s**, one existing framework deprecation.
The separate coverage configuration retains the C tracer, `relative_files=True`,
`source=yt_playlist`, and its own data file, avoiding concurrent runs' databases.
Cold setup takes 43.150s under instrumentation; private setup thereafter takes
0.995–1.194s. All 22 starts have matching completion receipts and exit status 0.
Body contexts share **4,741 of 5,649** observed source lines. Two identical-line
groups contain the ordinary, capped and over-limit polling attempts, separately
for missing and placeholder caches. Those parameters assert distinct rendered
retry values and remain intact. The setup report groups the 21 warm clones;
10,280 lines attributed only to the first case include shared fixture creation,
not unique behavior of that first test. Coverage overlap alone does not establish
assertion equivalence or justify removing a regression.

The seven `home_inputs` consumers also pass **7 cases in 103.26s**, one existing
framework deprecation. Cold input construction takes 30.21s; later private setup
takes 0.620–0.635s. Initial preparation remains real: ordinary calls take
5.869–6.794s, the repeated-source-update check takes 18.613s, and accumulating
all twelve menus in separate bounded attempts takes 17.677s.

```sh
uv run pytest tests/rec/test_home_prepared.py \
  -k 'generation_shares or album_budget_deferral or reporting_failure or budget_retry or slow_revisit or changed_artist_links or small_preparation_batches' \
  --test-time-limit=120 --test-progress=/tmp/tuneconsole-534-timing-03/home-initial.jsonl \
  --junitxml=/tmp/tuneconsole-534-timing-03/home-initial.xml --tb=short --maxfail=1 --durations=8
.venv/bin/python -m coverage html --rcfile=/tmp/tuneconsole-534-timing-03/coverage.ini \
  --show-contexts -d /tmp/tuneconsole-534-timing-03/html
```

The final changed-consumer cohort passes **10 cases in 94.72s**, one existing
framework deprecation:

```sh
uv run pytest tests/rec/test_home_prepared.py \
  -k 'genre_preference_edits or cached_discovery or feedback_changes_the_next or publication_acknowledges or preparation_can_publish or explicit_result_replaces or tuned_result_is_reused' \
  --test-time-limit=120 --test-progress=/tmp/tuneconsole-534-timing-03/home-publication.jsonl \
  --junitxml=/tmp/tuneconsole-534-timing-03/home-publication.xml --tb=short --maxfail=1 --durations=8
```

Together the four disjoint Home cohorts complete all **45 affected parameter
cases** across 33 functions: initial preparation/retries, complete menus,
generation races, current eligibility/metadata, exclusions, exposure, held
choices, polling recovery, changed preferences, concurrent unrelated writes,
deadline/shutdown before commit and after reader cleanup, explicit replacements
and tuning expiry. All have matching end receipts and exit status 0. The twelve
unchanged fresh/file-backed cases retain the preceding complete module pass;
they were not repeated for another report. No assertion was removed.

### Dynamic runtime target, following the user's correction

The fixed two-minute whole-suite cutoff above was the agent's choice, not the
user's requested limit. It is superseded for global diagnostics by
`--test-runtime-target=900`. The coordinator knows the selected count and tracks
completed cases against the 15-minute completion line. Its lag is elapsed wall
time minus `900 * completed / selected`; by default it allows 10% of the target
(90 seconds) for startup and uneven case costs. Every completed case advances
the allowance. `--test-runtime-slack=SECONDS` changes that allowance explicitly.
A short fixed cutoff is no longer the criterion for stopping a full run.

Once a run exceeds the allowed lag, a `pace_stop` JSONL/console receipt records
the target, allowance, elapsed time, lag, completed/selected counts, active test
names/phases/durations, and ten slowest observed cases with phase timings.
Only that coordinator writes its private stop flag; its own workers poll it
and interrupt themselves, preserving teardown and reports. Workers never infer
global pace from their unequal file assignments. The temporary control directory
is removed during cleanup, and machine lock files remain intact. Counts form a
diagnostic progress line, not proof that a partially run tail would pass.

```sh
uv run pytest tests/test_pytest_diagnostics.py \
  --test-runtime-target=60 --test-runtime-slack=15 \
  --test-progress=/tmp/tuneconsole-534-timing-04/dynamic-diagnostics.jsonl \
  --junitxml=/tmp/tuneconsole-534-timing-04/dynamic-diagnostics.xml --tb=short --maxfail=1 --durations=8
```

All **16 diagnostic checks pass in 42.20s**. Real serial and two-worker processes
prove both outcomes: healthy progress continues past its initial allowance,
while blocked setup and calls stop after falling behind, identify the correct
active case/phase, clean fixtures, preserve completed failure/pass receipts and
save/merge per-test coverage. The existing fixed-limit and signal-interruption
regressions still pass. The fixed limit remains available for focused sessions;
it and the dynamic mode are mutually exclusive.

The first real dynamic global diagnostic selected **13,091 tests in 1,032 files**:

```sh
uv run pytest -m '' -n 4 --test-lock-wait=60 --test-runtime-target=900 \
  --test-progress=/tmp/tuneconsole-534-timing-04/progress.jsonl \
  --junitxml=/tmp/tuneconsole-534-timing-04/junit.xml --durations=30 --maxfail=1 \
  -o faulthandler_timeout=30 --basetemp=/tmp/tuneconsole-534-timing-04-workspace
```

It stopped based on measured lag at **157.17s**: 973 completed cases earned
66.89s on the target line, leaving **90.28s lag** against the 90s allowance.
Teardown/reports finished at **160.53s (2m40s)**: **973 passed**, four framework
warnings, exit status 2, no failures observed. Four active cases and the unrun
tail remain unverified. The active cases were domain rejection of infinite
`shown_at`, genuine predecessor-v21 replay, Home album-budget deferral and a
memory snapshot-reader setup. The `pace_stop` receipt retains their exact IDs,
phases and elapsed phase times, plus the ten slowest observed cases. The
disposable workspace and private runtime-control directory were removed.

That receipt identified avoidable domain-fixture commits. A direct trace of the
`taste_modes.size=-1` case split construction, actual regular admission and
actual rejection, using a `TemporaryDirectory` for its isolated Store and home.
Before: **12.048/8.199/0.212s**, with 433 CREATE statements outside a transaction.
After constructing the same independent v103 fixture inside one transaction:
**0.363/8.098/0.222s**, all 1,593 observed CREATE statements within transactions.
This changes only test input construction; each case retains real admission,
constraint rejection, byte-for-byte retained data, rollback and restart checks.
Reports: `/tmp/tuneconsole-534-timing-04/domain-profile-{before,after}.json`.
The timings isolate the builder's cost under concurrent machine work rather than
establish a controlled whole-suite speed ratio.

Five Bridge route event tests also replace unclosed fresh-bootstrap Stores with
the existing private Store fixture, retaining their account setup, live/untagged
frames, paused presence, raw event and rich-heartbeat assertions. All **97 route
cases pass in 34.45s**, with ResourceWarning and unraisable warnings as errors:

```sh
uv run pytest tests/web/test_bridge_route.py --test-runtime-target=90 --test-runtime-slack=20 \
  --test-progress=/tmp/tuneconsole-534-timing-04/bridge-after.jsonl \
  --junitxml=/tmp/tuneconsole-534-timing-04/bridge-after.xml --tb=short --maxfail=1 --durations=8 \
  -W error::ResourceWarning -W error::pytest.PytestUnraisableExceptionWarning
```

The complete domain-contract module passes **85 cases in 83.96s**:

```sh
uv run pytest tests/core/test_domain_contracts.py --test-runtime-target=120 --test-runtime-slack=30 \
  --test-progress=/tmp/tuneconsole-534-timing-04/domains-after.jsonl \
  --junitxml=/tmp/tuneconsole-534-timing-04/domains-after.xml --tb=short --maxfail=1 --durations=8
```

The next dynamic global diagnostic, after those two fixture changes, selected
**13,102 cases**. It stopped at **174.00s (2m54s)** with **1,215 complete** and
**90.54s lag**. Cleanup and reports finished at **177.96s (2m57s)**:
**1,215 passed**, four framework warnings, exit status 2, no failures observed.
The disposable workspace and private runtime-control directory were removed.
Compared with the preceding attempt, it completed 242 more cases before the
lag trigger; the changing checkout and concurrent machine work preclude a
controlled speed ratio. This remains an incomplete run, not a full-suite time.

```sh
uv run pytest -m '' -n 4 --test-lock-wait=60 --test-runtime-target=900 \
  --test-progress=/tmp/tuneconsole-534-timing-05/progress.jsonl \
  --junitxml=/tmp/tuneconsole-534-timing-05/junit.xml --durations=30 --maxfail=1 \
  -o faulthandler_timeout=30 --basetemp=/tmp/tuneconsole-534-timing-05-workspace
```

The slow list now includes genuine predecessor replay (roughly 12–14s per
case), cold Home input construction (37.52s), and Home's real repeated-source
preparation (19.69s). A separate cProfile of populated v21 replay and regular
admission took 48.32s under instrumentation: 18.54s cumulative in schema
statement generation and 18.62s in live structural validation, with overlapping
call totals. The predecessor-contract cache had one miss and two hits, so
increasing that cache would not address the observed cost. Profile:
`/tmp/tuneconsole-534-timing-05/predecessor-profile.pstats`. Any reuse must retain
actual database validation and genuine migration execution.

The corrected alternating schema-compilation experiment patched both import
boundaries and observed seven cache hits/five misses. Warm replay measured
6.504s without reuse and 6.019s with it; seventeen shipped versions contain
7.85 MB of SQL. No compilation cache was added for that modest observed gain.
Caching only pure FK-deferral parsing likewise changed eight ordinary Store
admissions from 3.336/3.362s to 3.196/3.268s; that candidate was not retained.

Profiling ordinary Store admission instead identified duplicate whole-layout
validation: routine admission validates both histories and their owners in one
read snapshot, then unnecessarily starts the regular runner even when every
regular change is complete. The runner now invokes regular migration only when
that validated history has pending changes. Pending changes retain independent
snapshot/reservation/revalidation; no installed result is cached. Alternating
eight-admission measurements were **3.412/3.323s** with the previous second pass
and **2.409/2.414s** with one pass (27–29% less for this operation). These are
private in-memory Store admissions in one process, not a suite speed ratio.
Report: `/tmp/tuneconsole-534-timing-05/single-admission-experiment.json`.

Installed validation plus consumer fixture checks pass **19 cases in 60.37s**,
including one read-only admission, later installed/history damage, genuine
fresh-bootstrap parity and private memory/WAL/browser fixture ownership.
Independent regular-schema checks pass **10 cases in 76.21s**, covering pending
changes, rollback, CLI, competing startups, writer availability, retained
preparation evidence and genuine owner upgrades. Their repeated empty v79 input
now uses the existing genuine read-only predecessor template; each case retains
its own file and real regular changes/upgrades. The same ten cases then pass in
**45.08s**; later setup falls to 0.090–0.185s, compared with 3.270–4.183s.
Two additional cases with a removed regular owner (v79 and current) pass in
**16.60s**. Valid history alone therefore does not admit damaged regular owners.

```sh
uv run pytest tests/core/test_installed_schema_validation.py tests/test_store_fixture.py \
  --test-runtime-target=120 --test-runtime-slack=30 \
  --test-progress=/tmp/tuneconsole-534-timing-05/admission-after.jsonl \
  --junitxml=/tmp/tuneconsole-534-timing-05/admission-after.xml --tb=short --maxfail=1 --durations=8
uv run pytest tests/core/test_independent_schema_changes.py \
  --test-runtime-target=120 --test-runtime-slack=30 \
  --test-progress=/tmp/tuneconsole-534-timing-05/regular-template-after.jsonl \
  --junitxml=/tmp/tuneconsole-534-timing-05/regular-template-after.xml --tb=short --maxfail=1 --durations=8
uv run pytest tests/core/test_installed_schema_validation.py -k regular_owner \
  --test-runtime-target=45 --test-runtime-slack=15 \
  --test-progress=/tmp/tuneconsole-534-timing-05/regular-owner-after.jsonl \
  --junitxml=/tmp/tuneconsole-534-timing-05/regular-owner-after.xml --tb=short --maxfail=1 --durations=4
```

The next full desktop/TV selection, after the single-admission and independent
schema-fixture changes, selected **13,110 cases in 1,034 files**. At 151.98s it
had completed 1,206 cases and was 69.19s behind the target line. It stopped at
**177.00s (2m57s)** with **1,262 complete** and **90.36s lag**; reports and teardown
finished at **180.13s (3m00s)**: **1,262 passed**, four framework warnings, exit 2,
no failures observed. Disposable fixture/control directories were removed and
permanent lock files retained. The incomplete tail remains unverified.

```sh
uv run pytest -m '' -n 4 --test-lock-wait=60 --test-runtime-target=900 \
  --test-progress=/tmp/tuneconsole-534-timing-06/progress.jsonl \
  --junitxml=/tmp/tuneconsole-534-timing-06/junit.xml --durations=30 --maxfail=1 \
  -o faulthandler_timeout=30 --basetemp=/tmp/tuneconsole-534-timing-06-workspace
```

Active cases were Spotify durable reopen, genuine v23 replay, Home's bounded
complete preparation and missing-plan recovery. The slowest observed setup was
the shared cold Home input builder at 54.21s, versus 37.52s in the preceding
attempt; whole-run ratios therefore remain uncontrolled. Historical replay and
native recovery retain real migrations, precision and file ownership checks.
HEAD remained `bc5746b0e45d684fd86f87ea8e8a7343973dbdfb` with the dirty shared
checkout; these local diagnostics do not establish hosted 15-minute acceptance.

The Spotify/Deezer durable-reopen consumers identified by that receipt now clone
only their initial genuine empty current database. Each retains actual initial
admission, durable writes, connection close/reopen, deadline recovery and newer
outage preservation. Both complete provider modules pass **107 cases in 45.26s**
with per-test coverage, ResourceWarning/unraisable warnings as errors, and one
existing framework deprecation. The reopened case calls take **1.167/1.064s**
under coverage, versus roughly 9s each in the preceding uninstrumented concurrent
full run; these are different conditions, not a controlled speed ratio.

```sh
uv run pytest tests/providers/test_spotify.py tests/providers/test_deezer_iao.py \
  --test-runtime-target=180 --test-runtime-slack=45 \
  --test-progress=/tmp/tuneconsole-534-timing-06/providers-after.jsonl \
  --junitxml=/tmp/tuneconsole-534-timing-06/providers-after.xml \
  --cov --cov-context=test --cov-config=/tmp/tuneconsole-534-timing-06/coverage.ini --cov-report= \
  --tb=short --maxfail=1 --durations=8 \
  -W error::ResourceWarning -W error::pytest.PytestUnraisableExceptionWarning
.venv/bin/python scripts/test-coverage-overlap.py /tmp/tuneconsole-534-timing-06/provider-coverage.sqlite3 \
  --progress=/tmp/tuneconsole-534-timing-06/providers-after.jsonl \
  --output=/tmp/tuneconsole-534-timing-06/provider-overlap-run.json
.venv/bin/python scripts/test-coverage-overlap.py /tmp/tuneconsole-534-timing-06/provider-coverage.sqlite3 \
  --progress=/tmp/tuneconsole-534-timing-06/providers-after.jsonl --phase=setup \
  --output=/tmp/tuneconsole-534-timing-06/provider-overlap-setup.json
.venv/bin/python -m coverage html --rcfile=/tmp/tuneconsole-534-timing-06/coverage.ini \
  --show-contexts -d /tmp/tuneconsole-534-timing-06/provider-html
```

All 107 cases have completion receipts and exit status 0. Test bodies share
**3,924/4,630** observed lines, with **21 identical-line groups**. Inspection
shows distinct URL trust rules, response shapes/statuses, retry values, vendor
codes and cancellation classes despite identical executed lines; all assertions
remain. Setup shares 1,134/6,203 lines, with 5,069 cold-construction lines charged
to the first Store consumer; that attribution is not unique case behavior.
HTML: `/tmp/tuneconsole-534-timing-06/provider-html/index.html`.
The global result above predates these two final consumer fixture changes.
No completed global or hosted 15-minute timing is established; #534 remains
In progress. No commit or push was made.

## Prioritized recommendations

| Item | Disposition and preserved contract |
| --- | --- |
| P1. Successful fake writes | Seed real fake catalog/readback state in duration, generated-playlist, cluster-save, account-selection, fresh-enrichment and mode-engagement fixtures. `tests/playlist_fixture_support.py` shares catalog construction. Incomplete delivery, retry and acknowledgment failure fixtures retain their original behavior; production retry logic is unchanged. |
| P2. Historical schema setup | `core/test_current_schema.py` builds genuine predecessor templates with SQLite backup and computes the fresh contract once. Each upgrade, idempotence, foreign-key and integrity check still has its own clone. Relationship tests clone a genuine pristine v24 template and explicitly replay historical upgrades before ordinary admission. Their fourteen invalid-write checks share one v25 upgrade, retaining every rejection and rolling back even unexpectedly accepted writes between cases. This tests the actual historical constraints rather than retired read-only views. Populated evidence, nonnumeric ordering, current history/feedback, reopen idempotence and interrupted-upgrade rollback retain separate real upgrade paths. Capture-evidence and recording-vibes upgrade coverage remains intact. |
| P3. Radio motion | Transfer dense fork-lag and fixed-control clearance checks into the stronger motion journey, then remove the older 75-frame duplicate. Preserve broad pure geometry sampling, all four shapes, reversal, desktop behavior and motion under reduced-motion preferences. The third 1792-by-1398 case retains layout, covers, history navigation and scrolling while omitting the seven repeated motion traversals. Jump the hidden 30-second phase; retain real intervening frames in the 20-second evolution assertion. Diagnostic captures are opt-in. |
| P4. Prepared Home fixtures | `home_fixture_support.publish_menu` publishes four valid cards with sixteen real catalog subjects through the production cache writer. Regeneration failures, tuning geometry, discovery/story exposure and Home markup consumers use this instead of ranking/preparing a library. Analytics retains its real 84-day facts but no longer prepares unrelated Home recommendations. Baseline/tuned eligibility uses two disjoint sixteen-subject publications. Real ranking, publication races, readiness, first-user progression and vibe persistence tests retain their actual pipeline. Post-unlock control tests explicitly isolate the growth gate; growth tests do not use that override. No mutable Store is shared. |
| P5. Similar dialog | Consolidate the overlapping journeys, preserving both parents, desktop widths and artwork states. Remove phone variants under the user's scope correction. Bounds, image recovery, checkbox selection, action reachability, scrolling, overflow and Escape remain. Reachability scrolls the actual last rendered action rather than assuming seed order equals display order. |
| P6. Regeneration failure matrix | Run all six failures once with a compact published menu, reading the actual button endpoint rather than rewriting `hx-post`. Two short HTTP/template cases render both actual variants and verify endpoints, targets, swap/trigger attributes, shared handler and held opportunity. Preserve network failure, HTTP error with valid content, 422, missing/invalid fragments and empty success; each still checks deduplication, retry, original rows and animation cleanup. The real held-opportunity/animated-swap integration remains separate. |
| P7. Deterministic waits | Replace slow playlist deletion sleeps with entered/release Events and release in cleanup. Discovery/story clocks pause during the initial hidden interval; explicit frame steps deliver Alpine's deferred panel reveal and native IntersectionObserver updates before advancing dwell time. Real requests and persisted counts still establish hidden, visible and fragment-poll behavior. The response barrier matches the requested tile's subject/kind/ID so another visible tile's concurrent POST cannot release it early. Analytics likewise waits for its actual Alpine observer/timer. The retained audio-card test runs every normal page tick under a clock, records request times in that clock and preserves retry-spacing/no-retry assertions; recovery waits for the real server response. This replaces 24.4 seconds of exposure waits and eleven seconds of audio retry waits, not a measured wall-clock speedup. The story test also waits for real lazy-fragment completion. Actual extension/media timing remains where page clocks do not control the isolated world or native capture. |
| P8. Fair queue | Keep one compact full drain with unique small-lane survival, blocked exclusions and exhaustion. Preserve partial-consumption, ties, muting, suppression and trace tests. Move the repeated 8,000/32,000 print-only timing comparison to `experiments/investigations/bench_fair_queue.py`. |
| P9. Policy retention | Ordinary preparation retention uses eight generations, four variants and 256-byte evidence while crossing a patched grace period; actual typed publication, bounded owner cleanup and exact latest configuration remain. `bench_policy_retention.py` retains the original 80-generation, 1,000-variant production-default budget on a real file database. All variants are installed through preparation, with one representative candidate pool: policy storage scale is the contract, not a thousand artificial pool publications per generation. The first generation verifies every variant was installed. The original 7 MiB default bound passes. The 1,008-variant sharing check includes canonical file wrappers in its explicit 512 KB ceiling, versus roughly 48 MB unshared. Both legacy byte-limited/resumable cleanup cases use genuine v69 SQL owners, not retired table names on the current schema, and pass with small fixtures. Other untouched legacy policy tests still need reconciliation. |
| P10. Source parsing | Cache immutable source ASTs for architecture traversal and ownership checks; synthetic per-source AST cases remain fresh. Cache parsed release-workflow YAML by file name; all thirty-nine workflow checks pass. No architecture assertion was removed. Three architecture guards failed in the recorded baseline and still failed after caching. |
| P11. Held-out fits | Extract `outer_fold` into the real robust-training implementation and use that same helper for robust and semisupervised before/after isolation checks. Real fitting, normalization, selection and unlabeled-cohort checks remain. Keep complete nested pipeline/export tests. Retain gold/silver full-cohort fitting because its assertions span teacher eligibility across folds and final export. Malformed-artifact tests already construct an artifact directly in the current snapshot; no training removal is needed there. CLI invocation/immutability coverage remains. |
| P12. Held writers | Combine twenty reader scenarios into four real file-backed writer states. Run all five foreground readers while each writer is held, retain private-reader closure checks and post-commit/rollback observations. No mock replaces contention. |
| P13. Pure JavaScript | Move all thirteen sync outcome, transport-recovery and disposal scenarios to `test_sync_outcomes_js.py`, loading actual `home.js` in a fresh Node VM per case. Keep native browser sync/SSE integrations. |
| P14. Data scale | Discovery cleanup uses four compact operation/staging cases plus two retained 8,000-track direct/staged cases. Recent-evidence membership fixtures use three old observations and real catalog/embedding owners while keeping all eight consumer/decode guards. Unrelated churn falls from 1,000/4,000 writes to four, with exact dependency/scoped-capture assertions retained. Retain policy-upgrade volume because the shared genuine v69 harness also proves physical shrink, WAL restoration and resumed compaction; splitting its smaller normalization/early-deferral seeds remains a follow-up during ongoing storage-harness changes. Five Home-refill eligibility cases now use thirty-two subjects total while retaining fresh/held offer capture, source selection, blocked counts and exact generation/context checks. |
| P15. Model dispatch/provenance | Item2vec dispatch uses valid fixed-vector spies while checking actual selection/publication. Five generated-history provenance cases use a fixed published vector space; numerical recall, temporal evaluation and real fit checks retain training. The older duplicate five-fold content-projection report is absent from the current snapshot; distinct leakage/recall integrations remain. |
| P16. Desktop transactions | Keep one 1280-pixel native MV3/card pause-resume journey and one identity typing/help journey. Home-vibe slow-request checks retain feedback visibility on desktop. Retain now-playing parents and desktop widths because they exercise different dock parents, source-return wiring and popup geometry. Similar-dialog checks retain both parents and artwork states on desktop. Phone widths and device emulation are excluded. |
| P17. Resource ownership | Close root/local Store fixtures with yield/finally; bound server startup/join cleanup in consolidated browser fixtures. Bridge readiness uses an Event and its loop/thread is stopped, joined and closed. Extension prompt-lock readiness uses one asynchronous `wait_for_function` instead of repeated browser roundtrips. Actual loop/thread and test-lock regressions remain. Other HTTPServer polling intervals are retained because cancellation/shutdown timing is part of their integration behavior; no global lock changes were made. |

## Concrete consolidations

| Item | Retained coverage |
| --- | --- |
| D1. Merge browser | Initial 2/2 and changed 1/2 assertions join the stronger apply/return journey. Delivery failure and both return destinations remain. |
| D2. Playlist hearts | Remove two mock-only cases; retain six real backend cases with desired-state payloads, sidebar controls, stale sensor handling and reload persistence. |
| D3. Taste recomputation | One six-to-seven dimension transition checks centroid shape, space stamp, stable IDs and no retired modes. |
| D4. Generated duration | Six default source/materialization combinations plus representative lower/upper custom boundaries replace the independent minute cross-product. Explicit unknown duration, lookup behavior, source precedence, receipts, lookup failure and all-overlong rejection remain. |
| D5. Upload album | One response checks availability, ownership/no Save, columns, enrichment, escaping, navigation and upload playback handoff. Partial-library, missing-artist and cover cases stay separate. |
| D6. Album page | Head tools join the render case; exact full-album share URLs and attachment checks join the live-full-album versus incidental-subset case. |
| D7. Cleanup routes | Detail/editor use one response. Cached-summary deletion checks remote effect, empty body and removed row. Strong three-copy keep-one retains refresh and survivor assertions. Negative live-read, backup and Undo cases remain. |
| D8. Home routes | Combine baseline shell/status/feed checks; fingerprint assertions join hidden-until-genres; playlist HTTP status joins the cross-page player case; breadth clamp is a second POST in the roundtrip. Presync and prepared paths remain explicit. Current route/growth failures remain visible. |
| D9. Onboarding | Thin Home and hidden cards/feed share a GET. Common welcome/invalid input runs once; all three formats retain readiness rejection, persistence and destination checks. |
| D10. Empty PPR modes | Keep the empty-mode result in `test_ppr.py`; remove its weaker duplicate from mode rankings. |
| D11. Breadth | Explicit neutral setup joins the stronger rare-family ranking case. |
| D12. Road Trip | The same forty-percent-rock recipe now checks both size preservation and proportion in one case. |
| D13. Mode cards | One sequence checks baseline, generated membership, unaffected items and exclusion changes. Current mute/invalidation remains separate. Fixtures publish through the current bundle owner. |
| D14. Playlist promotion | One success checks remote generated-marker clearing and local generated-group removal. Remote edit failure stays separate. |
| D15. Nags | Last.fm href joins render/dismiss/persistence. Takeout href and initial welcome/no-nag assertions join stronger response cases. |
| D16. Release promotion | Retain authenticated two-page success plus outage; remove the weaker second-page-only duplicate. |
| D17. Repository roundtrips | Keep exact generated-provenance key-set exclusions. Move unique binding fields into stronger identities, tracks, actions, genres, history, playlists, collection and charts roundtrips before deleting their weaker binding-only tests. |
| D18. Analysis | Near-group detection and dismissal share a graph; identical-group ignore joins membership-change reappearance. |
| D19. Waterfall | Both audit rows join the provider-precedence case. |
| D20. Trends | The promotion regression now actually marks/adopts a generated playlist and clears its group before checking catalog artist counts. |

## Other targeted improvements

| Recommendation | Disposition |
| --- | --- |
| Provider validator fixtures | A second pass splits the Spotify, Deezer and GitHub fixture boundaries: transport-only cases no longer construct a Store. Every durable admission, cooldown, credential, concurrency and restart case keeps its actual database and transport lock checks. DeepSeek's paid admission fixture remains coupled to durable credentials; its current album-identity failures need fixture reconciliation before further consolidation. |
| Spotify eviction | Patch cache capacity to eight and retain eviction/order assertions. |
| Sync truncation | Twenty-six playlists, one with 101 songs, retain both greater-than-25 and greater-than-100 boundaries without building thirty large playlists. |
| Repeated statistical samples | Reuse each sampled result for mode-bandit and transient-recipe assertions. Statistical sample counts are unchanged. |
| Experiment ledger | Retain mature-cohort integration and its negative assignment/probability matrix. These cases prove the report stays powered with 99 controlled and one uncontrolled episode and then rejects promotion. A pure admission stub would lose that connection. Cloning needs a baseline that includes the current policy/ledger owners; the independent-sample requirement was not lowered. |
| Feature assets | Retain the current browser matrix: it verifies loaded execution order, initialized Alpine factories, page errors, responsive overflow and distinct player dock parents. The second pass exposed six baseline failures expecting `genOpenYT` to be undefined outside Home/Road Trip although the shared launcher defines it globally. No assertion was removed or browser case consolidated while that contract is unresolved. |
| Diagnostic screenshots | Home, discovery, stories, analytics, genre badges, adaptive groups and Radio use `tests.visual_support.capture`, enabled by `TUNECONSOLE_VISUAL_REVIEW=1`. Actual pixel assertions and browser failure artifacts retain capture. |
| Node process startup | Retain fresh VM/process cases and per-case failure reports. No process-startup bottleneck was measured, so ownership/mute/deadline permutations were not batched away. |
| Musical preparation | Normal regression uses five recordings for fanout/index behavior. `bench_musical_preparation.py` retains all 96 recordings, the 15-second throughput budget and indexed query contract. |

## All 25 original benchmark modules

Moved files retain their original base names under
`experiments/investigations/`. They were already opt-in, so relocation itself
does not reduce ordinary pytest discovery time.

| Original module | Disposition |
| --- | --- |
| `bench_audio_lab_model.py` | Move historical runtime/CSP controls, CPU/extractor comparisons and natural reference-window studies. Product runtime digest/CSP checks remain in audio-vibes browser/model tests; real cancellation remains in audio-preview/probe tests. |
| `bench_cleanup_analysis.py` | Move old-git-implementation comparison. |
| `bench_database.py` | Move SQL profiling and old-query comparisons. |
| `bench_enrichment_queue.py` | Move priority SQL profiling and diagnostics. |
| `bench_getsongbpm_pilot.py` | Move bounded live-provider/manual matching pilot. |
| `bench_home_database.py` | Move saved-library SQL timing/plan report. |
| `bench_maintenance_logging.py` | Move real-clock log audit/model-fitting investigation; ordinary fake-clock logging regressions remain. |
| `bench_musical_character.py` | Move saved-catalog listening/audit/retune reports. Update audio-lab imports and runtime digest inputs. |
| `bench_musical_provider_pilot.py` | Move live ReccoBeats/manual quality pilot. Neutral matching helpers live in `tests/musical_provider_support.py`; ordinary provider matching imports that helper. |
| `bench_ready_recovery.py` | Move incident-specific saved-snapshot rehearsal. Recurring protected-graph recovery cycles remain in `bench_evidence_recovery.py`, and current daily-selection restore remains in `bench_choice_evaluation.py`. |
| `bench_taste_groups.py` | Split: synthetic engine/feature comparisons move; production preparation/update budgets, membership, lineage and retained definitions remain opt-in in tests. Shared input/report helpers live in `tests/group_benchmark_support.py`. |
| `bench_svd_background.py` | Split: two saved-library before/after comparisons move; four real full-SVD/generation/refresh foreground checks remain opt-in in tests. |
| `bench_analytics_reads.py` | Retain output parity and foreground latency acceptance. |
| `bench_choice_evaluation.py` | Retain current daily-selection shared inputs, full-history fit, concurrent reads and exact restore. Remove the superseded product-comparison scheduler rehearsal (#826). |
| `bench_choice_retention.py` | Retain full Home menus through report refresh and unanswered-offer retirement. Remove the superseded 28-day trial/promotion scheduler rehearsal (#826). |
| `bench_evidence_recovery.py` | Retain quota pressure, repeated recovery cycles and exact restore. Seed controlled historical comparison pairs from saved outputs; do not run the retired scheduler (#826). |
| `bench_group_home.py` | Retain real membership/ranking/resource acceptance. |
| `bench_home_prepared.py` | Retain incremental preparation, real menus and exposure. |
| `bench_progressive_preparation.py` | Retain bounded preparation during ongoing writes. |
| `bench_progressive_recovery.py` | Retain cold/growing-library usefulness and resources. |
| `bench_public_flatpak.py` | Retain signed/public packaged release acceptance. |
| `bench_rec_sparse.py` | Retain sparse-model memory and no-dense-fallback acceptance. |
| `bench_recording_vibes.py` | Retain genuine historical evidence-preserving upgrades. |
| `bench_scorecard_attribution.py` | Retain real batches/index/private-reader acceptance. |
| `bench_vibe_predictions.py` | Retain cold/upgrading prediction usefulness and budgets. |

The new recurring policy-retention and musical-preparation budget files remain
in `tests/`; the new print-only fair-queue study lives with investigations.
`experiments/run.py investigate <file> ...` explicitly loads shared pytest
fixtures, validates the investigation path and does not acquire the global-suite
lock. Documentation and frozen runtime source/digest lists follow moved modules.

## Second redundancy pass, 2026-11-01

The static pass found no identical test bodies of at least nine lines after
ignoring docstrings. That does not establish absence of semantic overlap.

Spotify, Deezer and GitHub transport fixtures implicitly requested a freshly
initialized Store even for URL validation, decoding, bounded response reads,
cache/expiry and response-close checks. Their installers now accept an explicit
Store only when the test actually uses durable admission. This removes 54
unnecessary database initializations from transport-only cases and three more
from restart tests that already own a file-backed database: 57 in total. Every
test name, parameter case and assertion expression remains. The restart tests
now check transport outside the actual reopened Store's lock/transaction,
replacing checks against an unrelated in-memory fixture.

For the 23 passing Spotify cases common to the before/after reports, summed
pytest case time was 109.210s before and 0.028s after. The first run's final
offline-parsing case hit an externally removed default temporary directory;
the subsequent run uses a unique explicit basetemp and passes all 49 cases in
123.40s, including every durable integration. These observations measure focused
case time on an active shared checkout, not a controlled whole-suite speedup.

The browser asset baseline passed all six connected-player checks and failed
six shared-helper visibility assertions (74.29s). Those tests are unchanged.
An attempted DeepSeek fixture split also stopped before transport because album
identity was unresolved; that split was reverted in full. Its focused baseline
reproduces the same two failures in the unchanged file: exact public POST and
rejected-credential restart (2 failed, 24 deselected in 15.84s). Remaining album
owner-fixture work is not reported as successful transport coverage.

All 49 Spotify cases pass (123.40s); all 90 Deezer/GitHub cases pass (283.30s).
Together these establish the changed fixture boundaries across 139 retained
provider cases. Assertions were compared structurally with the pre-edit files;
all test names, parameter cases and assertion expressions are unchanged.

#701 exposed two stale cross-test fixture imports missed during integration.
Both populated checks now import only neutral copy support, copy all owned files
through the existing read-only helper, and explicitly run the historical upgrade
on a disposable copy before ordinary startup/restart. All preservation assertions
remain; neither test stamps a current schema as historical or restores bulk work
to ordinary admission. Both modules collect and skip correctly without a donor.
Three adjacent genuine historical-format checks pass in 32.17s. A static audit
of 1,485 direct imports from test modules found no other unresolved top-level
names. Actual populated schema-95 execution and a fresh PR shard/coverage run
remain #701 review requirements; no suitable retained donor was found locally.

Reports for this pass are `/tmp/tuneconsole-534-provider-before.xml`,
`provider-after.xml`, `provider-other.xml`, `assets-before.xml`,
`paid-provider.xml` and `paid-provider-baseline.xml` with the same prefix, plus
`/tmp/tuneconsole-701-focused.xml`. Reports are outside disposable fixture trees.

Exact final focused commands:

```sh
uv run pytest tests/providers/test_spotify.py --basetemp=/tmp/tuneconsole-534-provider-after --junitxml=/tmp/tuneconsole-534-provider-after.xml --durations=8 --tb=short
uv run pytest tests/providers/test_deezer_iao.py tests/providers/test_github_iao.py --basetemp=/tmp/tuneconsole-534-provider-other --junitxml=/tmp/tuneconsole-534-provider-other.xml --durations=5 --tb=short
uv run pytest -m browser tests/browser/test_feature_assets_browser.py -k 'setup or charts or playlists' --basetemp=/tmp/tuneconsole-534-assets-before --junitxml=/tmp/tuneconsole-534-assets-before.xml --durations=5 --tb=short
uv run pytest tests/providers/test_deepseek_iao.py -k 'exact_post or rejected_credential' --basetemp=/tmp/tuneconsole-534-paid-provider-baseline --junitxml=/tmp/tuneconsole-534-paid-provider-baseline.xml --durations=3 --tb=short
uv run pytest tests/schema/test_populated_action_work.py tests/schema/test_populated_collection_documents.py --collect-only -q --basetemp=/tmp/tuneconsole-701-collection
uv run pytest tests/schema/test_populated_action_work.py tests/schema/test_populated_collection_documents.py tests/core/test_action_work_migration.py::test_upgrade_preserves_action_links_plain_draft_and_unavailable_historical_backup tests/core/test_action_work_migration.py::test_workflow_settings_upgrade_keeps_unknown_accounts_and_ordered_transfer_slots tests/core/test_collection_documents_migration.py::test_upgrade_retains_import_unknown_account_and_verbatim_canvas --basetemp=/tmp/tuneconsole-701-focused --junitxml=/tmp/tuneconsole-701-focused.xml --tb=short --durations=5
```

## Targeted validation and limits

Environment: Python 3.14.8, SQLite 3.50.2, shared changing checkout and concurrent
machine activity. Only explicit targeted pytest paths were run. No full suite,
commit, push or native user-database migration was performed for this ticket.
Pytest uses the allowlisted plain `uv run pytest <paths>` form with no environment
prefix or permission override. Browser cases additionally select `-m browser`.

Reports are `/tmp/tuneconsole-534-<name>.xml`, outside disposable basetemp trees.
Each row is a separate run; counts must not be added as independent coverage.

| Report | Result and meaning |
| --- | --- |
| `schema-after` | 111 passed in 636.84s: genuine predecessor contracts, integrity and idempotence at that checkout state. Later admission/history changes require renewed verification. |
| `similar-rendered-order` | 12 passed in 71.33s: all retained dialog combinations. |
| `browser-after` | 23 passed, 2 setup errors, 47 deselected in 158.78s. Merge, real-backend hearts and Event-held deletion passed; the dialog scroll setup errors were fixed and superseded by the twelve-case run above. |
| `radio-locks` | 5 passed, 14 deselected in 79.76s: stronger 1440/390 motion journeys and three extension lock cases. |
| `album-js` | 31 passed, 5 failed in 77.58s. All thirteen Node scenarios and consolidated upload render passed. Four album readiness requests returned 409 rather than 200; the held-offer check found no opportunity in its ledger. |
| `render-delete` | 28 passed, 104 deselected in 55.48s: selected Node, album/upload, pagination, provenance and cleanup cases. |
| `tail-network` | 151 passed, 4 failed in 448.38s. Two mode fixtures and the truncation fixture were corrected and passed later; first-sync expectation of a nonempty state remains unresolved. |
| `model-isolation` | 57 passed in 124.25s: robust/semisupervised training, full pipeline/export, mode exclusion, item2vec, roll and bandit checks. |
| `readback-dispatch` | 38 passed, 5 failed, 102 deselected in 192.62s. Readback successes, fixed-vector dispatch/provenance and eight repository roundtrips passed. Three account-selection Road Trip cases require a revision, and two scoped album unsaves returned 422. |
| `split-bounds` | 27 passed, 4 failed, 1 skipped in 302.12s. Both retained large discovery cases and 96-recording throughput passed. Four compact discovery cases hit an accidentally retained large-byte assertion, subsequently corrected. Production taste-group acceptance skipped without its frozen input. |
| `discovery-small-final` | 4 passed, 26 deselected in 21.17s after the compact-byte assertion correction. |
| `native-typing` | 1 passed, 1 failed, 31 deselected in 20.40s. Native pause/resume passed; identity typing expected two valid indicators and found zero. |
| `unit-timing-after` | 35 passed, 4 failed in 88.44s: three architecture guards and final Radio publication ownership fence. The corresponding architecture failures were also recorded before caching. |
| `final-core` | 11 passed, 6 failed, 137 deselected in 171.14s. Current Home welcome text, direct bundle publication, preparation readiness and breadth gate differ from expectations; progressive capture lacks two expected dependencies. The generated-mode identity fixture failure was corrected and superseded by `model-isolation`. |
| `small-bounds` | Initial 5 failures in 18.23s were fixture mismatches. Typed policy publication/sharing, genuine legacy scan owners and real recent-evidence subjects are now covered by the passing focused runs below. |
| `fixture-recheck` | 5 failed, 32 deselected in 1.44s: ordinary admission rejects genuine v24 before relationship assertions execute. Historical template setup is genuine; the explicit upgrade harness needs reconciliation. |
| `engagement-readback` | Initial domain-owner setup rejection is superseded by the three passing engagement cases in `owner-recheck`. Fixtures now use the bundle publication owner. |
| `small-fixtures` | Eight recent-evidence cases passed; this run also contained two initially passing template checks and three engagement setup errors. Later template/engagement results supersede those portions. |
| `owner-recheck` | 9 passed, 31 deselected in 68.18s: five compact baseline/tuned eligibility cases, three real engagement/readback cases and compact preparation retention. The retention helper was subsequently narrowed to policy scale and rechecked below. |
| `regeneration` | 6 passed, 1 deselected in 49.89s: every retained failure, deduplication, cleanup, original-row and successful retry assertion with compact publication. |
| `controls` | 5 passed, 11 deselected in 39.97s: four tuning-launcher viewports plus the dense tab/keyboard/genre/subgenre/era geometry journey. |
| `radio-layout` | 2 passed, 6 deselected in 33.92s: tall history/layout/scrolling and real artwork evolution plus hidden-phase jump. Desktop/mobile motion evidence remains in `radio-locks`. |
| `exposure-recheck` | Four discovery cases passed at both widths; two story/analytics fixture failures were subsequently resolved. |
| `story-clock-recheck` / `story-clock-final` | Analytics exposure passed in the first run; the final story run passed in 9.43s after awaiting actual lazy-fragment completion. Hidden headings, visible dwell and poll deduplication remain asserted. |
| `published-consumers` | 6 passed, 11 deselected in 59.16s: all three reading layouts, both real analytics/filtering widths and sparse-history behavior after removing unrelated Home preparation. |
| `menu-contracts` | 12 passed, 1 failed, 54 deselected in 102.04s: eleven first-use/readiness/progression cases and plain template wiring passed. Modeled wiring exposed seven INSERT placeholders for six `mix_choice_options` columns; the source was subsequently corrected elsewhere in the shared checkout. |
| `markup-current` | Both real template variants passed in 11.25s after that source correction; no assertion was relaxed. |
| `menu-readiness` | 5 passed, 46 deselected in 42.46s: one/three/four-card readiness across Home, growth and setup, plus both unusable-offer cases. Explicit tuned fixture publication preserves existing model context. |
| `policy-compact-final` | 1 passed, 14 deselected in 6.60s: eight preparation cycles, patched grace, bounded retirement, byte/count ceilings and exact latest ranker/decay. |
| `policy-variant-budget` | 1 passed in 51.21s: all 80 generations and 1,000 policy variants per generation, production-default grace and original 7 MiB ceiling. The earlier artificial thousand-pool attempts were interrupted (746.40s in memory; 120.14s on disk), not reported as passes or comparable speed measurements. |
| `policy-scan-bounds` | 2 passed, 13 deselected in 2.53s: genuine SQL-policy predecessor, byte-limited cursor resumption and bounded orphan-body collection. |
| `audio-retry-clock` | 1 passed, 33 deselected in 7.20s: actual failed/successful HTTP loads, all interval ticks, backoff spacing, saved answer restoration and no further loads. |
| `workflow-cache` | All 39 workflow checks passed in 1.59s with parsed YAML reused. |
| `final-fixtures` | 17 passed, 11 deselected in 95.80s on the later shared checkout: both actual markup variants, all recent-evidence cases, engagement/readback and the selected compact retention/sharing/legacy-scan cases. |
| `final-relationships-v2` | All 10 retained tests passed in 69.46s, including all fourteen rejected writes within one rollback-isolated historical constraint matrix. The first draft replayed unrelated later metadata conversions and hit retired-view/row-ID mismatches; this passing run supersedes it and the earlier `fixture-recheck` admission failures. |
| `final-browser` | 9 passed, 1 failed in 90.62s. All six regeneration failures, both discovery widths and retained audio retry passed. Story dwell exposed an imprecise response barrier: an adjacent subject's POST could release it before the target write. The barrier now matches the actual requested subject or discovery kind/ID. |
| `final-guards` | 66 passed, 3 failed in 28.08s. All 13 Node scenarios and 39 workflow checks passed. The same three architecture guards fail in `/tmp/tuneconsole-534-baseline.xml`: import-cycle detection, repository-owner callers, and frozen-numbered-migration runtime imports. Their assertions remain intact. |
| `final-relationships-merged` | All 10 passed in 72.03s after a concurrent merge restored older test files and the finishing changes were reapplied. This establishes the actual remaining working-tree implementation, including all fourteen historical rejection checks. |
| `final-exposure-reveal` | All 5 passed in 49.29s: story hidden/visible/poll behavior and both widths of discovery exposure and real prepared-song readiness. The clock is now actually paused, deferred panel reveal receives explicit frame time, and response matching identifies the target tile. Supersedes the intermediate runs that exposed a naturally advancing clock, a frozen panel reveal, and an app-signature mismatch during concurrent merging. |

Additional focused checks: generated-playlist promotion integration passed
(2 cases, 8.17s); `./experiments/run.py check --syntax-only` parsed 194 files;
the moved taste-group and musical-provider investigations each collected one
case through `./experiments/run.py investigate <file> --collect-only -q`.
The six screenshot-helper browser modules collected all 75 current cases.

Representative exact invocations, with unique basetemp/report paths omitted:

```sh
uv run pytest tests/test_audio_vibe_robust_training.py tests/test_audio_vibe_semisupervised.py tests/test_mode_cards_no_repeat.py tests/test_item2vec.py tests/test_roll_recipe.py tests/test_mode_bandit.py --tb=short --maxfail=5
uv run pytest tests/repos/test_discovery_cleanup_isolation.py -k 'large_cleanup and not 8000' --tb=short --maxfail=5
uv run pytest -m browser tests/browser/test_setup_browser.py tests/browser/test_play_pause_confirmation_browser.py -k 'identity_guide_stays_open or card_confirms_pause_resume' --tb=short
uv run pytest tests/repos/test_relationship_constraints.py tests/repos/test_discovery_cleanup_isolation.py -k 'invalid_direct or populated_upgrade or failed_upgrade or non_numeric or 8]' --tb=short --maxfail=5
uv run pytest tests/test_mode_engagement_integration.py --tb=short --maxfail=1
uv run pytest -m browser tests/browser/test_mix_regeneration_browser.py -k recovers_from_failure --tb=short
uv run pytest -m browser tests/browser/test_into_recently_browser.py -k story_cooldown --tb=short
uv run pytest tests/rec/test_policy_storage.py -k 'legacy_cleanup or orphan_body or repeated_advancing' --tb=short
uv run pytest tests/bench_policy_retention.py --tb=short
uv run pytest tests/test_release_workflows.py --tb=short
uv run pytest tests/repos/test_relationship_constraints.py --tb=short --maxfail=2 --durations=5
uv run pytest tests/test_sync_outcomes_js.py tests/test_release_workflows.py tests/test_architecture.py --tb=short --maxfail=4 --durations=5
uv run pytest -m browser tests/browser/test_into_recently_browser.py::test_story_cooldown_only_counts_visible_headings_and_ignores_fragment_polls tests/browser/test_discovery_browser.py::test_only_visible_discovery_tiles_count_once_per_visit tests/browser/test_discovery_browser.py::test_prepared_songs_are_ready_when_discover_opens --tb=short --maxfail=1 --durations=5
./experiments/run.py check --syntax-only
./experiments/run.py investigate bench_taste_groups.py --collect-only -q
./experiments/run.py investigate bench_musical_provider_pilot.py --collect-only -q
```

Timing observations do not establish a controlled speedup. Three matched
predecessor cases totalled 35.347s before and 31.116s after; eleven matched passing
unit cases totalled 30.058s before and 25.076s after. Different selections,
template amortization, concurrent edits and machine activity prevent attributing
those differences solely to this change. A same-revision/environment comparison
of both lanes remains outstanding. Full-suite work remains with the review owner,
subject to the user's current restriction to focused tests. Historical CI weights are not
measurements of these changes.

Required review follow-up: establish which earlier current-storage/Home/typing
failures remain after ongoing integration, and recheck their affected focused
contracts. The relationship fixture admission mismatch is resolved; the broader
predecessor matrix still needs integration verification after storage changes.
Run taste-group production acceptance with its frozen input. The policy budget
is verified against its actual owner. The three baseline architecture failures
remain visible. No current whole-suite status is established.

Implementation handoff: all seventeen priorities, twenty consolidations, other
recommendations and twenty-five original benchmark modules have a recorded
disposition. The finishing fixture and clock changes have passing focused
evidence. Independent acceptance, controlled end-to-end runtime comparison and
the broader shared-checkout integration gaps above belong to review. No full
suite was run for this handoff, and no implementation assertion was removed to
hide a baseline failure.

## Recorded failure tickets, 2026-11-01

The unresolved observations above are now assigned to the following focused
Ready / P2 tickets. Each contains exact failing cases, recorded diagnostics,
focused reproduction commands and acceptance criteria. Filing did not rerun the
tests against later shared-checkout changes; these are recorded failures to
reconcile, not newly established production regressions.

| Group | Ticket |
| --- | --- |
| Home/album readiness, typed publication and unlocked consumer fixtures | [#714](https://github.com/reubenfirmin/TuneConsole/issues/714) |
| Account-scoped Road Trip draft and album unsave contracts | [#715](https://github.com/reubenfirmin/TuneConsole/issues/715) |
| Radio coherent publication through the current owner | [#716](https://github.com/reubenfirmin/TuneConsole/issues/716) |
| First-sync persisted retry state | [#717](https://github.com/reubenfirmin/TuneConsole/issues/717) |
| Progressive repair dependency capture and invalidation | [#719](https://github.com/reubenfirmin/TuneConsole/issues/719) |
| Identity typing, save feedback and help visibility | [#721](https://github.com/reubenfirmin/TuneConsole/issues/721) |
| Feature asset guards and shared playlist launcher | [#722](https://github.com/reubenfirmin/TuneConsole/issues/722) |
| DeepSeek album identity and paid-admission fixtures | [#723](https://github.com/reubenfirmin/TuneConsole/issues/723) |

The three baseline architecture guards were added to existing Ready / P2
[#583](https://github.com/reubenfirmin/TuneConsole/issues/583). The disappearing
Spotify basetemp observation was linked to existing Ready / P2
[#703](https://github.com/reubenfirmin/TuneConsole/issues/703); that provider
case's later pass does not establish safe concurrent-session cleanup.
Superseded intermediate failures were excluded from new tickets.

The pushed PR run
[41316661540](https://github.com/reubenfirmin/TuneConsole/actions/runs/41316661540)
failed collection because the tracked cleanup test loads a script absent from
the pushed checkout. Existing P1
[#713](https://github.com/reubenfirmin/TuneConsole/issues/713) owns that blocker.
The original populated-helper import errors in
[#701](https://github.com/reubenfirmin/TuneConsole/issues/701) were absent from
the sampled new logs. Successful shard execution remains unestablished.
This failed run is not a completed-suite runtime measurement.

The user subsequently clarified that the local housekeeping script does not
need automated suite coverage. `tests/test_test_artifacts_cleanup.py` has been
removed, eliminating its collection-time import. The manual utility remains.
#713 is handed to review pending the next user-controlled push and CI run.

## Selected Vibe reads, 2026-11-02 (#534)

The current typed owner receives the same decoded publication for plain, v1 and
v2 private input encodings. The former 24-case cross product repeated complete
1,096-subject seeding, profile publication and unrelated-pool hydration for each
encoding and filter. Three pure round-trip checks now prove complete codec
parity. Two database cases retain both lanes and independently check all four
include/avoid scenarios against the full-hydration reference. They preserve the
200-item pools above the 192-item production bound, selected-profile spies,
candidate order and filter assertions. Each case owns its Store; no live mutable
fixture is shared between tests. Retry, invalidation, detached results, dislike
rechecks and absent/old-state tests remain.

Focused measurements (Python 3.14.8 / pytest 9.1.1):

```sh
uv run pytest tests/rec/test_vibe_pool_reads.py -k 'plain and fresh' --tb=short --durations=5 --maxfail=1 --junitxml=/tmp/tuneconsole-534-vibe-before.xml
uv run pytest tests/rec/test_vibe_pool_reads.py --tb=short --durations=5 --maxfail=1 --junitxml=/tmp/tuneconsole-534-vibe-after.xml
```

Before: four fresh/plain filter cases passed in 148.29s (calls 33.92–36.93s).
After: the complete revised module passed nine cases in 141.23s; its Fresh case,
covering all four filters, took 44.52s, Explore 40.56s and retry 42.62s. Concurrent
focused work and ongoing shared-checkout changes prevent treating these as a
controlled machine benchmark. The concrete reduction is 24 database setups to
two, with three separate codec checks. No whole-suite projection is made.

The required complete intended desktop/TV CI runtime of at most 15 minutes still
needs a future hosted run. Run 41497980082 aborted collection, tracked by #825;
it establishes neither passing behavior nor that runtime target. The current
user explicitly prohibited a full suite. No full suite, commit or push was run.

## Hosted bottlenecks and fixture boundaries, 2026-11-02 (#534)

Downloaded artifacts from PR run 41528680110 identify actual work rather than
duration-plan estimates. Unit shards 1–3 completed with failures; shard 4 has a
partial journal and no completed JUnit report. These are baseline measurements
of the hosted merge, not measurements of the subsequent local edits.

| Module | Recorded seconds | Setup / call seconds | Decision |
| --- | ---: | ---: | --- |
| `rec/test_home_prepared.py` | 1748.05 | 1149.34 / 597.84 | Full preparation is still expensive. Preserve ranking, incomplete-processing, publication and recovery contracts; small published-menu consumers already have a separate fixture. |
| `test_radio_intensity_priority.py` | 772.77 | 385.84 / 386.44 | Retain the 110-subject tail that proves refill beyond lookahead; do not lower that boundary to meet a timing target. Separate publication cost from the live-retune contract in further profiling. |
| `core/test_current_schema.py` | 737.74 | 4.53 / 733.14 | Real historical migration/admission work dominates; clone reuse does not remove the required migration matrix. |
| `rec/test_product_comparison.py` | 545.34 | 38.01 / 506.70 | #826 removes the superseded scheduler and its exclusively associated tests; current daily-selection coverage remains. |
| `rec/test_home_refill.py` | 499.33 | 339.11 / 159.86 | Partial shard-4 journal only. Fixture preparation is a separate remaining cost from refill behavior. |
| `core/test_snapshot_revisions.py` | 321.42 | 303.11 / 17.47 | Reuse pristine memory/file SQL templates while keeping separate Stores and the complete memory/file case matrix. Explicit external-writer/bootstrap controls remain genuine. |
| `core/test_metadata_runtime.py` | 200.42 | 194.83 / 5.12 | Inherit a private file-backed Store, then run actual runtime installation/activation and every existing publication, expiry and recovery assertion. |
| `repos/test_artist_identity.py` | 170.66 | 135.81 / 32.42 | Inherit pristine Store; retain each test's own post-case schema validation. |
| `core/test_repair.py` | 150.20 | 149.55 / 0.17 | Inherit pristine Store and retain actual repair mutations/assertions. |
| `browser/test_home_vibe_browser.py` | 897.74 | 79.64 / 808.65 | Seven UI-consumer cases use four published cards and a controlled assembly boundary. Real ranking/persistence/reset and genre-focus integration cases remain separate. |

The longest individual unit call was the Radio refill-beyond-lookahead check
(162.067s total case time); it protects a unique boundary. Home's hydrated-menu
and cache/restart cases took about 83.59s and 81.81s. Several browser Vibe cases
took 80–103s apiece. Slowness alone does not establish redundancy. The shared
fixture changes remove repeated empty-schema construction without removing
assertions. Fresh-schema DDL/logical-row parity, read-only template enforcement,
foreign keys and isolation are checked for memory, disk and browser consumers.
Twenty-five duplicate empty Store fixtures now inherit that root fixture; eight
custom consumers retain their seeds and validation around it. Returning-account
browser setup also inherits the private WAL fixture.

The seven Vibe consumers continue to exercise actual requests, settings,
publication identity, templates, HTMX replacement, timers and retry/readiness
messages. Their assembly fixture returns detached copies of a real baseline
publication with the actual epoch and selected Vibe. It does not establish
ranking quality or background-worker readiness. Existing full-pipeline Vibe
journeys retain that responsibility. This is an explicit integration boundary,
not an assertion that mocked assembly proves preparation.

Focused results (Python 3.14.8 / pytest 9.1.1):

- Store parity plus genuine historical Radio choice admission: 15 passed in
  58.33s; `/tmp/tuneconsole-534-disk-fixtures.xml`.
- Six changed consumers including repair, impressions and choice evidence:
  70 passed in 82.08s; `/tmp/tuneconsole-534-consumer-fixtures.xml`.
- Complete snapshot-revision and metadata-runtime modules: 87 passed in
  101.21s; `/tmp/tuneconsole-534-heavy-empty-fixtures.xml`.
- Seven revised Vibe browser consumers: 7 passed in 66.09s;
  `/tmp/tuneconsole-534-vibe-consumers.xml`.
- Complete artist-identity and mode-ranker modules: 34 passed in 87.65s;
  `/tmp/tuneconsole-534-owner-fixtures.xml`. Historical upgrade cases still
  dominate their call times (about 18 seconds apiece) and remain genuine.

Run these with `uv run pytest` and the named module paths; Vibe requires
`-m browser` and selection
`-k 'default_vibe or slow_vibe or feedback_replacement or feedback_waits or open_vibe_tab'`.
Private clone admission measured about 0.47s versus about 5.5s for a genuine
bootstrap in the fixture checks. Concurrent machine work prevents a controlled
CI speedup claim. No test functions/assertions changed in the 34 original
fixture-consumer modules; the later two heavy consumers also retain their
complete test functions.

The discovery-reassociation consumer cohort stopped at 2 passed / 2 failed in
8.33s (`/tmp/tuneconsole-534-parent-fixtures.xml`). Both failures also occur in
the earlier hosted shard 2: archived source evidence is empty where the tests
expect `{'a'}` or `{'a', 'b'}`. #660 owns that contract; the assertions remain.
Current CI failures and the unusually long fourth browser shard belong to #814
and its focused failure tickets. No complete hosted runtime receipt for these
changes exists. The required 15-minute target remains unestablished, and the
user's prohibition on a local full suite remains in force.

## Immutable Home and Radio seed reuse, 2026-11-03 (#534)

This section records the earlier seed-only pass. The prepared-output reuse
continuation above supersedes its per-consumer Home preparation description.

The Home `prepared` consumers and Radio `distant_chill` consumers now build their
unchanged real seeds once per module. Each test first restores a private SQL
clone and byte copies of the seed's file domains into its own namespace. Filename
coordinates are relocated under the existing coordinate-update transaction;
packed offsets and FAO logical ages are unchanged. Copies use no hard links.
The seed SQL is read-only and every owned memory directory has close cleanup.
This helper is for these generated current seeds, not a general installed
library backup or migration mechanism. Non-null unmanaged coordinates fail
instead of silently borrowing a source file. File-only historical preparation
domains have no namespace in the memory source.

The override runs while constructing `store`, before returning-listener, account
or player fixtures mutate it. Mutable returned pools/bundles are deep copies.
Home still executes actual `home_prepared.prepare()` separately for every
`prepared` consumer. Direct `seed()` calls, initial/unpublished preparation,
changed-size seeds, races and file-backed cache/restart cases keep their genuine
construction. All original Home/refill/Radio test bodies are unchanged,
including the complete 110-subject Radio tail and live retuning assertions.

The initial exporter/restore implementation passed the four sampled cases but
took 238.24s, so it was replaced with private byte copies. Intermediate exporter
snapshot and equal-root setup errors also affected a concurrent diagnostic;
those versions do not establish acceptance of the current helper.

The current five-case cohort passed in 116.60s (Python 3.14.8 / pytest 9.1.1):

```sh
uv run pytest tests/test_prepared_fixture_support.py \
  tests/rec/test_home_prepared.py::test_preparation_is_bounded_complete_and_records_no_exposure \
  tests/rec/test_home_prepared.py::test_new_skip_filters_an_already_prepared_menu_without_changing_the_heart \
  tests/test_radio_intensity_priority.py::test_explicit_genre_mute_cannot_be_escaped_for_intensity \
  tests/test_radio_intensity_priority.py::test_indexed_genre_pin_follows_playback_instead_of_an_old_style_anchor \
  --durations=8 --tb=short --maxfail=2 \
  --junitxml=/tmp/tuneconsole-534-private-files-accepted.xml
```

| Unchanged sampled case | Before, seconds | Current, seconds |
| --- | ---: | ---: |
| Home initial preparation (includes module seed construction) | 45.719 | 51.645 |
| Home skip filtering (warm seed) | 31.106 | 7.498 |
| Radio genre mute (includes module seed construction) | 17.401 | 22.665 |
| Radio current genre pin (warm seed) | 16.736 | 2.132 |

The before cohort passed in 111.76s; its report is
`/tmp/tuneconsole-534-prepared-before.xml`. Current figures are JUnit per-case
totals, including setup/call/teardown. The extra clone-isolation case took
31.860s and is absent from the before inventory. It restores real six-mode
profile data into both memory and WAL consumers, removes the source profile
files, then verifies both consumers still read their independent files. It also
checks distinct namespaces, read-only source SQL, private SQL/nested-payload
mutations and foreign-key integrity. No speedup is inferred for the new check.
Concurrent machine work prevents treating the sampled timing difference as a
controlled benchmark or a whole-suite/hosted runtime result.

The complete modified modules are checked with this focused invocation:

```sh
uv run pytest tests/rec/test_home_prepared.py tests/rec/test_home_refill.py \
  tests/test_radio_intensity_priority.py tests/test_prepared_fixture_support.py \
  --durations=12 --tb=short --maxfail=2 \
  --junitxml=/tmp/tuneconsole-534-prepared-modules.xml
```

Final outcome: **103 passed in 1787.55s (29m47s)**, with one existing
Starlette/httpx deprecation warning. This is a serial focused cohort, not a
complete suite or hosted workflow result. The retained file-backed cache/restart
cases each spent about 120s in their test bodies; the unchanged Radio lookahead
case spent 115.24s in its body. Seed reuse does not eliminate these call costs.
All original test bodies, fresh construction cases and assertions remain.

Regular schema change 27 for #836 was added while this cohort was running.
`uv run pytest tests/test_prepared_fixture_support.py --tb=short --maxfail=1
--junitxml=/tmp/tuneconsole-534-ownership-schema-integration.xml` then renewed
clone isolation against that schema: **1 passed in 37.98s**, with the same
deprecation warning. The required complete hosted workflow at no more than
15 minutes remains unestablished. This fixture pass has run focused checks only,
without a full suite, commit or push.
