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

The format-four runtime implementation is under `src/yt_playlist/rec/`:
`vibe_joint_calibrated.py` owns fitting, prediction and mixture quantiles;
`vibe_joint_model.py` owns versioned representation and compatibility;
`vibe_joint_preparation.py` owns masked covariates and guarded publication.
Experiments contain offline evaluators and historical recipes only. The
format-four recipe learns the full masked-metadata covariate, artist-nested
album effects, separate audio/provider residuals and provider support slope.

## Development decisions

The sealed product human comparison consumed the 30 previously reserved
recordings; they are no longer available for candidate selection or independent
reuse. Its known-artist slice has 21 recordings across 21 clusters; unseen has
all 30 across 30 clusters. Within-vibe positives recovered at ten are mixed:
known Focus 0/3 to 1/3, Cruise 5/8 to 6/8, Energize 3/6 to 2/6, Relax 2/2
unchanged, Release 2/3 to 3/3; unseen Energize improves 3/12 to 5/12 and the
other observed-positive counts are unchanged. Uplift has no positive target.
Secondary top-choice coverage falls 9/21 to 5/21 known and rises 7/30 to 8/30
unseen; cross-vibe scores remain independent suitability scales. These sparse
positive-only outcomes do not establish precision, numeric calibration or a
general human gain. Both human-evaluation training sets exclude all 115 human
identities. Runtime fitting uses original measurements, never their annotations.
Protocol SHA256
`58189da68ae45571b6d59c76bc95be2bbf67fbb3edc67970479ee388f18b306b`;
prospective seal SHA256
`76b61d70506374a0827b3c3cf29ccffb67d33277ab07f581277b15c7c141e061`.
All 24 tasks completed using the unmodified product fitter/posterior. Exact
sources, inputs, full results and gaps are in
`build/test-reports/implementation819-product-human-campaign-20261106/`.
Earlier reserve statements below describe the preceding campaigns.

The format-four product integration reproduces the scalar development gain
using unmodified runtime code: all 60 fits complete, macro MAE improves 15.28%,
the paired-cluster absolute-gain interval is [.01281, .01615], and nominal-90%
coverage is 90.38–93.82%. Every numerical development gate passes. Fits take
2.38–6.69s with 44.36–48.91 MB accounted data. This reuses development originals
and does not establish human quality or operational acceptance. Protocol SHA256
`02816a360e20bf16bfb9730b1461e71b28ef93b6c5994e0a27fbc913ae020430`;
reports, exact inputs and unchanged full-source archive:
`build/test-reports/implementation819-calibrated-runtime-campaign-20261106/`.

The format-three product candidate completed its corrected-ranking campaign
with a 14.80% macro MAE gain, but failed coverage (known Focus 95.75%) and
unseen Relax, Focus and Cruise ranking gates. Reject adoption. Protocol SHA256
`9b4f37b4c98a11ffc439ecb8793c58634d58f800edcb72917b379f13afdaca55`;
exact sources and the separately corrected summary identity are preserved in
`build/test-reports/implementation819-runtime-within-model-20261106b/`.
This failure is distinct from the detached scalar recipe's passing development
campaign and requires evaluating the format-four product integration separately.

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

A separate posthoc diagnostic ranks targets within each fitted fold, rather
than pooling predictions from five differently calibrated models into a list
that no product model serves. All twelve conditional-source ranking differences
then exceed −.00403; the paired 1,000-cluster bootstrap gives a macro absolute
MAE gain interval of [.01020, .01291]. This identifies a ranking-estimand defect,
not prospective qualification. Preserve the original failed gates. Diagnostic
protocol SHA256:
`113ab81578f92e8877f85e7a603edf3491f13240914a84282311d56ebe9d5461`.
Its protocol and report are retained beside that campaign.

The simpler scalar recipe also clears all twelve within-model ranking point
comparisons on the reused folds. Its categorical single-model check still
opposes adoption: known-artist Focus retrieval at ten falls from 3/11 observed
positives to 1/11, and unseen Focus from 4/18 to 1/18. Known evaluation has
47 targets across 39 artist clusters; unseen has 85 across 77. Both exclude
all 115 human recordings from fitting. Unseen training additionally removes
every human connected artist component, leaving 2,587 originals. Each stratum
ranks one model's predictions, avoiding pooled-fold calibration differences.
Unselected labels remain unknown; these small reused positive sets do not
establish precision or six-vibe numeric calibration. Preserve the 30 reserved
identities. Protocol SHA256:
`e908e11f5432deb33b71a9779b0e73debb2aeb182b39f1ff54078deacd04ce49`.
Reports and exact sources:
`build/test-reports/implementation819-metadata-human-single-model-20261106/`.

A prospectively frozen corrected-ranking scalar campaign completed 60/60 fits:
15.28% macro MAE gain, paired-cluster absolute gain interval [.01281, .01615],
90.38–93.82% interval coverage and no failed numerical development gates.
Fits took 2.45–7.90s with 44.1–48.7 MB accounted data. Its plan SHA256 is
`83a295bde70030caf9a1e287b7c0e4c2864eb478707145117aaf2589e8ea4fe9`;
reports and source archive are in
`build/test-reports/implementation819-within-model-selection-20261106/`.
This reused measurement result does not resolve the human Focus concern.

The paired single-model human recipe retained 3/11 known Focus positives at
ten but retrieved 2/18 unseen, versus 4/18 for the incumbent. Cruise declined
from 6 to 4 positives in each stratum. A refreshed fixed-offset recipe restored
Cruise retrieval but retained the scalar Focus losses. Preserve the reserve;
neither resolves adoption. Their protocols and sources are in the respective
`implementation819-paired-human-single-model-20261106/` and
`implementation819-offset-human-single-model-20261106/` report directories.
Posthoc paired-cluster intervals on these fixed judged pools remain broad;
containing zero does not establish noninferiority. The exact diagnostic and
limitations are in `implementation819-human-uncertainty-20261106/`.

Adding an exact-source/specific-genre random effect tests shared classifier
bias within a genre. The central-difference fit deferred four of twelve human
tasks at ten seconds. Exact marginal derivatives matched an independent dense
likelihood check but their bounded inverse-column implementation was slower:
all twelve fits deferred. Reject these implementations for adoption; do not
infer complete human quality from the partial outputs. Source archives and
protocols are in `implementation819-source-genre-human-single-model-20261106/`
and `implementation819-source-genre-exact-gradient-human-20261106/`.

Forward finite differences and reuse of identical symbolic sparse geometry
completed all twelve source/genre fits in 2.45–6.79s. This resolves the observed
fit deferrals, not human transfer: Focus retrieval at ten remains 3/11 to 1/11
known and 4/18 to 1/18 unseen; Cruise falls from 6/22 to 4/22 known and 6/35 to
3/35 unseen. Reject adoption and preserve the reserve. The cache contains only
symbolic ordering, rechecks each caller's memory admission and never retains a
posterior or fitted parameters. Protocol SHA256:
`0984b6c6463cdd8f9c3cf8973d169dba89c400cf802c9b48d8bd7021583c412f`.
Reports and frozen sources:
`build/test-reports/implementation819-source-genre-forward-human-20261106/`.

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
The format-four product integration records exact masked profile identities
and invalidates changed source/raw inputs. Its separate runtime evaluation is
required; the research driver's passing result does not establish that contract.

`./experiments/run.py vibe-pooling-descriptors` retains individual dimensions
and supports as covariates. `vibe-pooling-transfer` binds that candidate to the
typed categorical inventory and requires an exact prospective seal for reserved
predictions. Transfer also supports scalar, paired-source, fixed-offset and source/genre recipes;
`--fold-policy single-model` uses one fit per stratum so within-vibe human
retrieval never pools separately calibrated folds. `vibe-pooling-pairs` tests within-recording source calibration from
the separately captured, current-input provider/audio pairs. None publishes a
product generation or qualifies a candidate automatically.

`vibe-pooling-ranking freeze|run` binds the within-model diagnostic to all sixty
existing reports and its own code. It preserves the original campaign and marks
every result posthoc and unqualified. Independent tests compare weighted cluster
resampling against an expanded ranking oracle and demonstrate the cross-model
calibration problem.

`vibe-pooling-selection` freezes a separate development protocol with corrected
within-model ranking and a new deterministic five-fold assignment. It preserves
the original numerical limits and human exclusions, checks every target against
its frozen original, and reports paired artist-cluster gain uncertainty. Reused
originals remain development evidence; no report selects or qualifies production.

`python3 scripts/test-library.py run --baseline BASELINE --report REPORT --
-m experiments.audio_lab_tools.vibe_pooling_operations /reports/operations.json`
rehearses the product-format candidate on a disposable library snapshot. It
reports bounded background progress, shared generation storage and paired SQL
score-read timings. Existing prepared output does not establish fresh-user
startup; incomplete work and unmeasured recovery/resource gates remain explicit.

The bounded warm-library rehearsal preserved existing useful scores across
5,495 foreground SQL requests. Source/content queues did not drain in either
180-second phase; no joint model fitted. Its similar p95 timings therefore do
not establish candidate latency or startup acceptance. Raw results and the
assessment are in `build/test-reports/implementation819-operations-20261106/`.

The final bounded rehearsal froze 1,492 source/configuration files and uses
production request/background priority and the same 100ms request cadence in
both phases. All eight product models became ready in 37.31s; the first
generation-linked scores appeared in 41.53s. Two model cycles published 448
prediction versions referencing sixteen generations. The cache remained at 75,056 rows and
every serving probe retained 600 ranked results and 128 score/overlay results.
Foreground p95 was 64.34ms baseline and 64.89ms candidate; neither foreground
path fitted or loaded a model. Both 180-second phases remained incomplete,
so this is bounded-progress evidence, not the completed-refresh latency gate
or fresh-user startup acceptance. Each fit used all 3,441 originals and
2.74–3.58s, with 55.7–55.9 MB accounted data; process peak RSS was 194.4 MB.
Candidate-phase SQL/index growth was 483,328 bytes, including continuing
measurement preparation, and sixteen retained model files occupied 5,257,869
bytes. Kernel process counters record 225,374,208 written bytes during that
phase; workload progress and cache effects differ, so this does not isolate
model write amplification. Exact sources, I/O counters and database/WAL sizes:
`build/test-reports/implementation819-frozen-idle-cleanup-operations-20261106/`.

Score preparation reconstructs one posterior per bounded worker admission.
Only bounded target estimates and the guarded snapshot survive between turns;
publication and queue acknowledgement occur after the complete batch. Close,
deadline expiry and stale publication release the snapshot and file-reader
leases, leaving the durable queue for retry. Maintenance polling also closes
idle expired batches and batches paused for storage work, serialized with any
active capture. A symbolic geometry cache avoids
repeating ordering work without retaining numerical posterior state.

The separate `experiments.audio_lab_tools.vibe_pooling_artifacts` module uses
the same manager invocation with `/reports/artifacts.json`. It deliberately
bypasses scheduling to test shared storage and independent domain recovery.
On 3,441 originals, one Relax model occupied 334,929 physical FAO bytes; 128
actual targets added compact generation references and no SQL contributor
edges. Unchanged publication reused IDs, corruption was rejected, and model
plus provenance domains restored after producer closure and model removal.
This controlled batch does not establish ordinary preparation, whole-corpus
retention or fresh startup. Evidence and exact sources:
`build/test-reports/implementation819-artifacts-independent-restore-20261106/`.
A conditioning-aware repeat also preserved each target's genre/album receipt
through producer-independent restoration, with unchanged-result reuse and
45,056 SQL/index bytes for 128 targets. Its report and sources are in
`build/test-reports/implementation819-conditioned-artifacts-20261106/`.

The measurement gates remain those frozen in `vibe_pooling_evaluation.CRITERIA`:
5% macro MAE gain, at most 2% stratum MAE regression, at most .01 per-vibe/stratum
NDCG@10 drop, 85–95% coverage, adequate artist diversity and a positive paired
artist-cluster bootstrap gain. Lifecycle, storage and foreground resource gates
remain additional requirements. Development summaries never auto-select or
qualify a candidate.
