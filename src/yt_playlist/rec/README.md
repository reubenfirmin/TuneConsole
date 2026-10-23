# Recommendation application boundaries

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
and HTTP projections remain separate collaborators.

`home_cards.HomeCards` owns Home candidate assembly, lane and genre policy, pool
selection, offered counts, served impressions and diagnostic audits. Its request
and result dataclasses carry policy inputs and template-independent results. The
job port admits pool work only on an explicit start request; reading cards or
polling a pool does not start work. `web/routes/home.py` parses and validates HTTP
input, renders the results, and preserves the Home audit point after rendering.
Existing rankers, recipes and recommendation surfaces remain the policy engines.

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
create playlists through `library.executor`. These boundaries do not change
eligibility, pool budgets, duration fitting or the persisted draft format. Pool
exhaustion behavior, including the concern in #205, remains a separate behavior
change rather than an implicit part of this extraction.

Repositories own persistence queries and writes. They may import neutral core
definitions and detached data models, but must not import recommendation policy,
Store or web orchestration. Shared saved-setting definitions live in
`core.recommendation_settings`; runtime code does not import numbered migrations.
Historical migration definitions remain frozen.

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

`model_refresh` is the production training lifecycle. Enrichment queues source
changes for that lifecycle; it does not run an independent incremental placement
pipeline. The like-ratchet upgrade remains frozen in migration `v007_like_ratchet`.
Isolated model builders used by algorithm and saved-format tests live in
`tests/model_support.py`; they are not application entry points. Production
invalidation, equivalence and atomic-publication tests exercise `model_refresh`.
