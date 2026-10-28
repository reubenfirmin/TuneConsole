# Recommendation application boundaries

Monthly recaps use each account's retained listening calendar. `trend_statistics`
folds account dates into monthly totals, discovery, diversity and streaks.
`trend_payload` prepares the recap on a private snapshot reader; `recap_metrics`
and `recap_details` measure and select its optional detail screens. `trend_rollups`
reads quarantined inputs and publishes only `built_at` and `story`, together with
the first-play index, through the existing source-generation check. Expensive work
stays in background maintenance; a stale or failed publication preserves the prior
generation. Home owns recap viewed/dismissed state. The retired Trends charts,
weekly overlays, insight detectors, health payload and spotlight are no longer built.

Radio's genre dial has explicit endpoints. Fully Eclectic actively chooses a
different genre from the preceding song, favors genres heard less recently, and
uses genre-family distance to prefer broader changes when recency ties. Fully
Themed pins every continuation to the currently playing song's genre, including
when that song changes during a pending edit. Missing genre metadata cannot
satisfy a pin; an exhausted pinned genre does not spill into another genre.
The middle retains embedding similarity and local style transitions, with
progressive genre contrast or focus toward either end. Energy and familiarity
rank candidates within the endpoint's genre policy.

`radio_genres` owns genre identity and contrast independently of embeddings.
`radio_candidates` inspects existing prepared metadata across all taste regions
and hydrates at most four pools per endpoint pick; it never loads the library
matrix or fits a model during a pick. `radio_directions` passes observed playback
separately from its proposed future, so an unheard song cannot move a genre pin.
Moving the dial rebuilds the future without restarting playback, changing the
heard history, or permanently rejecting songs excluded by a temporary genre pin.

Radio's coordinator applies lifecycle transitions and policy results.
`radio_edits` permits one append or replacement operation plus a queued DJ
choice. Each operation holds at most one uncertain write. `radio_mutations`
owns their shared intent/receipt journal, transport, complete readback,
substitution, timeout and local publication protocol. A lost reply is reconciled
before another write is allowed. `radio_playlist_tail` plans suffix edits from
explicit playback and candidate views; it performs no I/O. `radio_policy`
receives candidate, history and feedback views instead of a web Session, and
returns feedback/exclusion results for the coordinator to apply.
`radio_persistence` retains the v2 checkpoint shape and migrates v1 flags.
Receipt journaling updates only the current episode's operation, preserving a
concurrent Stop and any newer queued choice. Playback observation/navigation
and HTTP projections remain separate collaborators. `radio_launch` owns seed
resolution, initial creation, settled initial readback and checkpoint attachment;
only the coordinator selects the live session and navigates after persistence.
`radio_listening` owns observed history, feedback application, visible-menu
attribution and ephemeral direction changes. `radio_preparation` returns prepared
candidate upgrades and detached listening samples; the coordinator rejects results
for stopped or replaced sessions before publication. These owners receive explicit
ports and session inputs, with no reference or callbacks to the whole controller.
`Session` applies confirmed mutation results and preserves heard positions and
choice credit when the server accepts an alternate upload.

`home_cards.HomeCards` owns Home candidate assembly, lane and genre policy, pool
selection, offered counts, served impressions and diagnostic audits. Its request
and result dataclasses carry policy inputs and template-independent results. The
job port admits pool work only on an explicit start request; reading cards or
polling a pool does not start work. `web/routes/home.py` composes the page and the
board, tuning, discovery, onboarding and generation route groups. `home_views`
shares request-cached prepared reads and fragment presentation across those groups,
including response headers and audit points. Recommendation policy stays outside HTTP.
Existing rankers, recipes and recommendation surfaces remain the policy engines.

Vibe selection consumes one prepared score per listening purpose. Background
`vibe_preparation` uses the recording's own captured excerpts and selected
provider measurements (including AcousticBrainz), then conservative predictions
from distinct measured recordings with the same canonical artist credit set and
specific subgenre, then artist-only or subgenre-only evidence. The original
musical heuristic fills missing evidence. Broad genres never form prediction
cohorts, and predictions never become measurement evidence. Home uses these
scores directly; radio's intensity control uses the greater prepared Energize
or Let off steam fit, so slow forceful music does not masquerade as chill.

Seven relations own this data: `sample_attributes` references immutable sample
JSON files; `measurements` retains model scores, linked through `measurement_samples`;
`predictions` retains projected scores, with `prediction_artists` for their exact
conditioning credits and `prediction_inputs` for original contributing measurements.
`recording_vibes` selects current scores for serving and indexed ranking. Musical
profile JSON contains dimensions, without duplicating these scores. Cohort
statistics are calculated from current original measurements in the background;
`rec_vibe_pending` pages affected recordings through the existing preparation worker.
New measurements, metadata/credit corrections and configured model
changes refresh dependencies without a full content-model fit. Support increases
with distinct recordings and agreement; one useful measurement already nudges a
prediction. Replays and multiple uploads do not add votes. Missing or incomplete
audio retains the provider/heuristic path, and last-good profiles remain readable
through preparation, restart and failed publication.

Migration 61 preserves prior scores and model versions. Legacy predictions have
`lineage_complete=0`: their old JSON did not retain exact contributor IDs, so the
migration does not invent them. Unknown historical measurement times are null.
New predictions retain complete measurement links. Exact-video reassociation moves
sample file references and retires obsolete measurement/sample links; original
calculations and historical prediction inputs keep their original ownership.

Human popup labels are **INTERNAL DEVELOPMENT ONLY**. They are targets for the
local trainer and evaluation, never product preferences, per-recording overrides,
runtime inference inputs or seeds for artist/subgenre predictions. The exported
JSON coefficients may use both base metadata and captured audio; their feature
normalization and regularization treat both sources equally.

`group_home` assembles Home Mix tapes from the last complete versioned graph
projection in `group_serving`; initial preparation and explicit rollback retain
the previous path. Each offer freezes its exact definition and hierarchy for
`group_learning`, and `group_viz` presents that same generation in Taste Model.
See [the adaptive grouping design](../../../ADAPTIVE_TASTE_GROUPS.md) for serving,
lineage, work budgets and isolated actual-library reproduction.

Initial setup offers verified library songs as soon as they arrive. Leaving setup
requires a completed import and usable prepared mixes; sparse accounts can receive
these offers before any taste modes are fitted. Empty imported accounts have no
library preparation to wait for. Metadata coverage is reported as counts rather
than a readiness percentage. The obsolete
`onboard_coverage_min` and `onboard_library_min` controls and their saved overrides
are retired. `onboard_feedback_min` still controls when the optional getting-started
suggestions end; it does not bypass prepared-offer readiness.

Road-trip work has three owners:

- `road_trip_candidates` acquires provider rows and metadata, reads eligible
  library candidates, and ranks the acquired pool. `InputRequest` and
  `CandidateBatch` describe one provider acquisition without changing a draft.
- `road_trip_mix.construct` accepts a fixed `MixRequest` and explicit random
  source, and returns allocation, ordering and shortfalls in `MixResult`. It has
  no provider, HTTP, repository or playlist-write dependency. Shortfalls remain
  visible while a draft is building and after final fallback fills.
- `road_trip` owns draft creation, incremental acquisition transitions, edits,
  normalization, quotas, rerolls and the persisted JSON shape. It applies mixer
  results to the draft and delegates searches to the acquisition owner.

The road-trip routes continue to persist drafts through `repos.road_trip` and
create playlists through `library.playlist_generated`. These boundaries do not change
eligibility, pool budgets, duration fitting or the persisted draft format. Pool
exhaustion behavior, including the concern in #205, remains a separate behavior
change rather than an implicit part of this extraction.

Repositories own persistence queries and writes. They may import neutral core
definitions and detached data models, but must not import recommendation policy,
Store or web orchestration. Shared saved-setting definitions live in
`core.recommendation_settings`; runtime code does not import numbered migrations.
Historical migration definitions remain frozen.

Recommendation reads have four explicit owners. `repos.rec_query` projects catalog
metadata, content inputs and library distributions. `repos.rec_evidence` reads
historical provenance and constructs current or cutoff training baskets on one
pinned snapshot. `repos.rec_eligibility` shares generated-playlist quarantine,
catch-all exclusion, feedback suppression and saved track-length rules.
`repos.rec_candidates` retrieves bounded, ranked playable library candidates;
`rec.surfaces` composes those sources and applies personalized lane policy.
Each owner binds directly to the serving Store or its private snapshot. Catalog
queries do not forward historical, eligibility or candidate methods.

Serving persistence has explicit owners too. `repos.rec_ledger` owns rendered
offers, responses, exposure counters, immutable serving policies and historical
attribution inputs. Its reserved `home_shown` proposal key only deduplicates
actual offers. `repos.rec_proposals` owns last-good prepared output;
`repos.playlist_recipes` owns saved recipes and Clusters canvases;
`repos.rec_signals` owns mood feedback, themes and graduation history; and
`repos.artist_similarity` owns the Last.fm cache and canonical similarity edges.
`rec.scorecard` calculates rates, clustered intervals and experiment eligibility
from detached ledger inputs after the private reporting snapshot is released.
Trace policies and outcomes are loaded on the same snapshot, preserving historical
ownership even after catalog changes. Repositories do not import scorecard policy.

Bundle preparation first releases its private reader and calls
`rec_proposals.stage_prepared_bundles`. Immutable UTF-8 chunks commit at most
64 KiB per transaction, remaining hidden behind the previous serving heads.
Identical retries resume their committed prefix; expired inactive builds are
removed in bounded transactions. The first cutover retains the legacy payload
without rewriting it. Direct legacy replacement or invalidation removes its head.
Discovery pruning and policy retention read the active payloads.

Bundle publication calls `rec_ledger.publish_prepared_policies`,
`rec_proposals.publish_prepared_bundles` and `settings.publish_values` in one
`ReadSnapshot.publish` transaction. Only small cache references are switched in
this transaction; none of these methods commits independently. Failure or stale
inputs retain both last-good proposal sets and prevent partial policy publication.

`model_refresh` prepares serialized model results on a private snapshot without
constructing SQL. Publication calls each persistence owner inside one
`ReadSnapshot.publish` transaction: `rec_model` applies changes to its fixed vector spaces,
`settings` writes saved values, `modes` publishes stable mode identities, and
`rec_refresh` acknowledges the captured notification revision. These operations
require the existing transaction and do not commit individually or accept
arbitrary SQL. A stale snapshot publishes nothing; a failure in any owner rolls
back the complete generation and leaves the notification pending. Fitting and
ranking finish before the publication transaction begins.

Playback ticks, ratings telemetry and pause events remain stored without queuing
model refreshes. Migration v040 narrows the durable player-event notifications to
track exits, disconnects and ended events. Skip queries track those event kinds
separately, so telemetry cannot invalidate an unrelated ranking snapshot. Expired
candidate pools request ranking only. Superseded model and pool calculations keep
their retry intent with increasing delays capped at sixty seconds; ratings and
other serving work continue during that delay. Failed calculations retain their
separate failure backoff and the last published results.

The vector repository compares serialized bytes on the pinned reader and prepares
upserts and removals before taking the serving lock. Identical rows remain intact,
including their revision counters. Reading each requested output space registers it
as a dependency, so a competing publisher invalidates the prepared changes. Library
and artist vector loaders use their fitted key order. Discovery vectors retain their
encoder's source order: if a delta would change that order, only that space falls
back to replacement. Partial publication therefore preserves full replacement's
ordering and tied scores.

Every file-backed `ReadSnapshot.publish` temporarily defers SQLite's automatic WAL
checkpoint, including Trends, cluster bundles, Home projections and model updates.
The atomic commit retains the configured synchronous policy and revision triggers.
After restoring the original checkpoint policy and releasing the serving lock, a
private connection performs a best-effort PASSIVE checkpoint. Busy readers or writers
can leave WAL pages for a later checkpoint. Checkpoint failure cannot turn an already
committed generation into a failed publication. This is no hard latency bound: durable
commit still holds the serving lock, and a later foreground commit can checkpoint if
it wins the race with the private connection. There is no durability reduction.
Stale, cancelled, rolled-back and unchanged publications do not request a checkpoint;
an explicitly disabled automatic checkpoint policy remains disabled.

Publication diagnostics report wall and thread CPU time separately for serving-lock
acquisition, lock ownership, writer reservation, revision capture, writes, commit and
checkpoint. Lock ownership of at least 100 ms is reported at INFO; faster publications
remain at DEBUG even when their checkpoint takes longer. Revision-trigger work during
writes is included in the write stage. These timings distinguish a slow durable commit
from checkpoint work and from foreground callers waiting for the shared connection.

`tests/bench_svd_background.py` provides complete, partial and unchanged refresh probes
with stage wall/CPU times, serving-lock holds, memory, event-loop lag and concurrent
Home/database latency. Its matched publication comparison alternates the old atomic
replacement and prepared changes with identical payloads, separating revision capture,
vector writes, commit and checkpoint. Review measurements on 2026-10-23 used 22,378
saved vector rows across five spaces and 13 modes, with all writes on disposable
backups. Alternating matched full-payload trials held the serving lock for 862–876 ms
with atomic replacement and 209–212 ms with prepared changes and deferred checkpointing.
The latter spent 127–128 ms writing vectors and 80–84 ms committing; its 372–374 ms
checkpoint ran outside the lock. A one-row change took 2–5 ms under the lock, compared
with 90–95 ms for replacement; identical vectors took 2–4 ms rather than 891–951 ms.

The saved-library regression target is less than half the matched replacement lock
duration for a full changed payload, with no vector writes for unchanged inputs and
only changed rows written for an ordinary partial refresh. This target compares the
same payload on the same machine; it is not a general foreground latency guarantee.
An actual complete-generation probe held the lock for 58 ms, with maximum Home/database
latencies of 125/52 ms and event-loop lag of 15 ms. Most fitted bytes were unchanged in
that probe, so the matched full-change trials remain the stronger publication stress
comparison. Durable commit and a racing foreground checkpoint can still limit latency.
The earlier 250 ms full-generation threshold was not an agreed requirement.

Those measurements covered vector generations. They do not establish latency for
ordinary projections: later live logs showed 318–411 ms commits from Trends and cluster
bundles, which had still used inline checkpoints. The shared publication boundary now
covers them. `test_compare_saved_library_projection_publications` alternates inline and
deferred checkpointing for both production builders, using fresh disposable copies of
one frozen library and the same calculation time. It reports wall/CPU stages, memory,
event-loop lag, Home/database latency and serving-lock holds. Review execution on
2026-10-23 passed both cases, with four alternating trials each on a 6,143-track library.
For bundles, the serving lock fell from 457–599 ms inline to 250–259 ms deferred;
commit fell from 384–532 ms to 184–191 ms, with 293–294 ms of checkpoint work outside
the lock. Maximum concurrent Home/database latency fell from 529–649/447–598 ms to
317–322/243–259 ms. Whole-builder cost remained 18.35–18.58 s wall and 14.61–14.70 s
thread CPU, with 339–348 MiB peak RSS and 92–114 ms maximum event-loop lag across all
trials. This change reduces publication contention, not the underlying fitting cost.
Trends showed no consistent latency improvement: inline/deferred lock holds were
48/34–49 ms, with Home maxima of 56–66/48–70 ms and database maxima of 43–45/32–39 ms.
Its builders took 0.80–0.84 s wall and 0.63–0.66 s thread CPU, with 170–173 MiB peak RSS
and 9–16 ms maximum event-loop lag. These are matched observations, not a new hard
latency guarantee for ordinary projections.

`tests/test_architecture.py` enforces import direction, migration separation,
SQL-free preparation and publication ownership. Model refresh and repository
publication tests cover model equivalence, stale inputs, coalesced notifications
and rollback after a late publication failure. These boundaries preserve the SVD
and item2vec implementations and ranking policy.

`model_refresh` retains the complete collaborative and artist training lifecycle.
`progressive.Preparation` also captures changed recordings in durable slices and
fits the established content encoder and taste clusters from accepted inputs.
An empty catalog waits for evidence; a sparse catalog can publish a content vector
without waiting for providers or the clustering minimum; growing catalogs expand
their content space and clusters while the previous results remain available.
Each captured batch re-encodes only its recordings against the exact published
basis, including its audio normalization. A changed basis replaces both content
spaces and its reconciled modes atomically. Catalog writes during this fit only
queue later capture; prepared-input or competing-model writes still reject it.
No stale-source exception is used for model publication.

Capture admits at most 32 recordings every five seconds, within two seconds of
cooperative work. Hot batches halve and rotate behind other work. Basis fitting
has a ten-second cooperative budget, a 32,768-row limit, a 128 MiB working-set
estimate and a one-minute interval. Native operations finish before the next
cooperative checkpoint. A basis needs ten percent changed recordings, capped at
128 and with a one-recording floor for small catalogs. A complete model fit needs
four explicit evidence changes or ten percent changed recordings (floor four,
cap 128), or changed model settings. Both stages admit smaller aged changes after
15 minutes. Complete fits retain the worker's one-minute admission interval and
have a twenty-second cooperative budget and a 512 MiB working-set estimate checked
before collaborative matrix construction and content encoding. Deferred work keeps its durable inputs
and last published output; retries continue even after capture drains its queue.
These are resource admission limits, not a claim that the entire catalog is ready.
The encoder fills one preallocated float32 matrix; clustering reuses it and keeps
its existing float64 arithmetic, distance chunks, seed and ordering. Decoded
inputs are released before clustering. Admission estimates the encoding,
clustering and publication phases separately, including cluster-member copies
and old serialized vectors. The 128 MiB estimate covers admitted working data,
not the application's total RSS.

The same preparation worker audits missing, unqueued inputs at startup and every
15 minutes, requeueing at most 32 keys per guarded slice. Existing queue revisions,
positions and accepted inputs are preserved. Memory/row-limit failures, and time
limits repeated three times, retry every 15 minutes without requiring new writes.
Other transient fit failures retain the one-minute retry interval. Fit status and
retry deadlines survive restart. Model Health polls a cheap saved-status endpoint
and warns when these limits still prevent progress; successful basis publication
clears the warning atomically. A retry cannot guarantee that an arbitrarily large
library fits the unchanged limits. Input publication and basis completion have
separate diagnostic phases so catch-up cannot hide a blocked fit.

The worker also retains up to 64 MiB of detached collaborative fits across deferred
publications. A retry reads its baskets and weights again and reuses a fit only
when their exact input fingerprint matches. This preserves SVD and SGNS objectives,
account/recording keys and strict publication guards. Accepted content inputs survive
restart; the bounded fit cache is disposable and is rebuilt after restart.

`Preparation.status()` exposes pending/prepared counts, published vector counts,
fit checkpoints, input fingerprints, estimated bytes and budgets. Bounded
maintenance summaries distinguish initial/accumulated/aged evidence, hot inputs,
bootstrap coverage, memory limits and time limits. Listener setup copy does not
expose these diagnostics. The like-ratchet upgrade remains frozen in migration `v007_like_ratchet`.
Isolated model builders used by algorithm and saved-format tests live in
`tests/model_support.py`; they are not application entry points. Production
invalidation, equivalence and atomic-publication tests exercise `model_refresh`.

Embedding calculations have explicit input and result contracts. `embedding_training`
fits sparse collaborative models from detached baskets and optional play-count gains,
returning `FittedVectors`. `content_model` fits the shared genre/audio vocabulary from
`ContentInputs`, encodes library and discovery rows, and owns persisted model identity.
`embedding_vectors` reads and decodes committed generations as `VectorSpace` values;
its discovery re-encoding retains the generation compare-and-publish check. `embed`
owns neighbor retrieval, connection geometry and cluster expansion. Its `expand_spaces`
calculation receives detached spaces and explicit blend/seed controls. Track and artist
models share normalization and percentile blending through `vector_math`. Request
adapters read existing vectors; fitting and complete publication remain in the worker's
snapshot lifecycle. These boundaries preserve key ordering, empty-space sentinels,
float32 output, sparse accumulation, deterministic random seeds and cancellation.
