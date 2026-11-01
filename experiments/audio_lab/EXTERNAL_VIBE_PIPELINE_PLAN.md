# Next experiment: externally supervised attributes and fixed vibe mappings

User clarification, 29 October 2026: studying/reading directly supervise Focus.
The intended pipeline has two stages: predict dataset attributes from our audio
analysis, then transform those predicted label scores into the six product
vibes. External dataset labels supply both training targets and evaluation
references. Local-label calibration is optional and secondary.

This plan supersedes using the historical 55-song calibration search as the
main decision for this approach. Preserve both completed specialist batches and
their negative local-calibration results. Those results do not reject externally
trained attribute predictors followed by fixed mappings.

## Stage 1: audio analysis predicts external attributes

Reuse the cached audio features, source identities, original annotations and
provenance from all thirteen expert-source versions. Compare our acoustic
attributes, MusiCNN embeddings and their combination. The previous specialist
batch used embeddings only. No new audio extraction is required unless the
input/provenance audit finds a missing or incompatible feature family.

Keep original annotation semantics and values. Listener vote fractions remain
fractions; continuous ratings remain continuous; classes retain their identity.
Select an appropriate proper loss for each annotation type and report ranking
alongside prediction error. Do not binarize fractional listener votes or select
every native task using MAE alone. Retain constant/mean/median controls.

Different attributes and vibes may use different sources, learners and
regularization. Reuse completed fits as baselines where provenance matches;
changed objectives require new training-fold selection. Use grouped nested
evaluation and training-only preprocessing. Preserve failures and small-group
limits, including VGMIDI's five franchises.

## Stage 2: frozen transformations produce vibe scores

Write a readable mapping registry before fitting the next comparison. Each rule
records its exact source labels, scale conversion, formula, supported vibes and
missing-label behavior. Keep all six scores independent.

| Vibe | Proposed label support |
| --- | --- |
| Relax | Calmness, peacefulness, relaxed; low tension |
| Focus | Reading and studying activity votes directly |
| Cruise | Moderate activation, easygoing/relaxed character, low aggression |
| Energize | Activation/energy, rousing, exercise activity |
| Lift my mood | Joy, happy, positive valence |
| Let off steam | Power, aggression, anger/force; preserve slow, heavy examples |

Start Focus with the equal mean of `Usage-Reading` and `Usage-Studying`.
`Usage-At_work` is a separate predeclared ablation rather than an automatic third
term. Do not blend the direct Focus score with generic calmness by default.
Likewise do not equate Cruise with driving, or require fast tempo or negative
valence for Let off steam. Preserve the current product definitions.

A source supports only the mappings justified by its available labels. Missing
targets do not become zeros or invented suitability judgments. Give supplied
rated excerpts and direct human activities their stated roles; retain weaker
tag-derived, song-level and mismatched-preview evidence as identified sources.
No weights or mapping formulas are fitted to the local vibe labels.

## Primary external evaluation: transform annotations and predictions alike

For each supported dataset/vibe pair, apply the same frozen rule to:

1. Held-out dataset annotations, producing the externally annotated reference.
2. Predictions generated without training or selection on those held-out rows.

For Focus, the reference is the mean of the human Reading and Studying vote
fractions; the prediction is the mean of the two predicted fractions. Score
their ranking and prediction error directly. This evaluates the composed
pipeline against the external judgments under the agreed mapping.

Predeclare ranking as primary for vibe suitability: tie-aware NDCG for the top
20 percent of each validation partition, capped at ten tracks. Retain top-set
mean annotated suitability, lift over a constant/random-ranking control,
Spearman correlation and annotation-appropriate loss. Handle partitions with
no relevance variation explicitly rather than manufacturing an improvement.
Use native proper losses to diagnose the attribute predictors.

Compare alternative models on the same external target rows and annotations.
Do not pick a dataset because its raw AP or MAE is numerically better than a
different task's result. Where mappings align, compare source-trained experts
on another corpus with source-only selection. Hold out known artist, recording
and audio connections from every contributing training source, including
matched-audio collections. Label unavailable identity links explicitly.

These are mapped external-annotation references. Direct activity judgments and
composite emotional rules retain their different semantics. Do not claim that
every composite is a separately collected direct judgment of listening benefit.

## Delivery order

1. **Focus end to end.** Freeze the Reading/Studying rule and external evaluation
   protocol, build the new comparison through the approved entrypoint, and
   produce held-out reference/prediction rows, ranking metrics, controls and an
   export replay check. Include acoustic-attribute and embedding ablations.
2. **All six vibes.** Implement the registry for the thirteen source versions,
   run every supported dataset/vibe pair and retain a coverage matrix. Select
   attribute/vibe specialists inside external training folds. A source lacking
   appropriate labels is marked unsupported for that pair.
3. **Transfer and source ablations.** Evaluate aligned rules across independent
   corpora, with overlap protection. Compare single specialists and bounded
   combinations using external training evidence. Preserve old transfer pilots
   as previously inspected results, not pristine new confirmation sets.
4. **Research export and consumer checks.** Export six explicit scorers with
   source/label/model/mapping hashes and no local-label dependency. Verify full
   serialization, independence of vibe scores, size and inference cost. Exercise
   shared intensity and Radio/Mix Tape consumers against isolated library inputs.
5. **Secondary local diagnostic.** After the full pipeline is frozen, optionally
   inspect historical human labels and local tracks for errors and coverage.
   Do not tune mappings or select the main model on these labels. Stored local
   predictions support operational/consistency checks and never become human
   training truth or independent votes.

Keep edits, models, reports and acquired data under `experiments/`, use
`./experiments/run.py`, preserve prior frozen inference code and artifacts, and
run heavy stages sequentially. Freeze exact mappings, model grids, objectives,
splits and provenance before fitting. The first deliverable is the externally
evaluated Focus pipeline; product adoption remains a separate reviewed change.
