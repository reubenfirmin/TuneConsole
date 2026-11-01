# Focus experiment #576: retain the current scorer

30 October 2026. The bounded native-MSE comparison improves estimated vote
fractions and produces small replayable models, but does not establish a
reliable ranking gain. Retain the current scorer. Keep the three candidates as
research artifacts; do not select the browser variant from its outer result.

The frozen plan is `local/individual-20261030/focus-1/plan.json`, SHA256
`5ea7fc8938c9efc01c4efcf5fd28d8af8a034d0326bcc3666803f0a0c0d842d7`.
All 494 original CAL500 rows survive the current local-identity exclusions.
The comparison below uses the same 450 product-disjoint recordings and original
five grouped outer folds. Reading and Studying remain separate native listener
fractions; Focus is their equal mean. Selection uses three inner source folds,
native MSE, a training-mean option, five ridge settings and six small RFF
settings. No target ranking, local labels or outer result selects a head.

| Method | NDCG | Top-set suitability | Lift | MSE |
| --- | ---: | ---: | ---: | ---: |
| Historical embedding | 0.4708 | 0.2203 | 0.1181 | 0.01911 |
| Historical combined | 0.4540 | 0.2117 | 0.1095 | 0.01923 |
| Browser embedding | 0.4767 | 0.2203 | 0.1181 | 0.01904 |
| Exact prior native MAE heads | 0.4453 | 0.1920 | 0.0898 | 0.02077 |
| Frozen shared checkpoint | 0.4279 | 0.1749 | 0.0727 | 0.02120 |
| Refreshed calibration | 0.2614 | 0.1267 | 0.0245 | 0.02575 |
| Refreshed native and calibration | 0.3306 | 0.1438 | 0.0416 | 0.02221 |
| Browser shared control | 0.3940 | 0.1682 | 0.0660 | 0.02113 |
| Acoustic ridge control | 0.3613 | 0.1708 | 0.0686 | 0.02108 |
| Training mean | 0.2227 | 0.1022 | 0.0000 | 0.02180 |
| Training median | 0.2227 | 0.1022 | 0.0000 | 0.03213 |

The browser candidate's NDCG difference from the prior is +0.0314, with
conditional paired 95% interval **[-0.0437, +0.1558]**. Top-list lift differs by
+0.0283, interval [-0.0106, +0.0841]. Its MSE difference is -0.001731,
interval [-0.002720, -0.000763]. Historical embedding shows the same pattern:
NDCG +0.0255 [-0.0491, +0.1496], MSE -0.001662
[-0.002672, -0.000668]. These 1,000 bootstrap draws resample whole identity
groups within the fixed outer folds. They condition on fitted procedures and
previously inspected data, and do not establish fresh confirmation or correct
for every historical research choice.

Native Reading/Studying MSE for browser embedding is 0.02549/0.03267, versus
0.02751/0.03640 for the prior and 0.02763/0.03600 for training means. Native
MAE, five fixed calibration bins, all fold results and every selection trace
remain in the machine reports. Shared normalized scores and native vote
fractions have different semantics; their MSE is a reference-scale diagnostic,
not proof of calibrated user suitability.

All three new exports replay **each native head** exactly through the
independent NumPy implementation. Their sizes are 33,440 / 38,290 / 33,465
bytes; maximum warm three-excerpt inference is 0.118 / 0.152 / 0.132 ms.
The evaluation process peaks at 653,266,944 bytes RSS. These pass the frozen
256 KiB / 100 ms / 1 GiB bounds. They measure Python inference, excluding the
encoder and application startup. The exact prior export is 467,940 bytes and
fails this round's artifact-size budget; it remains unchanged for comparison.

The read-only local export contains 84 compatible labeled recordings, including
18 explicit Focus positives and 11 one/two-excerpt captures. All identities
were previously inspected. There are no explicit Focus negatives or
distraction labels, so they cannot measure false positives or concentration
benefit. First/all-excerpt scores and capture positions are retained in
`local.report.json`; combined historical features are not applied to an
unverified current acoustic schema. CAL500 itself labels whole songs while
these cached features describe one ten-second excerpt.

[The new-reference audit](FOCUS_REFERENCE_RESULTS.md) records two acquired
activity/study datasets, 209 Moods/Activities title-and-artist candidates and
13 study-music candidates, but **zero verified recording/excerpt joins**.
They supply no new confirmation rows. No proxy label or unselected local vibe
was turned into a negative. No listening packet is needed for this no-go.

Reproduction (preserve the existing completed directory):

```sh
./experiments/run.py focus freeze --directory experiments/audio_lab/local/individual-20261030/focus-reproduction
./experiments/run.py focus fit --directory experiments/audio_lab/local/individual-20261030/focus-reproduction
./experiments/run.py focus evaluate --directory experiments/audio_lab/local/individual-20261030/focus-reproduction
./experiments/run.py focus verify
./experiments/run.py check --test test_focus_experiment.py
```

Frozen execution checks current code against its archived version, so a later
entrypoint change requires a new freeze. Completed receipt verification remains
available. Six focused checks pass, including fractional constant targets,
group leakage rejection, per-head replay cancellation and historical receipt
compatibility. Fit/evaluation receipts and all referenced hashes verify.
An additional independent check in `test_individual_experiment.py` reproduces
NDCG, top-set suitability and MSE for every method on the actual 450 saved rows
without importing the ranking implementation; it also verifies output hashes.
Runtime: Python 3.14.8, NumPy 2.4.6, SciPy 1.18.1, threadpoolctl 3.7.0.
The logs retain earlier bounded test-lock waits and setup failures; neither
was counted as a model result. All files and artifacts stay in `experiments/`.
