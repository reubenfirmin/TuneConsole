# Product baseline comparison and rollout plan

29 October 2026, #530. All changes and generated evidence remain under
`experiments/`; production code and the live database are unchanged.

## What was measured

`./experiments/run.py product-baseline` replays the exact configured production
artifact, including both regression stages, clipping and six-vibe normalization.
Its SHA256 is
`5351572e6eb1bc17f80a6519b9b3f1b8461775726591aa847077a4fdb55732a0`.
The artifact reports 400 native Emotify training songs and 55 locally labeled
training songs. The refitted Emotify expert in the preceding research report
was a different comparator.

Evidence is in `local/mapped-vibes-20261029/product-baseline-1/`:
`plan.json`, the copied `product-model.json`, `inventory.json` and `report.json`.
The report records input and prediction hashes and retains both all-row and
known-product-training-disjoint slices. The table uses the latter. Artist,
recording and audio matches exclude whole target identity components; weaker
title matches exclude individual rows. Unknown aliases, missing artists and
upstream backbone training overlap remain unresolved.

Scores are tie-aware graded NDCG, higher is better, averaged across the existing
five validation folds. Each fold evaluates its top ten recordings, or its top
20% if smaller. Differences are NDCG points, not percentage-point accuracy.
The candidate heads, fixed mappings and equal VA ensemble were chosen before
this product comparison. There is no fitting or target-dependent recalibration.

| Vibe and reference cohort | Rows | Production | Candidate | Difference |
| --- | ---: | ---: | ---: | ---: |
| Cruise, natural ManyMusic | 186 | 0.6669 | 0.8358 | +0.1689 |
| Cruise, generated ManyMusic | 95 | 0.7022 | 0.8943 | +0.1921 |
| Cruise, Memo2496 pilot | 63 | 0.8156 | 0.8646 | +0.0491 |
| Focus, CAL500 Reading/Studying | 450 | 0.4279 | 0.4453 | +0.0173 |
| Release, Soundtracks anger, Emotify power | 470 | 0.6217 | 0.6502 | +0.0285 |
| Release, Soundtracks anger, Jamendo aggressive | 470 | 0.6217 | 0.6452 | +0.0236 |
| Energize, natural ManyMusic | 186 | 0.7960 | 0.7575 | -0.0385 |
| Energize, generated ManyMusic | 95 | 0.9187 | 0.9305 | +0.0118 |
| Energize, Memo2496 pilot | 63 | 0.9160 | 0.8701 | -0.0459 |

Cruise and Energize pilot candidates are the predeclared equal PMEmo/DEAM VA
ensemble. Focus uses the prior independent native Reading and Studying heads,
with grouped out-of-fold predictions; 44 known training-identity matches are
excluded here. The other table rows are source-to-target transfers. Two known
product-training matches are excluded from natural ManyMusic. These cohorts
were previously inspected and are not untouched confirmation data. No paired
uncertainty interval against production has been established by this audit.

On soundtrack Energize, production scores 0.8489, versus 0.8535 for refitted
Emotify, 0.8192 for DEAM and 0.5992 for the source-selected acoustic PMEmo model.
This does not support replacing production Energize with the VA ensemble or
preferring acoustic-only activation. Release also has mixed evidence: the two
candidate NDCG improvements accompany slightly lower top-list lift than
production. Relax and Uplift were not included in this bounded product audit.

The audit checks the vectorized product replay against the actual product
module's scalar stages on three recordings per cohort, with absolute tolerance
1e-12. Production uses mean embeddings before prediction; the pilot candidate
uses the declared mean native prediction before mapping. Both receive the same
recording's stored interval embeddings. Scores from the normalized production
vector and independent candidates have different meanings, so lower raw MSE
alone would not establish calibrated product suitability.

## The enlarged local corpus

The read-only inventory finds **84 usable human-labeled recordings**, all with
embeddings compatible with the configured product model. The previous protected
identity snapshot contains 76 recording IDs; eight compatible identities are
additional. These counts describe usable recordings, not all annotation rows.

The eight additions have not been used for model fitting, model selection or
quality scoring in this audit. Reserve them before further local tuning, then
check artist/recording components and prior weak-training exposure before
calling them independent holdouts. Changed labels on previously known IDs are
not measured by this identity-count comparison. External human annotations
remain the primary training and evaluation reference; local labels provide a
supplementary check against the product's actual use.

The current product was trained on 55 local songs. A fair next comparison should
include a refreshed version of the existing architecture using eligible older
labels, leaving reserved groups out, alongside the frozen product artifact.
That comparison was not part of this initial audit. The subsequent
[refreshed-baseline experiment](CRUISE_BASELINE_RESULTS.md) now reports both
controls, their identity exclusions and the reserved supplementary check.
The table above continues to describe the original frozen product comparator.

## Productionization

1. Mainline Cruise first. Package the equal PMEmo/DEAM native attribute heads and
   the fixed Cruise mapping. Retain the validated shared model for Focus,
   Release, Energize, Relax and Uplift pending their own replacement evidence.
   Preserve the declared per-excerpt aggregation in the shipped implementation.

2. Add a versioned per-vibe model format and router with explicit feature order,
   embedding signature, transforms, model identity, bounded dimensions and
   artifact hashes. The present loader accepts older formats and caps JSON at
   256 KiB; the full six-scorer research export is about 3.87 MB. Export only
   selected heads, measure size and latency, and validate any compression or
   compact replacement before changing coefficients or learner families.
   Reuse the current audio encoder and stored compatible measurements.

3. Keep suitability-score semantics separate from shared classification and
   thresholds. Independent vibe scores must not be normalized to sum to
   one. Replacing Cruise must not rescale the other five scores. Audit consumers
   that choose one best vibe or apply the existing 0.65 threshold; an independent
   score cannot simply be inserted into those decisions without validation.
   Freeze calibration on external training folds and check transfer, with the
   reserved local labels as supplementary evidence. Require a useful gain
   against both frozen and refreshed baselines before promotion.

4. Integrate with the existing model-identity preparation version and bounded
   background queue. Re-score compatible captures without recapturing audio;
   acquire new measurements only when missing or incompatible. Rebuild derived
   recording scores and propagated results from original measurements. Stored
   predictions are operational inputs or explicit weak supervision, never new
   human labels or independent evidence. Preserve per-vibe fallback and an
   artifact rollback path.

5. Validate on an isolated copy of the actual library: fresh startup, one partial
   capture, missing metadata, incompatible embeddings, ongoing captures, stale
   cache invalidation, propagation and Mix Radio under the normal preparation
   time limit. Existing extended-time replay is not startup acceptance. Check
   browser-produced feature compatibility, bounded resource use, dataset release
   permissions and listening quality. These are correctness and regression
   checks for the mainline implementation; there is no shadow phase or staged
   enablement decision.

Cruise is now integrated into the production backend and active by default.
This records source integration and local verification; it does not claim that
the running application has been reloaded or a release published.

The current implementation round advances **Cruise only**. Focus, Release,
Energize, Relax and Uplift retain the validated shared scorer; their individual
model investigations are in Backlog. See [Cruise production preparation](CRUISE_PRODUCTIONIZATION.md)
for the compact artifact, routing contract, real-capture replay and remaining
integration work.

## Next-round tracking

All subtasks belong to #530 and have priority P2. Cruise is the first mainline
individual model; the other five vibes have explicit model investigations.
#574 was closed as not planned because the user requested direct mainlining.

| Work | Ticket |
| --- | --- |
| Enlarged corpus freeze and refreshed baseline | [#570](https://github.com/reubenfirmin/TuneConsole/issues/570) |
| Compact per-vibe artifact, starting with Cruise | [#571](https://github.com/reubenfirmin/TuneConsole/issues/571) |
| Independent score semantics and calibration | [#572](https://github.com/reubenfirmin/TuneConsole/issues/572) |
| Cruise mainline integration, bounded rescoring and verification | [#573](https://github.com/reubenfirmin/TuneConsole/issues/573) |
| Focus: Reading/Studying native heads and confirmation | [#576](https://github.com/reubenfirmin/TuneConsole/issues/576) |
| Release: aggression/power experts and distinct activation | [#577](https://github.com/reubenfirmin/TuneConsole/issues/577) |
| Energize: embedding/combined activation comparisons | [#578](https://github.com/reubenfirmin/TuneConsole/issues/578) |
| Relax: calmness, VA and direct relaxed-vote comparisons | [#579](https://github.com/reubenfirmin/TuneConsole/issues/579) |
| Uplift: valence, joy and happiness comparisons | [#580](https://github.com/reubenfirmin/TuneConsole/issues/580) |
