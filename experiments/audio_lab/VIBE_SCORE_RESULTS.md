# Independent Cruise score decision (#572)

29 October 2026. Cruise remains a candidate for **selected-vibe ranking**.
Neither these scores nor the old 0.65 threshold establish a strong match.
Keep overall classification and the other five production scores unchanged.
The source-only calibrator passes the predeclared mapped-rating error gates,
but brings no ranking gain and little transfer benefit; it remains experimental.
Product enablement still requires the preparation/listening work in #573–574.

The [consumer contract and frozen protocol](VIBE_SCORE_CONTRACT.md) inventory
normalization, threshold/margin, argmax, cached affinity, propagation, intensity,
annotation, readiness and UI consumers. `vibe_score_contract.py` demonstrates
independent fits, multiple matches, missing evidence and abstention, retaining
the exact separate shared vector/classification. No production source, model,
schema or Radio control changed.

## Procedure and custody

```sh
./experiments/run.py check --test test_suitability_contract.py
./experiments/run.py score-contract freeze
./experiments/run.py score-contract fit
./experiments/run.py score-contract evaluate
./experiments/run.py score-contract verify
```

The new directory is `local/mapped-vibes-20261029/score-contract-1/`. It preserves
input files, exact old comparison receipts/artifacts, model source identities,
native identity audits, the written protocol, code archive, runtime, grouped fit
traces, coefficients, predictions, evaluation and checksummed stage receipts.
Previous rounds and packages remain intact. Every research stage uses the
normal machine test/build lock and one numeric thread, without bypasses.

- Plan SHA256: `10f8685f515e37f3ab1ac5636749a4e4502726ae6f3301dc36ca9a943793b96a`
- Fit receipt: `394f74cbb10da124ba9556ade290b18f952a91bb6ec1f663b2a3ffb75205886f`
- Evaluation receipt: `493a6617d8de6c154d688b193dc3f8c1332763a81ebc0bec0b9a3130451922b5`

ManyMusic natural supplies 186 calibration recordings / 164 identity groups.
Five fixed outer folds contain three grouped inner folds for family selection.
Every inner/outer calibrator fits only its training partition. All five outer
folds select nonnegative affine calibration. The final source-only fit is
`0.2784114209629262 + 0.4509112928896128 * raw_score`, bounded to [0,1]. Transfer
uses that single fit, without selecting on either target's labels.

Transfer retains 95 generated ManyMusic recordings / 88 groups and 63 Memo2496
recordings / 62 groups. No additional known source-identity exclusion was
needed. Both native expert training cohorts were audited against every pilot;
no recorded match was found. Unknown artist identities, aliases and backbone
training overlap remain unresolved. These previously examined corpora estimate
a frozen follow-up conditional on earlier model/source selection, not a fresh
independent discovery benchmark.

## Full-capture comparison

NDCG averages the five existing folds, ranking the top 20%, capped at ten rows
per fold, with ties averaged. Source calibration results are nested out of
fold; target results use the source-only final fit.

| Domain | Checkpoint | Refreshed calibration | Refreshed both | Raw Cruise | Calibrated Cruise |
| --- | ---: | ---: | ---: | ---: | ---: |
| Natural source | 0.6669 | 0.6544 | 0.6569 | 0.8358 | 0.8358 |
| Generated transfer | 0.7022 | 0.6916 | 0.6646 | 0.8943 | 0.8943 |
| Memo transfer | 0.8156 | 0.8100 | 0.7947 | 0.8646 | 0.8646 |

The positive affine fit preserves within-fold ranking. Both raw and calibrated
variants pass the predeclared ranking-candidate gates. Paired group-bootstrap
95% intervals for gain against checkpoint are [0.0927,0.2373] on source,
[0.1015,0.2735] on generated and [-0.0374,0.1334] on Memo. Memo's gain remains
uncertain. The 1,000 draws resample whole groups within fixed folds; intervals
condition on fixed models, selection and prior examination.

| Domain | Raw MSE | Calibrated MSE | Raw MAE | Calibrated MAE | Raw rating-bin gap | Calibrated gap |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Natural source | 0.026557 | 0.017154 | 0.129926 | 0.103700 | 0.090932 | 0.012678 |
| Generated transfer | 0.012432 | 0.013367 | 0.088200 | 0.094015 | 0.035658 | 0.044486 |
| Memo transfer | 0.017597 | 0.017595 | 0.112092 | 0.113572 | 0.064633 | 0.082042 |

The source MSE gain is 0.009403, conditional 95% interval [0.006290,0.012664].
Generated MSE worsens by 0.000935, inside the frozen 0.005 tolerance; Memo is
effectively unchanged. Transfer MSE-gain intervals contain zero. Transfer bin
gaps and MAE worsen, so passing these particular gates does not show universal
calibration. A training-mean baseline already achieves nested source MSE
0.017951, but loses ranking (NDCG 0.7651); affine retains that ranking.

The full report includes proper errors and rating bins for all three shared
controls. Their relative, normalized scores have a different absolute meaning;
lower mapped-rating error is not itself a fair proof of recommendation benefit.

## Intrusions, thresholds and partial captures

The fraction of top-ranked entries with mapped rating <=0.30 falls from 5% to
2.5% on source, and stays 0% on generated and Memo. **These are low-proxy
intrusions, not listener judgments of unsuitable music.** Only seven source
and two generated rows meet the low-proxy definition; Memo has none, so its
zero cannot establish rejection of unsuitable examples.

No threshold passes, in any outer source fit or the final source fit. The
source has only 16 high-proxy examples (rating >=0.75), generated has 12 and
Memo nine. These small slices cannot support the required count/group/precision
claim. Final calibration selects zero rows at >=0.70; at >=0.60 its inner-OOF
high-proxy precision is only 11.3%. Every product strong-match result remains
unknown. Direct listening-purpose labels and serving-domain/capture validation
are also missing; 0.65 has no privileged meaning here.

| Domain | One actual excerpt MAE | Two actual excerpts MAE | All actual excerpts MAE | Full covered interval |
| --- | ---: | ---: | ---: | --- |
| Natural source | 0.103162 | 0.103860 | 0.103700 | 30 seconds / 3 excerpts |
| Generated transfer | 0.096531 | 0.095273 | 0.094015 | 30 seconds / 3 excerpts |
| Memo transfer | 0.122824 | 0.120724 | 0.113572 | 60 seconds / 6 excerpts |

Each excerpt is ten actual seconds. The full report stores actual intervals,
union duration and annotation-interval completeness per row, and compares raw,
calibrated and all shared controls at the same coverage. No missing interval is
padded or claimed observed. Partial predictions target the full annotation
interval only as a missing-coverage stress check; they do not establish accuracy
for the unheard interval, whole recording or normal playback capture schedule.
All partial strong-match flags remain disabled. Zero capture is unknown.

## Validation and handoff

Focused checks passed **24 in 0.94 seconds**. They include held-out outcome
perturbation with unchanged own-fold predictions/thresholds, grouped inner/outer
separation, a source-only fit with no transfer/local outcomes present, weighted
isotonic ties, conservative group threshold support, duplicate/partial/missing
coverage, multiple matches, exact preservation of the shared vector and receipt
mutation rejection. Those checks use controlled fixtures; external numerical
results use the real frozen supplied-audio annotation cohorts above.

All three shared controls and Cruise reproduce the saved full-capture baseline
within 1e-12 absolute tolerance. Partial Cruise batch inference also matches the
strict scalar runtime on each domain/capture variant. Input/code/output receipts
verify. Syntax parsing and scoped whitespace checks pass. Runtime is Python
3.14.8, NumPy 2.4.6, SciPy 1.18.1 and threadpoolctl 3.7.0.

The already consumed #570 eight-recording local check remains supplementary;
this round does not reread its labels for fitting or selection. No new
actual-library, UI, live provider, fresh startup, listening, release or full-suite
acceptance is claimed. #573 must resolve prepared affinity buckets, mixed
fallback scales, versioned propagation and bounded rebuilding before adoption.
#574 retains blind listening and actual quality/resource gates. Relax, Energize
and Uplift retain current models; new Focus/Release remain in shadow.
