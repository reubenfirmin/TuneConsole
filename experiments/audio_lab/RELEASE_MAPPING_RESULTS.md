# Release mapping results #614

30 October 2026. **Retain the production scorer and preserve #577's exact
Jamendo aggression head as the research candidate.** The learned power blend
and source-balanced native-task model do not improve its anger ranking. The
power blend has a supported regression on browser-prepared inputs. Lower
numerical error alone does not establish a better Release top list.

The [label audit](RELEASE_LABEL_AUDIT.md) and
[prefit plan](RELEASE_MAPPING_PLAN.md) describe the native references and
procedures. Completed artifacts are in
`local/release-614-20261030/round-2/`. This is author evidence for independent
review, not production acceptance.

## Comparison and decision

All primary results use the same **470 Soundtracks anger ratings**, grouped
into 295 identities, and the original five folds. Ratings retain the fixed
`(rating - 1) / 8` conversion. Native aggression and induced power remain
different training targets; no candidate defines its own evaluation truth.

| Method, historical inputs | NDCG | Top-list mean | Top-list lift | MSE |
| --- | ---: | ---: | ---: | ---: |
| Frozen shared scorer | .6217 | .4463 | .2928 | .06476 |
| Exact #577 Jamendo aggression head | **.7111** | **.4981** | **.3447** | .04118 |
| Newly source-selected aggression anchor | .6735 | .4672 | .3137 | .03647 |
| Zero-power calibration control | .6735 | .4672 | .3137 | .03773 |
| Learned aggression/power blend | .5783 | .4077 | .2543 | .03909 |
| Source-balanced native tasks | .6850 | .4736 | .3202 | **.03635** |

The source-only selector chose ridge alpha 100 with the newly frozen inner
folds, whereas the exact prior head used alpha 1000. The protected training
rows are unchanged. This changes ranking even before adding power; the new
anchor is an explicit control, not a replacement for the exact prior baseline.

The main blend selected **75% predicted power and 25% predicted aggression**
using nested Jamendo training MSE. Its frozen output is
`clip(1.00219646 * (.25 * aggression + .75 * power) - .03700224, 0, 1)`.
The zero-power control is `clip(.80301317 * aggression + .03508030, 0, 1)`.
Inputs are separately learned, clipped native scores. No Soundtracks labels
selected these weights or calibrations. On the fixed anger reference, adding
power under this procedure lowers NDCG from .6735 to .5783 and lift from .3137
to .2543 relative to the new anchor.

| Candidate versus exact #577 head | NDCG difference, conditional 95% interval | Lift difference, conditional 95% interval |
| --- | --- | --- |
| Blend, historical | -.1328 [-.2452, +.0081] | -.0904 [-.1682, +.0035] |
| Blend, browser | **-.1770 [-.2503, -.0240]** | **-.1377 [-.1815, -.0180]** |
| Native tasks, historical | -.0261 [-.0884, +.0160] | -.0245 [-.0743, +.0147] |
| Native tasks, browser | -.0482 [-.0837, +.0185] | -.0426 [-.0686, +.0145] |

Browser NDCG is .7112 for the exact head, .6477 for the new anchor, .5342 for
the blend and .6630 for native tasks. The native-task model remains ahead of
the shared scorer in point estimates, but its historical gains are uncertain:
NDCG +.0633 [-.0337, +.1615], lift +.0274 [-.0330, +.1002]. It does not improve
the stronger named research candidate. All refreshed-calibration, refreshed
shared, historical source and browser controls remain in `primary.report.json`.

Neither candidate meets the frozen requirement of at least +.02 NDCG and +.01
lift over the exact Jamendo head, positive paired lower bounds, and improvement
over the incumbent. Missing fresh confirmation is an additional limitation;
the observed ranking failures already justify retain without invoking it.

## What the native labels and audio predictions establish

New native held-out comparisons use protected pools of **187 Jamendo tracks
in 95 groups, 255 Emotify excerpts in 97 groups, and 467 Soundtracks excerpts
in 295 groups**. All methods use the same rows and outer folds within a source.
These smaller populations differ from #577's original native OOF cohorts.
The separately retained legacy OOF control also has its original larger
training population; it is not the fair training-pool control for this round.

| Native reference | Source anchor NDCG / MSE | Blend NDCG / MSE | Native tasks NDCG / MSE |
| --- | --- | --- | --- |
| Aggression | .5630 / .04785 | .6264 / .04751 | .5892 / .04485 |
| Power | .6971 / .02465 | .6961 / .02646 | .7057 / .02396 |
| Anger | .7830 / .02114 | .7825 / .02144 | .7803 / .02151 |

Native aggression's blend ranking gain is +.0634 [-.1105, +.1293]; lift gains
only .0120 [-.0876, +.0487]. The source-balanced changes also have intervals
including no improvement. Thus neither favorable native point estimates nor
source balancing establishes general Release transfer. All native-task fits
selected penalty 1.0; source weights are equal in the loss, with separate
targets and intercepts and no invented negative labels.

The semantic diagnostic learns a small mapping from *other observed human
attributes* to each separately observed reference, excluding that reference
from its inputs. The same fitted mapping is then fed audio-predicted attributes.

| Reference | Training-mean MSE | Mapping from human labels MSE | Same mapping from audio predictions MSE | Human / predicted-input NDCG |
| --- | ---: | ---: | ---: | --- |
| Aggression | .06098 | .04747 | .05102 | .6344 / .5553 |
| Power | .03739 | .02022 | .02667 | .6960 / .6505 |
| Anger | .05627 | .01964 | .02536 | .7184 / .7314 |

Replacing human inputs with predicted inputs increases power MSE by .00646
[+.00187, +.01110] and anger MSE by .00572 [+.00208, +.00967]. Aggression's
increase is .00355 [-.00226, +.01143], which remains uncertain. Existing
co-annotations therefore contain useful native information, and this audio
prediction path loses some of it. Direct audio heads have lower MSE than the
predicted-attribute path for all three targets.

This is not an information ceiling or a claim that observed labels always
produce better top lists: anger's predicted-input NDCG is slightly higher,
with an uncertain difference. The diagnostic tests one bounded linear mapping,
not every possible end-to-end model. No supplied source jointly labels the
same excerpts with aggression, power and anger, and none measures cathartic
benefit. Those semantic questions remain unidentified by this experiment.

## Reciprocal transfer and supported distinctions

For each target below, its entire corpus is excluded from training. These are
separate construct transfers on previously inspected populations.

| Held-out reference | Training anchor / auxiliary | Exact prior anchor NDCG | New anchor | Blend | Native tasks |
| --- | --- | ---: | ---: | ---: | ---: |
| Jamendo aggression, 216 rows | Anger / power | .7085 | .7085 | .7541 | .7029 |
| Emotify power, 400 rows | Aggression / anger | .7024 | .7098 | .6856 | .7038 |
| Soundtracks anger, 470 rows | Aggression / power | .7111 | .6735 | .5783 | .6850 |

The anger/power blend improves aggression transfer in point estimates, but its
NDCG interval [-.0135, +.0880] and lift interval [-.0190, +.0526] include no
gain. In the opposite direction, the aggression/anger blend loses .0242 NDCG
and .0112 lift against the **newly selected** aggression anchor on Emotify.
Both exceed the frozen material-loss thresholds, although their intervals
include zero. This reinforces the lack of a general replacement. The automatic
`decisions.json` reciprocal flags compare the exact legacy anchors; this
additional check against the new anchor is necessary and is recorded here.

Fixed Soundtracks slices help explain the primary blend's loss of selectivity:

| Slice | Rows | Mean anger reference | Exact aggression head | Blend | Native tasks |
| --- | ---: | ---: | ---: | ---: | ---: |
| High energy, low anger | 47 | .0644 | .0975 | .1608 | .1368 |
| High energy, happy, low anger | 23 | .0120 | .0954 | .1792 | .1472 |
| High anger, estimated tempo at most 90 BPM | 5 | .6973 | .2073 | .2059 | .2518 |
| High energy, negative valence | 32 | .4628 | .1698 | .1957 | .2104 |

The blend raises scores for energetic low-anger material without raising them
for the five high-anger excerpts with slower estimated tempo. Native tasks
preserve that descriptive contrast better, but still lose overall ranking to
the exact aggression head. These are slice means, not calibrated Release
probabilities or a matched causal comparison. Tempo is an unvalidated feature
estimate. There are no high-anger/low-energy rows at the fixed thresholds, and
only one gentle-happy row; those distinctions remain unestablished.

Calibration does not repair ranking. The original head's mean .0820 is below
the mean anger reference .1534; the blend's .1136 is closer while its ranking
is worse. The zero-power calibration changes error and means but leaves
ranking unchanged. Per-fold errors, pooled native errors, MAE, five fixed
calibration bins and training-mean/median controls are retained in the reports.

## Local evidence, resources and verification

The refreshed read-only local snapshot has 84 compatible, previously inspected
recordings, seven explicit Release positives and eleven partial captures.
First/three/all excerpt scores remain supplementary; unselected vibes are
unknown. Mean absolute first-to-all score changes are .0499 for the exact
head, .0547 for the blend and .0653 for native tasks (maxima .2400, .2247 and
.2879). These quantify capture sensitivity, not false positives or suitability.

All nine serialized scorers replay independently. Maximum numerical difference
is below 8e-16. The largest export is 33,703 bytes; the slowest of the measured
100 warm three-excerpt calls is .1514 ms; peak process RSS is 119,181,312 bytes.
All frozen resource bounds pass. These Python measurements exclude the encoder
and application startup. Host activity was concurrent and uncontrolled, so
they are observed resource checks, not isolated performance comparisons.

**Eleven focused checks passed in 1.49 seconds:** seven protocol checks and four
completed-artifact checks. They cover missing-label handling, source balance,
nested group exclusion, frozen fold memberships, input/archive/receipt hashes,
independent ranking/error recalculation and coefficient replay on real inputs.
Receipt verification also passed. No global suite was run.

The user clarified that experiments do not use the global-suite lock. That
rule is now in `AGENTS.md` and the research documentation. Release execution
and focused research checks run independently. The unfitted `round-1` is
preserved; `round-2` has identical frozen input hashes, folds, candidate grids
and acceptance criteria, with execution/check lock handling corrected.

Reproduction uses `./experiments/run.py release freeze`, `release fit`,
`release evaluate` and `release verify`; existing freezes are immutable, so
another freeze requires a new `--directory`. Focused checks:

```sh
./experiments/run.py check --test test_release_mapping.py --test test_release_results.py
```

Metrics are macro averages over the original five folds, with tie-aware linear
NDCG and top-list size `min(10, ceil(.2 * fold_rows))`. Reports separately retain
pooled native error. Intervals use 1,000 paired whole-group resamples within
those folds and condition on fixed fits and previously inspected corpora;
they do not account for prior searches. Nested selection uses seed 61430;
the reused bootstrap implementation retains seed 530580.

Runtime: Python 3.14.8, NumPy 2.4.6, SciPy 1.18.1, threadpoolctl 3.7.0.
Plan SHA256:
`f5c8eeb1dce146ff294a772e3cb787f56f78ce7065d1b0123a6fc83f430abfce`.
Evaluation receipt SHA256:
`84081c0db2c25dc95d583e94c205bd8ad9d673eb2bfd4fa1e4ac3911bd99d7b8`.

The audit identifies 1,072 unused published-audio matches from 230 Jamendo
artist IDs outside the existing cohort. A future bounded acquisition of
shards 70 and 63 could add 41 tracks from 34 artists, subject to a new freeze,
checksum verification and the 1.25 GiB combined ceiling. This is a small native
aggression extension, not independent anger-transfer confirmation. No audio was
acquired in this round. Unknown aliases, encoder exposure, sparse strong labels
and song/excerpt alignment remain limits; new artifacts remain research-only.

The practical follow-on is to preserve the exact aggression head and test its
native generalization on additional eligible artists before another broad
blend search. The present power weighting and source balancing are not grounds
for adoption. This completes #614's bounded investigation with a retain
recommendation, pending independent review. This work changes no production
code/models, live database, #405/#587 output acceptance or #137 genre calibration.
No commit or push was made.
