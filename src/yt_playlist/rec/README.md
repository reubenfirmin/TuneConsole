# Recommendations

Ranking and model fitting run locally on CPU. Requests read prepared output;
background workers fit models and publish complete generations. Failed or stale
work preserves the last usable output.

## Owners

| Area | Owner |
| --- | --- |
| Collaborative fitting | `embedding_training`: detached baskets and play-count gains into `FittedVectors` |
| Content fitting | `content_model`: genre/audio vocabulary, normalization and model identity |
| Committed vector reads | `embedding_vectors`: ordered `VectorSpace` values and guarded discovery re-encoding |
| Neighbors and graph expansion | `embed`; shared normalization/blending in `vector_math` |
| Taste | `scoring`, `transient`, `layers`; [listening-choice contract](../../../LISTENING_CHOICES_DESIGN.md) |
| Home cards | `home_cards.HomeCards`: lanes, pools, offers, impressions and audits |
| Home mix cache | `home_cache`: paths, compression and file lifecycle before atomic SQL head publication; `repos.home_cache`: SQL metadata and references |
| Group mixes | `group_home`, `group_serving`, `group_learning`, `group_viz`; [group contract](../../../ADAPTIVE_TASTE_GROUPS.md) |
| Recaps | `trend_statistics`, `trend_payload`, `recap_metrics`, `recap_details`, `trend_rollups` |
| Response metrics | `scorecard`: detached ledger inputs, clustered intervals and experiment eligibility |
| Model publication | `model_refresh` through persistence owners in one guarded transaction |

Routes compose pages and controls. They do not own recommendation policy.
Repositories own queries and writes; they do not import recommendation policy,
Store or web orchestration. Shared settings live in `core.recommendation_settings`.

Home mix cache writes use FAO compression and atomic replacement without fsyncs
or write-time rereading. The service writes files before reserving the database
writer, then switches publication heads and evidence references together. Its
existing file-lifetime lease prevents orphan collection between those steps.
Readers capture SQL descriptors before opening files outside the repository.

## Radio

The coordinator applies lifecycle transitions and policy results through explicit
ports. Collaborators do not receive the whole controller.

| Owner | Responsibility |
| --- | --- |
| `radio_genres`, `radio_candidates`, `radio_directions` | Genre policy, bounded prepared pools and observed playback direction |
| `radio_launch` | Seed resolution, initial playlist creation/readback and checkpoint attachment |
| `radio_edits` | One append/replacement operation plus a queued DJ choice |
| `radio_mutations` | Intent/receipt journal, transport, readback, timeout and reconciliation |
| `radio_playlist_tail`, `radio_policy` | Detached edit/feedback calculations without I/O |
| `radio_listening` | Observed history, feedback and visible-menu attribution |
| `radio_preparation` | Candidate upgrades and detached samples; reject stopped/replaced sessions |
| `radio_persistence` | Checkpoints, preserving concurrent Stop and newer choices |

A lost remote reply is reconciled before another mutation. Only confirmed uploads
enter applied state; incomplete reads cannot authorize replay. The coordinator
selects the live session and navigates after persistence.

Fully Eclectic changes genre and favors less-recently heard genres. Fully Themed
pins to the actually playing song's genre; missing metadata or an exhausted genre
cannot satisfy that pin. Intermediate settings blend local similarity and genre
contrast. Energy and familiarity rank within that policy. Tuning changes the
future queue without restarting playback or turning temporary exclusions into dislikes.

[Radio stream planning](radio-stream-planning.md) describes the four continuation
hypotheses, shared candidate facts, Apply lifecycle and performance measurements.

## Vibes

`vibe_requests` saves each explicit selection and runs one generation thread
outside the HTTP request lifetime, without an overall generation deadline.
New selections supersede pending work; only the current request may switch
cache heads. Incomplete preparation keeps the previous complete board.
The existing Vibe control observes the saved request with GET requests, including
after reload or restart, and never replays a selection POST to recover a lost reply.
Completed mixes wait until the listener closes an open mix before replacing it.

`vibe_preparation` uses direct capture/provider measurements, then conservative
predictions from distinct measured recordings with matching canonical credits and
specific subgenres. Broad genres do not form prediction cohorts. Predictions and
repeated uploads never add measurement votes; missing evidence uses the heuristic.

The inactive joint candidate lives in `vibe_joint_model` and
`vibe_joint_calibrated`; `vibe_joint_preparation` and `progressive` own bounded
background preparation. Product code never imports experiments. Its qualification
mapping remains empty until the recorded statistical and lifecycle gates pass.
Shared generation storage, uncertainty and compatibility are specified by the
[FAO owner contract](../../../experiments/retained_copy/FAO-OWNERS.md#shared-vibe-model-candidate-819).

The shared Emotify model supplies classification. Six independent specialists
supply within-vibe suitability. Their scores are not comparable probabilities
and never enter a mixed six-way maximum. Radio intensity retains the shared
Energize/Release channels. Human popup labels are development targets, excluded
from runtime preferences, inference and propagation.

[Model provenance](../../../experiments/audio_lab/README.md#model-provenance)
owns recipes, training inputs, model settings, adoption decisions and rejected
alternatives. Optional-model failure retains that vibe's shared score and other
working specialists. Model identity changes queue rescoring without audio
extraction or fitting.

| Relation | Purpose |
| --- | --- |
| `sample_attributes` | Immutable sample-file references |
| `measurements`, `measurement_samples` | Original scores and sampled inputs |
| `predictions`, `prediction_artists`, `prediction_inputs` | Predictions, conditioning credits and original contributors |
| `recording_vibes` | Current serving scores and indexed ranking |

Legacy incomplete lineage remains explicit. Exact-video reassociation changes
current sample links without rewriting historical prediction ownership.

## Road Trip and setup

`road_trip_candidates` acquires and ranks eligible candidates. The pure
`road_trip_mix.construct` accepts a fixed request and random source, returning
allocation, ordering and shortfalls. `road_trip` owns drafts, incremental work,
edits, quotas and persistence shape. Routes persist through `repos.road_trip`
and create playlists through `library.playlist_generated`.

Setup offers usable songs and prepared mixes/albums as they become available;
fitted taste modes are not a universal prerequisite. Feedback alone does not
complete setup. Metadata coverage is a count, not a readiness percentage.

## Persistence and publication

| Repository | Owns |
| --- | --- |
| `rec_query` | Catalog metadata, content inputs and distributions |
| `rec_evidence` | Historical provenance and cutoff training baskets |
| `rec_eligibility` | Generated-play quarantine, exclusions, feedback and length rules |
| `rec_candidates` | Bounded ranked playable candidates |
| `rec_ledger` | Offers, responses, deduplication, immutable policies and attribution |
| `rec_proposals` | Last-good prepared outputs and staged bundles |
| `playlist_recipes` | Saved recipes and Clusters canvases |
| `rec_signals` | Mood feedback, themes and graduation |
| `artist_similarity` | Last.fm cache and canonical similarity edges |

Fitting, ranking, serialization and file preparation finish outside the serving
transaction. Bundle chunks stage in bounded commits before their heads change.
One `ReadSnapshot.publish` transaction switches policies, bundles/settings or
model/vector/mode generations and acknowledges the consumed revision. Persistence
methods join that transaction; they never commit independently. A late failure
rolls back the whole publication. Changed dependencies reject stale model fits.
Recommendation pools finish and publish their captured snapshot while inputs
advance. They retain the policy actually used; serving applies current eligibility.

Vector deltas compare serialized bytes on the pinned reader. Unchanged rows keep
their revisions; changed spaces preserve fitted key order and tied scores.
Discovery order changes require replacing that space.

File-backed publication defers automatic WAL checkpointing until after commit
and serving-lock release. A private connection then attempts PASSIVE checkpointing.
Busy checkpoints retry later; checkpoint failure cannot undo a committed generation.
Commit retains its durability policy and can still hold the lock. Diagnostics
separate admission, writes, commit and checkpoint wall/CPU time.

## Preparation limits

`progressive.Preparation` persists changed inputs and publishes content spaces
and reconciled modes atomically. Catalog changes queue later input capture;
prepared-input or competing-model changes reject stale fits.

| Work | Admission budget |
| --- | --- |
| Capture | Up to 32 recordings per five seconds; two-second cooperative budget |
| Basis fit | Ten seconds, 32,768 rows, 128 MiB estimate; at most once per minute |
| Complete model fit | Twenty seconds, 512 MiB estimate; at most once per minute |
| Recommendation pool round | At most one start per 60 minutes, shared by worker and discovery jobs |
| Detached collaborative fit cache | 64 MiB; reuse only with an exact input fingerprint |
| Missing-input audit | Startup and every 15 minutes, at most 32 keys per slice |

Basis fitting normally needs 10% changed inputs, capped at 128 with a one-row
floor. Complete fitting needs four evidence changes or 10% changed inputs
(floor four, cap 128), or changed settings. Aged changes admit after 15 minutes.
Memory/row limits and three repeated time limits defer fitting for 15 minutes;
other transient failures retry after a minute. Deferred work retains inputs,
checkpoints and last-good output. Estimates cover working data, not total RSS;
native operations can finish after a cooperative deadline.

Playback telemetry does not queue model fits. Track exits, disconnects and
ended events retain their distinct signals. Candidate expiry requests ranking
only. Hourly checks compare preferences, relevant Home/mode impressions and
decay with the last captured round. An unchanged account skips ranking. Updates
during a round belong to the next check; they never cancel or restart it. The
check timestamp survives restart, and an active round cannot overlap another.

## Verification

`tests/test_architecture.py` checks import direction, SQL-free preparation and
publication ownership. Focused model/repository tests cover stale inputs,
equivalence, notifications and rollback. `tests/bench_svd_background.py` measures
full, partial and unchanged refreshes, projection publication and foreground
availability using isolated library copies. It retains the matched full-payload
target of less than half the replacement lock duration; historical timings are
not current latency guarantees.

See [test commands](../../../tests/README.md),
[database maintenance](../core/README.md) and the
[Charts contract](../../../LISTENING_ANALYTICS_DESIGN.md).
