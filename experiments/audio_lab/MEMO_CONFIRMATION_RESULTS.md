# Memo recording confirmation, #710

**No new model or production change.** Cruise/Relax are inconclusive against
both controls under the frozen rules; Uplift/Energize trigger regression limits
in both preparations. These are conditional same-corpus diagnostics, with wide
ranking intervals. No winner was chosen and no production subtask is warranted.
The [protocol](MEMO_CONFIRMATION_PLAN.md) freezes an evaluation-only cohort;
there are no model fits, mixture choices or calibration updates.

## Cohort and alignment

All **128 reserved recordings** acquired successfully, **647,396,794 MP3 bytes**.
Exact range requests, including retries, used **641,195,243 bytes** in
**299.17 seconds**, within the 1 GiB / 30-minute bounds. ZIP member CRC,
published member sizes and each saved audio SHA256 are checked. The whole
12.3 GB archive MD5 is not established by these partial reads.

The joint audit against **9,650 known source/local identities** found no known
component exclusions. Two new recordings, Memo 1576 and 1886, contain identical
audio SHA256 `f6fb55909fa342bc84ace1d1c3cab412de493aad60f650af1db46549ca69b165`.
They share one bootstrap component: **128 recordings / 127 components**, before
alignment and annotation missingness. Artist names are absent. This extends
same-corpus recording coverage and does not establish artist or domain
independence. All nonpilot Memo was reserved from the other new research.

Inventory-count correction: execution included all sixteen source-version files
(the original thirteen plus three pilots). The original plan's shorthand said
thirteen; current prose is corrected. Frozen plans/source archives/receipts are
unchanged, and no cohort, identity join or decision rule changed.

Six exact intervals cover [15,75), separately prepared historically and in
Chromium 148.0.7778.96. No padded or normalized audio. Independent validation
on an old DEAM clip reproduced all 27 stored acoustic values exactly; a complete
old Memo pilot check covered all 12 prepared chunks successfully. Both checks
ran before any selected VA outcomes were opened.

Eight sources end before 75 seconds and were excluded without padding or
backfill: Memo 638 (47.967 s), 2114 (66.038), 1052 (30.041), 2193 (60.029),
1399 (73.639), 1235 (70.452), 1625 (73.608), 526 (60.029). Independent checks
verified their audio hashes and stereo channels, establishing short duration
as the cause. All remaining **120 recordings / 119 components** have every
required annotation timestamp. No blank trajectory was converted to zero.

## All mapped results

Each triple is **current / prior / shared**, on the same 120 rows. NDCG uses
the top ten of this single external holdout; lift subtracts its reference mean.
The named priors are original DEAM native VA heads for Cruise, Emotify combined
Relax, PMEmo embedding Uplift and PMEmo attributes Energize. Priors keep their
original coefficients. Shared scores retain their six-label normalization.

| Preparation | Vibe | NDCG: current / prior / shared | Lift: current / prior / shared | MSE: current / prior / shared | Rule outcome |
|---|---|---|---|---|---|
| Historical | Cruise | .79342 / .75260 / .68913 | .03909 / .00061 / -.05487 | .01511 / .01777 / .14240 | Inconclusive |
| Browser | Cruise | .80441 / .76966 / .70073 | .05638 / .00139 / -.04687 | .01571 / .01753 / .11987 | Inconclusive |
| Historical | Relax | .92677 / .90793 / .78939 | .10723 / .09205 / .01757 | .01101 / .00898 / .19448 | Inconclusive |
| Browser | Relax | .92133 / .90640 / .76783 | .10723 / .09040 / .00519 | .00926 / .00843 / .18537 | Inconclusive |
| Historical | Uplift | .86301 / .87623 / .90350 | .11575 / .12061 / .11890 | .02788 / .01940 / .17283 | Regression flag |
| Browser | Uplift | .88027 / .87769 / .90331 | .11575 / .12061 / .11890 | .02983 / .01909 / .18773 | Regression flag |
| Historical | Energize | .90310 / .75195 / .88198 | .18326 / .13181 / .20728 | .01236 / .04847 / .08852 | Regression flag |
| Browser | Energize | .90371 / .89853 / .90489 | .18326 / .20273 / .20728 | .01400 / .04227 / .09948 | Regression flag |

Cruise improves prior NDCG by .04082 historical / .03475 browser, but paired
95% component intervals are [-.06750,.18519] / [-.06941,.15987]. Relax gains
.01884 / .01494, below .02, with intervals [-.03037,.03629] /
[-.03147,.03710]. Relax beats shared ranking in both preparations with positive
lower bounds, but confirmation required both controls.

Uplift exceeds prior mapped-MSE loss by .00848 / .01074, with intervals
[.00468,.01220] / [.00703,.01419]. Its NDCG against shared falls .04049 /
.02305; those ranking intervals include zero. Energize loses .02402 top lift
against shared in both preparations; its intervals also include zero. The
point-estimate regression flags follow predeclared limits and do not establish
universal or independently replicated regression. No rollback is selected here.

The report retains all 1,000-draw paired NDCG/lift/MSE intervals, MAE,
calibration bins and native VA MSE/MAE for every supported expert/control.
Native emotion accuracy is unsupported by Memo VA. Large mapped MSE differences
against shared competing probabilities do not establish ranking benefit.
These references do not supply Focus, Release or listening-benefit labels.

## Exposure, resources and verification

Predictions were sealed **2026-11-01 17:54:40.346 UTC**; selected targets were
first opened **17:55:02.385 UTC**. All eligible outcomes are now exposed for
future work; preserve this evaluation reservation and do not recycle it into
fitting, mixture selection or calibration. Unknown artist aliases and encoder
pretraining overlap remain unresolved.

Extraction took **1,205.06 s**, producing **1,428 new unique prepared chunks**
and reusing the duplicate pair's 12 chunks. Full coverage is 1,440 prepared
recording chunks. Feature caches occupy **13,226,747 bytes**. Replay of all
frozen models took **.101 s**. Python peak RSS **560,680,960 bytes**, largest
child **412,057,600 bytes**; these are individual peaks, not measured aggregate
RSS. Both are below 1 GiB. Temporary range/PCM workspaces are absent. Runtime:
Python 3.14.8 / NumPy 2.4.6 / Essentia 2.1-beta6-dev; complete runtime and assets
are recorded. Five receipts cover acquisition, features, predictions, outcome
report and independent rejected-source alignment audit. Three focused checks
and the frozen-cohort, acoustic parity, old-pilot preparation, alignment audit
and final actual-evidence investigations pass.

Frozen directory `local/memo-confirmation-710-20261101/round-1/`, plan SHA256
`56ee53e80c2c51fe5e02fe88edea13907b3372165a6ad93b674395e0584a743f`.
Exact artifacts, shared baseline, named priors, native aggregation, mappings,
decision limits, source archives, runtime and inputs are retained there.

```sh
./experiments/run.py memo-confirmation verify
./experiments/run.py check --test test_memo_confirmation.py
./experiments/run.py investigate memo_confirmation_710.py -k completed
```

Source-provenance correction found during #708: the original freezer captured
imported helpers but omitted this CLI's `__main__` entrypoint. The current
`memo_confirmation.py` is separately preserved under `postrun-source/`, with
its hash, capture time and original plan hash in `postrun-source.json`.
This is post-run reproduction code, not a prospective entrypoint hash. Original
plans, archived helpers, inputs, prediction/target-opening order records and
measured outputs remain unchanged. The freezer is fixed for future rounds.
Verification now covers six receipts, including the additional post-run source
record; the source-freeze gap remains an explicit limitation for independent review.
