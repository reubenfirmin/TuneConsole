# Shared Emotify classifier: provenance

Selected Oct 28, 2026 (#507). The bundled model remains the shared six-way
classifier and radio-intensity baseline. Six later specialists replace
within-vibe suitability independently; their scales are not comparable class
probabilities. Missing/incompatible embeddings retain the existing heuristic.

## Derivation

Frozen 200-value MusiCNN embeddings feed a ridge head trained on all 400
[Emotify](../datasets/emotify.md) excerpts and nine native emotions. A linear
six-vibe calibrator uses 55 developer songs with compatible embeddings. Clip
each regression to [0,1], then normalize the six outputs to sum to one.
The 56th human-labeled song lacks an embedding; all 63 prediction-labeled
silver songs are excluded. Nothing fits or looks up training labels at runtime.

Artifact: [emotify-vibes.json](../../../src/yt_playlist/rec/data/emotify-vibes.json),
51,197 bytes, SHA256
`5351572e6eb1bc17f80a6519b9b3f1b8461775726591aa847077a4fdb55732a0`.
Embedding signature:
`8a954566923632879d60fe1786e3c44af8732d7c72fe5fb5b03125c388265940`.
Original export: `local/external-20261028/models/consensus-soundtracks-set2+cal500+emotify+pmemo.json`.
Its Emotify expert alone is extracted by
`uv run python -m experiments.audio_lab_tools.export_emotify_product`.
The artifact stores source/report/snapshot hashes and attribution.

## Experiments and decisions

| Date / comparison | Outcome and reason |
| --- | --- |
| Oct 27, local corrections | On 40 reused tracks, individual legacy hard-example fitting reached 26/40, but nested procedure selection only 22/40. Alternative-label NLL/Libra did not improve it. Training fit is not generalization. |
| Oct 28, checkpoint | Emotify gave 38/55 acceptable top choices (69.1%), NLL .928, versus embedding-only 34/55 and 1.040. Five artist-group outer folds with training-only selection; exploratory reused developer data. |
| Oct 28, input/4Q controls | Combined acoustic/mood inputs and Emotify/4Q blends did not improve the checkpoint. Provider-shared inputs gave 23/55 and macro AP .256; this is not the embedding model's 69.1% accuracy. |
| Oct 28, objective check | The captured-audio heuristic had worse top choice (28/55) but better ranking AP (.602 versus .528). The checkpoint never established superiority for every ranking objective. |
| Oct 29, #530 campaign | Alternate native learners/calibrators, expert subsets, independent ranking, static/aligned targets, consistency, PCA/whitening, multitask and stored-teacher ablations did not establish a broad six-vibe replacement. Local soft-target selection chose zero weight; derived predictions add no human votes. |
| Oct 29, reserved additions | Primary 18 additions excluding prior silver use: checkpoint 12/18, expanded consensus 13/18, PCA 13/18; paired NLL gains inconclusive. Zero Release positives. The set is now consumed evidence. |
| Oct 29, per-vibe selection | Four-source independent AP selection macro .397 versus incumbent .516; expanding to thirteen sources did not justify a broad replacement. Prefer declared external native-target mappings to selecting another reused-local maximum. |
| Oct 29, shared refresh | Calibration-only and native+calibration refits improved some Release points but regressed other vibes. Retain shared classifier; adopt independent Cruise suitability. |

Evidence: `local/external-20261028/` and `checkpoint-1/reserved-cohort/`,
`local/per-vibe-20261029/{round-1,all-sources-1}/`,
`local/mapped-vibes-20261029/cruise-baselines-1/`.
Read-only `./experiments/run.py evidence` summarizes campaign artifacts;
`cruise-baselines verify` checks the refresh evidence. Coefficient replays and
installed-package checks establish parity, not population accuracy.

## Unselected pooling proposal

#819's `vibe-pooling` is a prospective shared prediction-pooling evaluation;
no run or qualification is established here. Respect any current execution hold.
`freeze --db ISOLATED_SNAPSHOT --plan PLAN.json` uses read-only repository access;
mask all target-recording observations, distinguish known/unseen artists, and
keep optional held-out human targets out of every fit. Format-2 plans require
60 tasks (two strata, five folds, six vibes). Each fit is bounded to ten seconds
and 128 MiB; incomplete fits are deferred. Adequacy requires twenty scored
recordings and five connected artist clusters per vibe/stratum plus all declared
quality, uncertainty, lifecycle and resource gates. No result auto-selects it.
Storage protocol: [FAO owners](../../retained_copy/FAO-OWNERS.md#shared-vibe-model-candidate-819).
