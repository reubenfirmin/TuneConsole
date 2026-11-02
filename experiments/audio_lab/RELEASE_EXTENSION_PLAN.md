# Release artist extension, #709

Acquire only official moodtheme audio-low shards 70 and 63, combined at most
1.25 GiB received bytes and 30 minutes, no automatic retry or backfill. Verify
each entire tar and every retained track against published SHA256. Preserve
missing/invalid recordings. Download archives live in TemporaryDirectory and
are cleaned on every exit; retain only eligible verified research audio.
Round 1 received HTTP503 for both CDN downloads, zero audio bytes, then exposed
an empty-array dtype bug before fitting. Round 2 corrects that bug and makes one
attempt per archive through the official MTG origin documented in the upstream
download script. Same SHA256, cohort, partition and total 1.25 GiB ceiling; no
new labels or model outcomes informed this recovery. Round 3 also corrects JSON
serialization of NumPy component IDs, found by the empty-acquisition regression
check before any new acquisition or model fitting. The first attempt remains
preserved. No further acquisition fallback is planned for this bounded round.
Official endpoint reference:
https://github.com/MTG/mtg-jamendo-dataset/blob/master/scripts/download/download.py

Metadata-only selection: complete seven native human vote tasks, published
checksum match, outside original Jamendo artist IDs, and no component already
known in all sixteen source-version/local inventories (original thirteen plus
three pilots). Identity joins include recording,
artist IDs, audio SHA256, conservative titles and declared links. Additional
artist/components sort by SHA256 of `release-extension-709-v1/` plus sorted artist
IDs joined by `/`; first floor(half) train, remainder sequestered evaluation.
Never split one connected artist/component across partitions. No target values,
model scores or Soundtracks outcomes select this partition.

Raw annotations were available and parsed in prior audits; identity/completeness
is inspected now, but held-out aggression fractions are not computed until all
predictions and models are sealed. This provides new-audio/model-holdout evidence,
not a fresh annotation population. Do not choose alternate artists after failure.

Use the existing exact center ten-second excerpt rule, mono 16 kHz MusiCNN
200-vector, the previously validated C++ implementation (feature tolerance .0005),
native aggressive positive-vote fraction, ridge alpha1000 and clipping. Reject
audio shorter than ten seconds or duration discrepancy exceeding max(3 seconds,
2% annotation duration). Whole-song votes are approximated by one excerpt;
do not claim the excerpt itself received those judgments.

Compare exact adopted alpha1000 head, matched original-pool refit, and original
plus additional-training-artist refit. Medians/normalization are fit on training
only. No new model grid, power mix or Soundtracks tuning. Exclude evaluation/local
components from both refits; for Soundtracks transfer, exclude its entire connected
population from both pools before fitting. New evaluation artists stay excluded
from every fit. All retained original rows keep their actual native labels.

Evaluate reserved artists as one external holdout, artist/component bootstrap
1,000 draws; Soundtracks anger uses five grouped metric folds, same references,
historical/browser preparations and paired components. Publish MSE/MAE/calibration,
NDCG, capped top-20% lift, all acquired/eligible/rejected IDs and actual artist
counts. Require at least 20 evaluation recordings / 15 components and native
variation. Otherwise retain with inconclusive support. For recommendation require
NDCG >=.02 and lift >=.01 gain over BOTH adopted and matched original controls,
positive paired NDCG lower bounds, no MSE increase >.005, and no Soundtracks
NDCG/lift loss >.01 or MSE increase >.005 in either preparation/control.

Bound extraction/fits/evaluation to one hour and recorded Python/child RSS to
1 GiB. Store exact frozen inputs/code/runtime, acquisition/feature/model/prediction
receipts, train/exclusion traces and outcome opening order. Unknown artist names,
aliases, backbone pretraining overlap and the aggression/anger/suitability gap
remain limitations. No production replacement or live-library writes.
