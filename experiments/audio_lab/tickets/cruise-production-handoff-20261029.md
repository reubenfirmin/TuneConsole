Cruise has now moved into production code and the bundled package, as explicitly requested. The runtime is `src/yt_playlist/rec/audio_vibe_cruise.py`; the bundled compact artifact is unchanged from package 3, SHA256 `3927a3294ad7a1670ef7f81d5c6119943991a699de60a7d5a60022851ba304eb`. The shared artifact remains `5351572e6eb1bc17f80a6519b9b3f1b8461775726591aa847077a4fdb55732a0`.

The backend defaults Cruise to active for compatible captures. `audio_vibe_model` supplies a separate suitability map; `vibe_preparation` publishes individual Cruise plus five exact shared scores. Shared classification, thresholds, margins and Energize/Release intensity remain on the shared model. The existing bounded queue handles artifact changes, mode changes and rollback. The extension continues sending the same captured features. Disabling/missing/invalid/incompatible Cruise retains the shared result.

Author commands/results:

```
uv run pytest --test-lock-wait=60 tests/test_audio_vibe_cruise.py tests/test_audio_vibe_external.py tests/rec/test_vibe_prediction.py
# 58 passed in 19.69s; controlled numeric fixtures, real production storage/worker/consumers.

./experiments/run.py cruise-production --directory experiments/audio_lab/local/mapped-vibes-20261029/cruise-production-2
# Passed: 93 real playback groups; 186 partial/available inference cases;
# 88 reconstructed actual recordings, 77 complete compatible measurements.
# All 77 Cruise fits changed; all five other fits/classifications preserved.
# Normal-budget rollout 2.18s, longest capture slice 0.42s; exact rollback.
# All product source hashes unchanged during replay; no human outcomes scored.

./experiments/run.py cruise-wheel --directory experiments/audio_lab/local/mapped-vibes-20261029/cruise-wheel-1
# Passed: offline wheel build; both bundled artifacts and scorer match source;
# fresh isolated process imports installed wheel, loads/scores Cruise,
# no experiment imports. Wheel SHA256:
# 68386820b4e5c8feb2feb4ad45c52dfd6703381f52ec7067235878d9469f83f7

./experiments/run.py check --test test_cruise_specialist.py
# 28 passed in 1.24s against the moved production loader/scorer.

uv run pytest --test-lock-wait=60 tests/test_audio_vibe_model.py tests/test_audio_vibe_embeddings.py tests/test_home_vibe.py tests/test_architecture.py
# 91 passed / 9 failed in 131.10s; details below.
```

Broader current-working-tree failures:

- `tests/test_audio_vibe_model.py::test_exact_catalog_metadata_joins_readonly_training_and_runtime_and_freezes_answer`: line 438 inserts into retired `entry_field_choices`; SQLite rejects writes to the view. Related #500. This exercises a legacy local correction, not the individual Cruise route.
- `tests/test_home_vibe.py::{test_vibe_changes_offered_styles_as_well_as_track_order,test_vibe_changes_rendered_tracks_persists_and_expires_without_learning,test_genre_requests_restrict_vibes_and_survive_clear,test_clearing_the_only_vibe_clears_tuning_indicator_and_deadline}`: `bundle_storage.resolve_family` rejects missing candidate policy `f7c99ab3a7586bb6` during fixture publication, before rendered Home assertions. Related #487. Shared inference is disabled by the ordinary test fixture.
- `tests/test_architecture.py::test_application_import_graph_is_acyclic_including_local_imports`: `core.playback_feedback` / `library.live_plays` import cycle.
- `tests/test_architecture.py::test_persistence_and_playlist_primitives_do_not_import_their_consumers`: `repos.rec_proposals` imports `rec.group_roots`.
- `tests/test_architecture.py::test_prepared_recommendation_policy_contains_no_connection_access_or_sql`: `rec.model_refresh` accesses `reader.conn`.
- `tests/test_architecture.py::test_public_repository_sql_methods_use_a_connection_guard`: `Repo.entry_artists_table` and `ListeningAnalyticsRepo.catalog_rows` are flagged. Architecture findings relate to the concurrent schema work under #482.

Runtime: Python 3.14.8 / NumPy 2.4.6 / SciPy 1.18.1 / threadpoolctl 3.7.0. Successful replay and build use the normal machine lock and one numeric thread. The first replay harness failure is preserved: a floating capture duration was passed to strict integral catalog metadata; the corrected harness omits that optional field and preserves original captured JSON.

The actual-capture cohort has no saved provider/collaborative history and is not the entire account. Full-account cold/update preparation and complete rendered Mix/Radio verification continue under #573, with existing listening/resource work in #405/#406. Cruise is mainlined directly and active by default; #574's unrequested shadow-rollout process is cancelled. No commit or push, full-suite pass, running-app reload or independent review acceptance is claimed. Configuration and detailed evidence: `experiments/audio_lab/CRUISE_PRODUCTIONIZATION.md` and `src/yt_playlist/rec/README.md`.
