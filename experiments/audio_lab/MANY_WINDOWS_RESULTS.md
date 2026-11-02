# ManyMusic all-window comparison (#707)

**Retain.** Additional rated windows do not meet the frozen gain or regression
criteria. No production model or productionization subtask is recommended.
This is exploratory VA-head research on previously inspected populations.

## Data and method

All **1,695 windows / 283 recordings / 3,000 responses** are accounted for,
without rejected intervals. Natural music supplies 1,600 windows from 188
recordings in 166 components, with 2,000 responses; 1,248 windows have one
response. Generated music supplies 95 windows from 95 recordings in 88
components, with 1,000 responses. Each generated recording has one window,
making this stratum an unchanged negative control. That eligibility correction
was frozen before any fit or model-comparison outcome.

Each thirty-second window retains three consecutive ten-second inputs and one
native VA target. Repeated-response aggregation, listener IDs, counts and
dispersion are preserved. Each recording contributes total training weight one,
including normalization. Alpha selection uses the same original selected
validation window for both procedures. No chunk is independently labeled.
Five outer and three inner component folds preserve recording, artist, prompt,
audio and conservative cross-source links across all sixteen earlier source
versions (original thirteen plus three pilots) and the frozen local identity inventory.

The matched native evaluation mask retains **171 natural recordings / 152
components** and **95 generated recordings / 88 components** under historical
and browser preparation. The original common-comparison mask excludes the
other 17 natural evaluation rows. Transfer uses 470 Soundtracks and 63 original
Memo pilot rows; complete destination components leave source training before
selection. Every nonpilot Memo recording remains reserved for #710.

The same ridge family, five-alpha grid, native targets and four fixed mappings
apply throughout. Exact adopted artifacts are separately replayed on identical
rows. Those full artifacts have training exposure to these populations through
constituent experts: their controls are distinct from source-native OOF and
omitted-corpus estimates. Existing specialist mixtures are not refitted here.

## Native results

Differences are all-window minus one-window. Positive ranking/lift differences
favor all-window training.

| Mapping | Historical NDCG | Browser NDCG | Historical top lift | Browser top lift |
| --- | ---: | ---: | ---: | ---: |
| Cruise | +.00474 | +.03015 | +.00390 | +.02636 |
| Relax | -.02145 | -.03387 | -.00368 | -.02727 |
| Energize | +.01480 | +.00920 | +.00750 | +.00387 |
| Uplift | -.03793 | -.04597 | -.02342 | -.02888 |
| Four-mapping mean | **-.00996** | **-.01012** | **-.00393** | **-.00648** |

Mean NDCG paired component-bootstrap 95% intervals are **[-.03773, +.02120]**
historically and **[-.03133, +.02123]** for browser preparation. Mean top-lift
intervals are [-.02703, +.02142] and [-.02316, +.02202]. All use 1,000 draws.
The required .02 NDCG/.01 lift gain and positive lower NDCG bound fail under
both preparations. Relax/Uplift exceed the .01 domain regression ceiling.

| Natural native MSE | One-window valence | All-window valence | One-window arousal | All-window arousal |
| --- | ---: | ---: | ---: | ---: |
| Historical | .03791 | .03864 | .04418 | .04367 |
| Browser | .03776 | .03898 | .04458 | .04425 |

Generated predictions and all one/all differences are exactly unchanged.
Historical generated VA MSE is .01354/.01622; browser MSE is .01394/.01605.

## Transfer and adopted controls

Natural-source transfer NDCG differences, all-window minus one-window:

| Destination / preparation | Cruise | Relax | Energize | Uplift |
| --- | ---: | ---: | ---: | ---: |
| Soundtracks historical | +.09426 | -.03565 | +.01769 | +.11247 |
| Soundtracks browser | +.07102 | -.01882 | -.00242 | +.13920 |
| Memo pilot historical | -.01780 | -.00451 | +.00517 | +.00926 |
| Memo pilot browser | -.03343 | +.00439 | +.00471 | +.01097 |

The corresponding top-lift differences are:

| Destination / preparation | Cruise | Relax | Energize | Uplift |
| --- | ---: | ---: | ---: | ---: |
| Soundtracks historical | +.10422 | -.02044 | +.00915 | +.05474 |
| Soundtracks browser | +.07594 | +.00302 | +.00585 | +.07347 |
| Memo pilot historical | -.02001 | -.00669 | -.00155 | +.00573 |
| Memo pilot browser | -.01919 | +.00231 | -.00185 | +.00725 |

Generated-source transfer remains unchanged between one/all procedures.
The production-control criterion also fails. Natural-source browser Soundtracks
NDCG differences against the exact adopted artifacts are +.03437 Cruise,
-.18989 Relax, -.08100 Energize and -.12119 Uplift. Browser Memo differences
are +.02364, +.03046, +.00163 and -.01829, respectively. Individual positive
cells do not authorize choosing a new model after evaluation.

`report.json` contains every per-model absolute ranking, top-list lift,
fixed-reference MSE, calibration bin, native MAE/MSE and paired comparison for
both strata/preparations. These emotion/VA references do not establish listening
benefit. Missing Memo artist metadata still limits overlap claims. Conditional
intervals neither establish improvement nor prove equivalence.

## Evidence and reproduction

Evidence: `local/many-windows-707-20261101/round-5/`. Plan SHA256:
`10d446e95e97b005c51eea9d94bd2dbe1c432479353bf20fdfd934ce31dfba85`.
All outputs, models and selection traces have **16 verified receipts**.
`extraction.json` retains every window's attribution and response support.

Final extraction uniformly uses the already validated #424 C++ MusiCNN path,
with two single-thread workers. All 283 original selected windows agree with
the original cache within **.00001272 maximum feature error**, below the existing
.0005 limit. Final extraction: **808.24 seconds**, 5,085 chunks,
**32,482,973 bytes** of chunk features; largest exited child RSS
**279,334,912 bytes**. Fitting/evaluation through its receipt: **112.00 seconds**
(93.75 seconds for fits and per-cell metrics before the joint bootstrap).
Recorded Python peak RSS: **560,680,960 bytes**; largest paired candidate artifact:
**42,210 bytes**.
Python 3.14.8, NumPy 2.4.6 and native Essentia 2.1-beta6-dev were used;
complete numerical versions, graph and package hashes are frozen.

Incomplete Node attempts remain preserved in rounds 1-4, including a snapshot
initialization collision. Their compute is additional to the final timing;
aggregate CPU time was not retained. No Node feature enters the final training
matrix. Temporary PCM was cleaned after interruption and completion. The plan
records pre-fit resource/eligibility revisions; no outcome-based search expanded.

```sh
./experiments/run.py many-windows freeze
./experiments/run.py many-windows extract
./experiments/run.py many-windows run
./experiments/run.py many-windows verify
./experiments/run.py check --test test_many_windows.py
./experiments/run.py investigate many_windows_707.py -k 'available_overlap or all_windows or original_selected or frozen_components'
./experiments/run.py investigate many_windows_707.py -k 'fitted_components or receipts'
```

Use a new `--directory` to reproduce without overwriting evidence. Seven focused
and seven actual-data evidence checks pass. Saved-evidence checks cover original
features/targets, complete interval accounting, required identity links, fitted
normalization/recording weights, inner/outer/transfer exclusions and exact hashes.
No global suite or machine test lock was used. This work changes experiments only.

Source-provenance correction found during #708: the original freezer captured
imported helpers but omitted this CLI's `__main__` entrypoint. The current
`many_window_experiment.py` is separately preserved under `postrun-source/`,
with its hash, capture time and original plan hash in `postrun-source.json`.
This is post-run reproduction code, not a prospective entrypoint hash. Original
plans, archived helpers, inputs and measured outputs remain unchanged. The
freezer is fixed for future rounds. Verification now covers seventeen receipts,
including the additional post-run source record; the source-freeze gap remains
an explicit limitation for independent review.
