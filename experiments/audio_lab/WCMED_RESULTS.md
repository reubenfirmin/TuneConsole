# WCMED classical transfer, #812

**Retain the frozen specialists.** The recovered benchmark demonstrates useful
classical rank transfer, especially activation, but no new model was trained
and reconstruction limits remain. No productionization task is warranted.

The [original WCMED paper](https://arxiv.org/abs/2002.09021) and release README
identify source Saarland Western Music recordings and 400 annotated excerpt
intervals. The [source listing](https://www.audiolabs-erlangen.de/resources/MIR/SMD/western-music)
provides exact download names and anonymous performer identifiers under
CC BY-NC-SA 3.0. The strict metadata join finds 58 of 61 original recordings,
387 excerpt intervals, and 288 intervals of at least ten seconds. Three
Debussy performances have no download link; another version is not equivalent.

The source set includes piano, strings, orchestra, voice and chamber ensembles.
It should not be described as piano-only, even though
[the later distribution-gap paper](https://arxiv.org/abs/2510.04688) does so.
That paper motivates stronger harmonic representations and diverse training
data; it does not establish source parity or eliminate distribution shift.
Its linked `joann8512/emo-data-gap` repository returned GitHub HTTP 404 during
the envelope/source audit on 2026-11-01.

The bounded source-only plan was frozen before downloading or scoring:
`7ff44f3fcdcfa7229b6352086403160348f0e180633025639796bb4aef2aaba5`.
Four exact previously frozen adopted specialists and the pre-existing DEAM
native VA prior are copied into the round. There is no WCMED training,
calibration or model selection. Known recording/work/anonymous performer
connections merge transitively, yielding 18 components in the full metadata
before recovery. No train/test folds are invented for this external holdout.

The original processed excerpts are absent from the release. The README
mentions short attack/decay processing without specifying the envelope, and
the paper does not resolve it. Center-ten-second intervals from the exact
original sources are therefore a documented reconstruction, not verified
sample-equivalent audio. An at-least-twelve-second interval sensitivity
provides one second of margin at both ends but does not prove fade parity.
All short, missing and uncovered interval exclusions must remain explicit.

Local hashes and extraction receipts establish reproducibility; no publisher
audio hashes were available to verify original historical payloads. Unknown
composition aliases across source training sets and encoder-pretraining
overlap remain unresolved. This study adds external benchmark coverage and
cannot by itself justify replacing a model.

## Recovery and alignment audit

The first pass recovered 57 files in 431.17 seconds and retained one TLS
handshake timeout. Before any target scoring, a second prospective round
froze the first acquisition receipt and all successful file hashes, reused
those paths, and retried only Schumann Op105-01. It succeeded in 16.76 seconds.
The complete 58-file recovery is 449,654,833 bytes (428.82 MiB). The evaluation
round plan is
`d3c1cd36bccee31db96d0b3e6f30df02cc1dffdee6d1e491373c77c9cde087dc`;
both rounds and their source snapshots remain intact.

The final repaired cohort has 285 excerpts from 56 recordings and 18
components. Exclusions: two ambiguous metadata rows, 13 unavailable-source
excerpts, 99 too short among available sources, and one uncovered full
interval. The latter is
`Chopin_Op031_103_20040204-SMD_9.mp3`: supplied stop 524.696 seconds exceeds
decoded source duration 524.69551 seconds. The center window itself would
fit, but the frozen full-interval coverage requirement excludes it.

All supplied start/stop/selected-duration arithmetic agrees within .001
seconds. The separate processed `recording_duration(s)` field has median
excess .05217 seconds, but two entries are shorter than their selected
intervals by 1.25667 and 1.74482 seconds (Chopin Op031 segment 8 and Janacek
FromTheStreet-01 segment 8). The excluded Chopin segment 9 reports 4.26545
seconds excess. These inconsistent fields reinforce the unresolved processed
audio alignment limitation. Primary source windows use explicit start/stop
selection metadata; they do not claim that those three processed durations
have been explained by an invented fade or padding policy.

Independent acceptance checks initially failed: the metadata repeats
`Chopin_Op031_103_20040204-SMD_8.mp3` at two different intervals and omits
ranked segment 10. Round 2 had incorrectly assigned one rank to both rows.
Round 3 excludes both ambiguous rows without guessing a correction, reuses
exact verified feature vectors, and recomputes the same fixed evaluation.
Its plan is
`cef4aa7293e428c7632e9482b01ee3c3829308d466ee57f8485b42aa807028e2`.
This is a data repair after outcome exposure, not independent confirmation;
round 2 and the prematurely posted ticket evidence remain explicitly
superseded. The other failed check was 1.11e-16 numerical replay variation
with unconstrained threads; the check now uses the same single-thread setting
as prediction. The final checks below were inspected before handoff.

## Frozen transfer results

The supplied continuous ratings equal `1-2*(rank-1)/400` (within numeric
precision), confirming rank 1 is highest. Thus their bottom endpoint is
-.995, rather than exactly -1. We evaluate native association with the ranks;
rank-space [0,1] transforms are not absolute emotional ratings.

| Frozen native model | Valence Spearman | Valence component interval | Arousal Spearman | Arousal component interval |
| --- | ---: | --- | ---: | --- |
| Existing DEAM VA prior | .34880 | [.19244,.46467] | .69717 | [.59677,.76050] |
| Adopted Cruise PMEmo expert | .26844 | [.11334,.40139] | .66983 | [.56524,.73715] |
| Adopted Cruise DEAM expert | .25935 | [.10712,.38317] | .48859 | [.34787,.59655] |

Kendall coefficients for the prior are .23563 valence and .49197 arousal;
all native coefficients and intervals are retained in report.json. These
are conditional 1,000-draw performer/work/recording component intervals.

| Fixed reference | Adopted NDCG | Prior NDCG | Difference | Component interval | Adopted top-list lift |
| --- | ---: | ---: | ---: | --- | ---: |
| Cruise rank-space proxy | .56613 | .51790 | +.04823 | [-.06345,.15661] | .04246 |
| Relax rank-space proxy | .76422 | .72247 | +.04175 | [-.11694,.27134] | .13129 |
| Uplift / valence rank | .81756 | .60912 | +.20844 | [.00303,.37489] | .29548 |
| Energize / arousal rank | .94991 | .88364 | +.06627 | [-.00471,.10466] | .45044 |

The top list is ten excerpts. Uplift has a positive conditional NDCG
comparison, while its lift difference remains uncertain. Energize orders
activation well; its difference from the prior remains uncertain. Cruise
and Relax additionally depend on unverified cross-axis rank-space distances
and midpoint semantics. Their formulas are explicit proxies, not direct
listener suitability labels. No Focus, Release or mood-regulation inference
is supported. Converted-score MSE/calibration summaries in the machine report
are descriptive rank-space errors only.

The 209-excerpt one-second-edge-margin subset yields adopted NDCG
.52579 Cruise, .78200 Relax, .74860 Uplift and .95008 Energize. Corresponding
prior values are .51729, .73875, .47879 and .90713. The component results and
recording-cluster sensitivity are both retained; neither resolves the missing
processed excerpts or historical annotation uncertainty.

## Reproduction and resources

Artifacts: `local/wcmed-812-20261101/round-1` contains the preserved first
acquisition, `round-2` holds the retry and superseded 287-row evaluation,
and `round-3` holds the repaired 285-row evaluation. Original extraction
took 93.44 seconds, 267.77 MiB child peak RSS; all 285 vectors were reused
in the repair with no new extraction. Final evaluation took 8.71 seconds. Python
3.14.8 / NumPy 2.4.6 / SciPy 1.18.1; C++ Essentia 2.1-beta6-dev;
one numerical thread. Every serialized adopted specialist replays exactly.

```sh
./experiments/run.py wcmed-study verify
./experiments/run.py investigate wcmed_812.py
```

All four evaluation-round receipts and the earlier acquisition receipt
verify; two actual-source/interval and exact replay checks pass. Fresh work
requires a new frozen round. No production code, live database, global test
suite, nonpilot Memo fitting or user listening collection was used. Review
must retain the stated alignment and ordinal-reference gaps; this is an
additional reconstructed-source benchmark, not exact original-audio parity.
