# Joint vibe pooling

The joint prediction candidate remains inactive. Product reads retain the
incumbent cohort policy. A successful fit does not qualify a model.
Storage/publication contracts belong to
[FAO owners](../../retained_copy/FAO-OWNERS.md#shared-vibe-model-candidate-819).

The current Gaussian candidate jointly estimates baseline, canonical artist,
specific genre, artist/genre interaction, album and measurement-source effects.
One original recording contributes once. Suitability, mean uncertainty and
new-track variation remain distinct. Marginal maximum likelihood estimates
variance components; the conditional posterior omits uncertainty in those
estimates. Shared classifier bias and true between-track variation cannot be
identified separately from measurement noise by this holdout.

## Development decisions

Nov 6, 2026: the source/support-mixture development recipe reduced combined
six-vibe measurement MAE from .09744 to .08342 (14.4%) and achieved 90.3–93.8%
coverage for nominal 90% predictive intervals. Reject adoption: ranking exceeded
the frozen .01 NDCG@10 regression limit for known-artist Focus and unseen-artist
Relax, Cruise, Energize and Release. A full masked-metadata unit offset retained
the same five failures. An explicit no-group fallback alone also left ranking
failures. These reused measurements are development evidence.

The learned masked-metadata covariate completed all 60 tasks on 3,334 originals,
excluding the 115 current positive human targets. Point macro MAE improved from
.09570 to .08099 (15.4%); 90% interval coverage ranged from 90.2% to 94.3%.
Reject adoption: unseen Relax NDCG@10 fell .67519 to .61194, Cruise .65321 to
.60534, and Energize .56310 to .51400. All known-artist ranking gates passed.
Fits took 2.08–7.06 seconds with 44.2–47.7 MB of accounted working data; this
does not measure process RSS or foreground performance. No bootstrap confidence
claim or operational acceptance follows from this failed development recipe.
Its human exclusions and refreshed snapshot differ from the earlier campaign,
so these results do not isolate the covariate's causal contribution.

Learned-covariate plan SHA256:
`372c95964b8ca90e7b8d0c76dfb62b702cd45cf713f06137e8fe8cdf8fc17c53`.
Predictions, source-specific errors, ranking diagnosis and nineteen hash-verified
source files are retained in
`build/test-reports/implementation819-learned-metadata-development-20261106/`.

The richer ten-dimension/support covariate recipe also completed all 60 tasks:
macro MAE .09570 to .08075 (15.6%), interval coverage 90.5–94.1%, fits 2.96–8.52
seconds and 52.2–56.3 MB of accounted working data. Reject adoption: unseen
Relax, Cruise and Energize rankings still failed. Their NDCG@10 values were
.58383, .59609 and .45874, respectively. Human development results were mixed:
unseen observed-positive top-choice coverage rose 31.8% to 41.2%, while coverage
among the first three choices fell 82.4% to 55.3%. This is descriptive evidence
on previously inspected labels, not an independent human gain. The 30 reserved
identities remain unscored. Measurement plan SHA256
`7c3ccd9af725dcaa8039840eb5cdd16bd28c093f91cde8489f734262e7c47a8a`;
human protocol SHA256
`9ee8b92ff3d3cafd42fed207421aa1a7b17da2bdabf62a7252b40611aadf896a`.
Reports and exact sources are under the corresponding
`build/test-reports/implementation819-descriptors-development-20261106/` and
`implementation819-descriptor-label-development-20261106/` directories.

A current-input audit found 70 audio originals with usable provider evidence,
excluding all positive human targets. Recomputed provider evidence and the
audio originals have matching exact versions. Mean provider-minus-audio
differences include +.3581 for Focus and −.0751 for Cruise. These differences
cannot identify which source measures true suitability. The next research
likelihood uses one correlated block per recording, with a shared track
residual and separate source residuals. A pair must never count as two songs.
Capture and source-specific differences are retained in
`build/test-reports/implementation819-current-provider-pairs-20261106/`.

Reject adoption of the paired-source recipe with empirical global source
proportions. It completed 58 of 60 tasks; two fits deferred at the unchanged
ten-second limit. Unseen Cruise still failed ranking (.65321 to .60534).
Incomplete known-artist cells cannot establish acceptance. Frozen plan SHA256
`8cd4d6e7cf246ffc374d2c4fef33b0ba7afeb45a84387e95b1c4c3503dcc6063`;
reports and the exact source archive are in
`build/test-reports/implementation819-paired-source-development-20261106/`.

The conditional-source recipe additionally estimates measurement-family
assignment from genre and connected artist clusters, using an artist-correlated
beta-binomial likelihood with marginally fitted pooling. Source choice enters
that separate likelihood once per original recording. Within-recording pairs
remain correlated in the suitability likelihood. New-source mixture proportions
therefore depend on admissible metadata rather than target measurements.

Reject adoption: all 60 tasks completed, with macro MAE .09570 to .08402 (12.2%)
and overall interval coverage 87.0–92.4%, but known Focus ranking fell .73692 to
.70476 and unseen Cruise .65321 to .60534. Source-specific coverage remains
poor despite acceptable pooled coverage. Fits took 5.06–9.41 seconds with
50.5–53.1 MB of accounted data. This does not establish RSS, source-free human
suitability or operational acceptance. Frozen plan SHA256
`affdae7cce3ea47fa06c7aca67ad36f80f7337d277d9a1569ced00ab64455ebf`;
reports, target-identity comparisons and exact source pins are in
`build/test-reports/implementation819-conditional-source-development-20261106/`.

The current product-format candidate's separate categorical evaluation excludes
all human recordings from fitting. It scored 45 known-artist and 85 unseen-artist
development targets. Observed-positive top-choice coverage improved from 24.4%
to 31.1% known and 31.8% to 42.4% unseen, while several within-vibe retrieval
measures regressed. Independent specialist scales are not class probabilities;
unknown unselected labels cannot establish precision or numeric calibration.
This mixed evidence does not support adoption. The 30 new identities in the
[library label inventory](../datasets/library.md) remain reserved.

Frozen categorical plan SHA256:
`87718687a09947deca55259a9b5d1ab64ac0eadd0e4505398e6078350a4db23e`.
Inputs come from manager baseline `17a9e5ff3fe5449988e8903ef6b5ce9e`, schema 107.
Evidence: `build/test-reports/implementation819-label-freeze-20261106d/`,
`build/test-reports/implementation819-categorical-development-20261106/`, and
`build/test-reports/implementation819-ranking-diagnosis-20261106.json`.
These ignored evidence paths are relative to the repository root.

## Evaluation

`./experiments/run.py vibe-pooling-labels` freezes typed categorical judgments,
historical exposure, canonical metadata and fully masked production profiles.
Its `run` and `summarize` actions preserve positive-only label semantics; reserved
predictions require a prospective code/configuration seal. Preserve every plan's
exact bytes and freeze a separate plan after source changes. A seal establishes
identity, not model quality or absence of unrecorded historical exposure.

`./experiments/run.py vibe-pooling-metadata` tests a learned full masked-metadata
covariate, artist-nested albums and separately fitted audio/provider residuals.
Support predicts residual variance with either sign; it is not assumed to be
calibrated precision. Equal-tail source/support-mixture intervals retain real
between-track variation. This research representation is never passed to
product publication. All 115 current positive human targets are excluded from fitting and
measurement evaluation, leaving 3,334 originals and preserving the new reserve.
Integrating such a covariate would also require exact profile-input identities
and invalidation on profile changes in the shared generation owner; the research
driver does not establish that runtime contract.

`./experiments/run.py vibe-pooling-descriptors` retains individual dimensions
and supports as covariates. `vibe-pooling-transfer` binds that candidate to the
typed categorical inventory and requires an exact prospective seal for reserved
predictions. `vibe-pooling-pairs` tests within-recording source calibration from
the separately captured, current-input provider/audio pairs. None publishes a
product generation or qualifies a candidate automatically.

`python3 scripts/test-library.py run --baseline BASELINE --report REPORT --
-m experiments.audio_lab_tools.vibe_pooling_operations /reports/operations.json`
rehearses the product-format candidate on a disposable library snapshot. It
reports bounded background progress, shared generation storage and paired SQL
score-read timings. Existing prepared output does not establish fresh-user
startup; incomplete work and unmeasured recovery/resource gates remain explicit.

The measurement gates remain those frozen in `vibe_pooling_evaluation.CRITERIA`:
5% macro MAE gain, at most 2% stratum MAE regression, at most .01 per-vibe/stratum
NDCG@10 drop, 85–95% coverage, adequate artist diversity and a positive paired
artist-cluster bootstrap gain. Lifecycle, storage and foreground resource gates
remain additional requirements. Development summaries never auto-select or
qualify a candidate.
