# Independent per-vibe model selection, 29 October 2026

This research asks whether selecting a different source, learner and parameters
for every vibe improves held-out ranking. It does not change the application.
The first batch covered four new native specialist fits and thirteen frozen
source experts. A separate extension covers all thirteen native expert-source
versions. These scopes must not be conflated with every discovered dataset or
every historical corpus variant.

All candidates use the existing cached 200-value MusiCNN embedding. The varied
models are dataset-trained heads and suitability heads, not alternative audio
encoders. No new audio extraction or encoder training is included in these fits.

Evaluation truth has two separate meanings. Native models are evaluated against
external human emotion/activity annotations, with five outer and three inner
group folds. Product-vibe heads are evaluated against 55 reused historical human
labels in 48 connected artist groups. The latter is an exploratory diagnostic,
not fresh independent six-vibe confirmation. The twenty previously inspected
reserved additions are excluded from fitting, selection and scoring. All 76
frozen local identities protect native fits. Prediction labels are never truth.

## First batch: four native sources

Immutable artifacts: `local/per-vibe-20261029/round-1/`.
Protocol: [PER_VIBE_ROUND.md](PER_VIBE_ROUND.md).
Plan SHA256: `698368ea885703c7c12d499202afbff1b27783bf6e8312dd85670f851af32cdc`.

| External task | Rows | Joint ridge MAE | Independent target selection MAE | Training-median MAE |
| --- | ---: | ---: | ---: | ---: |
| Emotify, nine emotions | 400 | 0.11251 | 0.11303 | 0.14327 |
| CAL500, fifteen activities | 494 | 0.12568 | 0.12604 | 0.09938 |
| PMEmo, arousal/valence | 766 | 0.08540 | 0.08540 | 0.14876 |
| DEAM, valence/arousal | 1,800 | 0.08220 | 0.08235 | 0.12769 |

Lower MAE is better within each row; errors from different tasks are not
comparable. Independent selection did not improve mean native error in this
batch. Sparse CAL500 activities also show why a low MAE alone does not establish
useful ranking: the constant median is better on that metric. Full reports retain
every target's correlations and errors, including reading, studying and work.
DEAM lacks artist names for 1,751 of these 1,800 rows; its grouping preserves
known recordings/duplicates and available artists, not unknown artist identities.

Each independent vibe selector chooses separately for all six vibes inside its
training folds. The first batch offered 457 candidates: the incumbent plus 24
model configurations on nineteen representations. The shared selector is a
control that makes one joint source/configuration choice.

| Historical OOF AP | Incumbent | Shared AP selector | Independent AP | Independent loss | Guarded AP |
| --- | ---: | ---: | ---: | ---: | ---: |
| Relax | 0.232 | 0.437 | 0.225 | 0.335 | 0.225 |
| Focus | 0.320 | 0.314 | 0.318 | 0.330 | 0.377 |
| Cruise | 0.631 | 0.674 | 0.658 | 0.584 | 0.634 |
| Energize | 0.620 | 0.614 | 0.493 | 0.494 | 0.493 |
| Lift my mood | 0.669 | 0.611 | 0.425 | 0.647 | 0.488 |
| Let off steam | 0.623 | 0.630 | 0.263 | 0.628 | 0.623 |
| Macro | 0.516 | 0.546 | 0.397 | 0.503 | 0.473 |

The fully independent AP selector performed worse. Its sources varied
substantially across folds. The guarded selector retained the incumbent for
Release in all five folds, but did not prevent overall deterioration. Focus's
guarded AP increase did not improve its top-five selected fraction (both 0.20)
and its AUC was lower (0.534 versus 0.573). No specialist is selected for adoption
from these outer results. The 0.546 shared result also needs new confirmation.

These numbers use this round's seed and connected groups. They must not be
treated as directly comparable to the earlier differently grouped checkpoint
diagnostic, or as evidence that the unchanged product model regressed.

Native exported-head replay matched exactly. Local exported heads differed by
at most 4.17e-17; complete dependency-bundle replay differed by at most 8.89e-16.
The research bundle is 17,687,935 bytes, including all controls and dependencies.
Cold Python bundle replay on 55 saved embeddings took 0.022–0.278 seconds across
selectors, including loading and checksum checks. This excludes audio embedding
extraction and is not a browser deployment benchmark. All five completed stages
were subsequently verified reusable without fitting or rescoring.

## All thirteen source versions

Protocol: [PER_VIBE_ALL_SOURCES.md](PER_VIBE_ALL_SOURCES.md).
Artifacts: `local/per-vibe-20261029/all-sources-1/`.

The extension retains the same learner grids and selectors. It verifies and
reuses the four native stages above, then fits Soundtracks, VGMIDI, Jamendo,
Coimbra MIREX-like, MERGE, matched EMMA previews, MERP, matched AMG1608 and matched
NTWICM. Its local comparison includes all thirteen frozen experts and all
thirteen native-specialist representations. Read `summary.json` and the stage
receipts for completion and results. All thirteen native stages and the local
stage completed. Plan SHA256:
`9d5217aee7945ac2e81b942b5801e376c274424b210e8194e99d794497fec2cb`.

| External source | Rows | Joint ridge MAE | Independent target MAE | Training-median MAE |
| --- | ---: | ---: | ---: | ---: |
| Emotify | 400 | 0.11251 | 0.11303 | 0.14327 |
| CAL500 activity votes | 494 | 0.12568 | 0.12604 | 0.09938 |
| PMEmo | 766 | 0.08540 | 0.08540 | 0.14876 |
| DEAM | 1,800 | 0.08220 | 0.08235 | 0.12769 |
| Soundtracks | 470 | 0.12381 | 0.12670 | 0.18758 |
| VGMIDI original subset | 76 | 0.09331 | 0.08778 | 0.11649 |
| Jamendo human votes | 216 | 0.22141 | 0.22521 | 0.28946 |
| Coimbra MIREX-like | 900 | 0.26957 | 0.26127 | 0.20000 |
| MERGE quadrants | 3,549 | 0.22809 | 0.20192 | 0.25000 |
| EMMA matched previews | 333 | 0.07359 | 0.07357 | 0.09401 |
| MERP static | 49 | 0.06728 | 0.07515 | 0.10244 |
| AMG1608 matched | 403 | 0.09163 | 0.09183 | 0.12756 |
| NTWICM matched | 55 | 0.10974 | 0.11491 | 0.13210 |

These are 9,511 corpus rows, not that many unique independent recordings:
corpora overlap. No pooled cross-corpus accuracy or confidence interval is
claimed. Independent selection improved mean MAE in four sources, tied in one
and worsened it in eight. MERGE's MAE reduction was 0.02616, with a conditional
fixed-OOF group interval [0.02106, 0.03103]. Coimbra's was 0.00830
[0.00261, 0.01432]; its training-median control still had lower MAE. VGMIDI has
only five franchise groups and an interval spanning zero. These intervals exclude
selection uncertainty and do not correct for comparisons across sources.

| Historical OOF AP, 673 candidates | Incumbent | Shared AP selector | Independent AP | Independent loss | Guarded AP |
| --- | ---: | ---: | ---: | ---: | ---: |
| Relax | 0.232 | 0.437 | 0.225 | 0.343 | 0.225 |
| Focus | 0.320 | 0.314 | 0.281 | 0.315 | 0.281 |
| Cruise | 0.631 | 0.674 | 0.658 | 0.579 | 0.634 |
| Energize | 0.620 | 0.614 | 0.473 | 0.445 | 0.503 |
| Lift my mood | 0.669 | 0.611 | 0.429 | 0.647 | 0.470 |
| Let off steam | 0.623 | 0.630 | 0.263 | 0.408 | 0.623 |
| Macro | 0.516 | 0.546 | 0.388 | 0.456 | 0.456 |

The broader source search did not improve the independent selection procedure.
The shared selection was unchanged. Neither native MERGE improvement nor a
post hoc local winning cell establishes a better six-vibe product model.
Per-vibe AUC, top-five selected fraction, positivity counts, inner and outer fold
choices, full-development exports and conditional AP intervals are retained in
`local.report.json`.

The complete all-controls research bundle is 140,812,982 bytes. Independent
full-bundle replay differed from fitted predictions by at most 8.89e-16.
Cold serialized Python replay on 55 saved embeddings took 0.088–4.988 seconds
across selectors, including loading and dependency checks. This is a research
verifier with duplicated neighbor inputs and all-control dependencies, not an
optimized deployment artifact or a browser/audio-extraction benchmark. Native
fits were quick because embeddings were cached; MERGE's full serialized replay
was substantially more expensive than the smaller-source checks.

Annotation quality differs between these sources. Emotify, Soundtracks, PMEmo
and DEAM provide rated supplied excerpts. Jamendo has explicit human song votes;
CAL500's original activity votes label whole songs rather than the extracted
window. VGMIDI labels are weak for the piano arrangements, Coimbra uses AllMusic
tag-derived clusters, and MERGE uses manually checked tag-derived quadrants.
EMMA previews are not verified to be the longer rated excerpts. MERP's static
means summarize dynamic ratings over more audio than the center input. AMG1608
and NTWICM use identity-matched audio with song-level annotations. These are
independent external sources with different evidential strength, not uniformly
direct human judgments of the exact model input. The frozen row metadata retains
the annotation units.

Groups also vary: Soundtracks holds films together, Jamendo uses publisher artist
IDs, and VGMIDI holds five franchises together. Available artist/recording/audio
links are preserved elsewhere. Unavailable identities cannot be excluded by
name; grouping and local-overlap checks do not resolve unknown overlaps.

The extended driver passed 75 focused research checks. Source and numerical
versions are frozen in each plan. The run uses Python 3.14.8, NumPy 2.4.6,
SciPy 1.18.1 and threadpoolctl 3.7.0, with one numerical thread.

## Interpretation

Different models per vibe remain a valid architecture to investigate. The first
batch does not justify deploying its selection procedure. A large search over a
small reused label cohort gives unstable choices; it does not replace objective
native-task evidence or fresh six-vibe listening/reference evaluation. The
external datasets improve the evidence for emotion/activity predictions, while
the translation into all six product vibes remains a separate unresolved claim.

Retain the current product model. Keep the frozen specialist exports for
independent comparison, preserve negative results, and avoid choosing individual
vibe winners after reading outer-fold scores.
