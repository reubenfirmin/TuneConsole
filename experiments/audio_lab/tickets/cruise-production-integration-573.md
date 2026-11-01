Parent: #530. Priority: **P2**. Related: #136, #571 and #572.

The user explicitly authorized production integration outside `experiments/` on 29 October 2026. This supersedes the earlier research-only restriction. Productionize the validated individual Cruise model in the backend; retain the best validated shared model for the other five vibes and overall classification.

Cruise is mainlined directly, active by default. There is no shadow-rollout or staged-promotion requirement. #574 was an unrequested extra step and is cancelled.

Implemented in the working tree:

- Production coefficient loader/inference in `rec/audio_vibe_cruise.py`, with the exact reviewed compact artifact bundled in `rec/data/cruise-vibes.json`.
- Separate selected-vibe `suitability` scores in `audio_vibe_model`, consumed by `vibe_preparation`. Cruise never renormalizes the five shared scores or enters their six-way classifier, threshold or shared intensity calculation.
- Preparation identity includes both artifact hashes, routing mode and aggregation-policy version. Existing compatible captures rescore through the normal bounded queue; original measurements remain the only cohort contributions. No extraction or model fitting occurs at runtime.
- Active by default, with explicit shadow/off controls and safe optional-model fallback. Rollback invalidates prepared results through the same queue.
- Research tools import the production scorer; the installed package has no dependency on experiment files. Historical artifacts and receipts remain preserved.

Author evidence:

- 58 focused production/preparation checks pass. Controlled fixtures cover fresh state, incomplete captures, missing metadata, malformed/incompatible optional artifacts, batch=1 rollout, continuing captures, one vote per recording, propagated updates, model replacement, rollback and preserved human feedback.
- A new isolated database reconstructed from 93 real playback groups contains 88 actual recordings. All 77 complete compatible recordings receive the new Cruise fit. 186 first/all-excerpt preview cases preserve the five other scores and shared classifier exactly. Normal production budgets converge; Cruise rollout takes 2.18 seconds of work, longest capture slice 0.42 seconds. Rollback restores every fit exactly. Home affinity and shared radio intensity readers agree. No labels or stored predictions are evaluation truth.
- An offline-built wheel includes the exact production module and both JSON artifacts. A fresh isolated process loads the installed Cruise artifact and scores an embedding without importing experiments.

Remaining acceptance work:

- Replay a read-only backup of the entire actual account, including provider metadata, collaborative state and prepared pools, through cold, partly prepared and continuing-update states. Never run migrations or acceptance checks against the live database.
- Exercise complete Home/Mix and Radio outputs after current schema/publication integration failures are resolved. The bounded captured-audio cohort and affinity reader checks do not establish rendered surfaces or full-account startup.
- Record exact runtime/resource results, current score/fallback coverage and immutable contributor lineage. Preserve usable output during paginated rescoring.
- Keep the remaining account, consumer and resource verification in this ticket. #405 and #424 remain open for their genuinely unfinished evaluation requirements; reference shared evidence instead of duplicating these checks. They add no shadow-rollout or staged-promotion requirement. No calibrated strong-match threshold is asserted by this ranking-only route.

Evidence and reproduction: `experiments/audio_lab/CRUISE_PRODUCTIONIZATION.md`; `cruise-production-2/report.json` and `cruise-wheel-1/report.json` under `experiments/audio_lab/local/mapped-vibes-20261029/`.

Broader focused regression run: 91 passed, 9 failed. One legacy metadata fixture writes the now read-only `entry_field_choices` view (#500); four Home fixtures fail because a referenced candidate policy is absent (#487); four architecture checks flag current playback-feedback imports, repository policy imports, SQL in model refresh and unguarded repository connection access (#482). These are outstanding integration failures, not Cruise numerical failures or a green suite. Exact commands and failures are recorded in the handoff comment.

Acceptance remains with independent review. No commit, push, live-database experiment write or running-app reload is claimed.
