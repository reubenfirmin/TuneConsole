# DEAM

1,802 supplied audio recordings join numeric IDs to original static and dynamic
VA labels. Audio mirror revision: `3db39c68bf69609b57cdc31dd46fa9f8fd24970f`.
Normalize native static 1–9 ratings explicitly. Most artist identities are absent;
recording/component grouping does not establish unseen-artist evaluation.

Oct 29 extraction completed 1,801 recordings. ID 2048's 151.91-second source
produces 440,999 stereo measurement frames under timestamp trimming rather than
441,000; its 16-kHz model decode is complete. This is a one-sample boundary
artifact. Preserve the frozen comparisons; any recovered feature needs a separate
audit. Later protected specialist fits retain 1,800 rows.

The feature bundle contains a DEAM-trained VA head: exclude its outputs from
DEAM native-task features and direct pretrained controls. Static/aligned models
provide useful VA transfer, but did not establish a general shared-model upgrade.
The common-target Cruise experiment retains 1,798 source rows and selects a
random-feature ridge head. Later consensus uses ridge native heads under its own
protected folds; these are distinct fits.

Evidence: `local/external-20261028/checkpoint-1/` and
`local/mapped-vibes-20261029/common-target-1/`.
Models: [Cruise](../models/cruise.md), [Uplift](../models/uplift.md),
[Energize](../models/energize.md). Source: [DEAM publisher](https://cvml.unige.ch/databases/DEAM/).
