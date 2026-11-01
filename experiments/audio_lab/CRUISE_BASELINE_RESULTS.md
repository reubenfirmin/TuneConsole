# Cruise against refreshed shared models

29 October 2026, #570 / #530. Advance the compact Cruise individual model and
retain the current validated shared checkpoint for the other five vibes.
Refreshing the shared architecture does not produce a consistent replacement.
These experiments do not enable any product model.

## Frozen procedure and inputs

Commands, in order:

```sh
./experiments/run.py cruise-baselines freeze
./experiments/run.py cruise-baselines fit
./experiments/run.py cruise-baselines evaluate
./experiments/run.py cruise-baselines verify
```

Evidence is in `local/mapped-vibes-20261029/cruise-baselines-1/`: frozen inputs,
code archive, plan, three shared artifacts, compact Cruise artifact, fit and
evaluation reports, external predictions and checksummed stage receipts.
`cruise_baselines.py` implements this bounded follow-up through the approved
entrypoint. No application source, defaults or live database content changes.

The read-only human-label export contains 84 compatible recordings. Reserve
whole connected identity groups containing an ID absent from the previous
76-ID protected snapshot: **76 development / 8 reserved**. The eight additions
have no recorded component matches against the original protected identities,
gold, silver, teacher training or exact PMEmo/DEAM Cruise training rows. The
original gold IDs show no changed labels. Missing artists, unknown aliases,
other historical weak-training exposure and upstream overlap remain limits.
An additional identity-only audit after scoring also finds no reserved matches
in the fixed shared model's original 400-row Emotify training cohort. It changes
no cohort, fit or result: `./experiments/run.py cruise-audit` records the audit in
`shared-native-exposure.json`, explicitly marked as a later provenance check.

Both controls preserve the shipped two-stage ridge architecture and score
normalization. `refresh_calibration` retains the original Emotify native head;
`refresh_both` refits that head after excluding local and all declared external
evaluation identities, retaining **260 native songs**. Three grouped training
folds select each penalty. The native penalty is 100 and both final calibration
penalties are 10. Five outer development folds diagnose calibration selection;
normalization and calibration fitting occur inside those folds. Native fitting
does not use any local vibe labels. The original checkpoint remains a control.

All shared artifacts are independently replayed against the actual product
module's scalar stages on all 76 development embeddings, within 1e-12 absolute
tolerance. Reserved outcomes are absent from fitting and selection. They were
scored after the exported artifacts and external comparisons were fixed, and
are now consumed supplementary evaluation data.

External evidence is primary. All controls use the same target rows, excluding
the union of known original and refreshed shared-training identities. This
retains 186 natural ManyMusic, 95 generated ManyMusic, 63 Memo2496, 470
Soundtracks and 450 CAL500 recordings. The same fixed annotation-to-vibe rules
apply throughout. Unsupported dataset/vibe pairs remain absent.

## External Cruise ranking

Values are tie-aware graded NDCG, averaged over the five existing folds. Each
fold ranks its top ten recordings, or top 20% if smaller. The compact specialist
is the previously fixed equal PMEmo/DEAM ensemble, with mean native attributes
before the Cruise mapping. There is no new candidate or mapping selection.

| Supplied-audio cohort | Rows | Checkpoint | Refreshed calibration | Refreshed both | Cruise individual |
| --- | ---: | ---: | ---: | ---: | ---: |
| ManyMusic natural | 186 | 0.6669 | 0.6544 | 0.6569 | **0.8358** |
| ManyMusic generated | 95 | 0.7022 | 0.6916 | 0.6646 | **0.8943** |
| Memo2496 pilot | 63 | 0.8156 | 0.8100 | 0.7947 | **0.8646** |

Paired 95% intervals for the individual model's NDCG gain:

| Cohort | Against checkpoint | Against refreshed calibration | Against refreshed both |
| --- | --- | --- | --- |
| Natural | +0.1689 [0.0865, 0.2274] | +0.1814 [0.0927, 0.2372] | +0.1789 [0.0907, 0.2451] |
| Generated | +0.1921 [0.0953, 0.2753] | +0.2027 [0.0902, 0.2635] | +0.2296 [0.1226, 0.2839] |
| Memo2496 | +0.0491 [-0.0444, 0.1439] | +0.0546 [-0.0325, 0.1511] | +0.0700 [-0.0304, 0.1608] |

The 1,000 paired bootstrap draws resample complete known identity groups inside
each fixed fold. They condition on fitted coefficients, source selection and
the previously inspected cohorts, without a multiple-comparison correction.
The Memo2496 improvement remains uncertain. This is confirmation on reused
corpora, not a fresh population benchmark. Absolute-score errors in the full
report do not make independently scaled scores calibrated probabilities.

## The other five vibes

All supported mappings are reported, including weaker sleep/exercise activity
proxies. The table contains all non-Cruise pairs in this bounded comparison.

| Cohort | Vibe | Checkpoint NDCG | Refreshed calibration | Refreshed both |
| --- | --- | ---: | ---: | ---: |
| ManyMusic natural | Relax | 0.7579 | 0.7583 | 0.7319 |
| ManyMusic natural | Energize | 0.7960 | 0.7669 | 0.7053 |
| ManyMusic natural | Uplift | 0.6919 | 0.6515 | 0.6727 |
| ManyMusic generated | Relax | 0.8607 | 0.8527 | 0.8523 |
| ManyMusic generated | Energize | 0.9187 | 0.9180 | 0.9162 |
| ManyMusic generated | Uplift | 0.9219 | 0.8658 | 0.8437 |
| Memo2496 | Relax | 0.8469 | 0.8348 | 0.8294 |
| Memo2496 | Energize | 0.9160 | 0.8053 | 0.8353 |
| Memo2496 | Uplift | 0.9162 | 0.8682 | 0.8770 |
| Soundtracks | Relax | 0.5799 | 0.5140 | 0.5333 |
| Soundtracks | Energize | 0.8489 | 0.7888 | 0.7754 |
| Soundtracks | Uplift | 0.7318 | 0.7157 | 0.7248 |
| Soundtracks | Release | 0.6217 | 0.6688 | 0.6619 |
| CAL500 | Relax (sleep proxy) | 0.5209 | 0.4801 | 0.4931 |
| CAL500 | Focus (Reading/Studying) | 0.4279 | 0.2614 | 0.3306 |
| CAL500 | Energize (exercise proxy) | 0.3346 | 0.2569 | 0.2066 |

The refreshed controls improve Soundtracks Release but regress on several other
vibes. Retain the validated shared checkpoint rather than replacing all five.
Release's result is evidence for its future investigation, not a reason to
insert another specialist during the Cruise-only rollout.

For completeness, Soundtracks Cruise is 0.8150 / 0.8342 / 0.8396 for the three
shared controls. The compact pilot-trained Cruise artifact is not given a
Soundtracks score here: it was not selected under that target-exclusion regime.
Its explicit comparisons remain the three common pilot cohorts.

## Supplementary local result

All three shared controls correctly choose an acceptable label on **6/8**
reserved recordings. That provides no top-choice separation. Cruise has only
**two positives** among these eight: shared Cruise average precision is
**0.4167**, versus **1.0000** with the individual model; the two positives rank
first. Other five scores remain unchanged for each corresponding shared model.
This is a small developer-label check, not evidence of perfect product quality.
There are no positive Focus labels in this slice, so its AP is unsupported.

Hybrid scores are compared only within each vibe. The overall shared classifier
continues to use its original six comparable scores; no top-choice accuracy is
computed by taking a maximum over mixed score scales.

## Validation and remaining work

`./experiments/run.py check` passed **118 checks in 4.10 seconds** using the
normal machine lock. New checks cover reserved collaborator components and
paired whole-group uncertainty; the compact-runtime checks include malformed
models, incompatible captures, partial captures and exact preservation of the
other five scores. Syntax parsing and `git diff --check -- experiments` passed.
The frozen baseline inputs, code, coefficients and output receipts verified.
Runtime: Python 3.14.8, NumPy 2.4.6, SciPy 1.18.1, threadpoolctl 3.7.0.

This closes the planned baseline comparison for review. Consumer integration,
score semantics, bounded real-library rebuilding, listening and release checks
remain #572–574; see [production preparation](CRUISE_PRODUCTIONIZATION.md).
Other individual models remain in #576–580, in Backlog.
