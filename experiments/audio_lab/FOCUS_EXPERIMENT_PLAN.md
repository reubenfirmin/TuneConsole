# Dedicated Focus experiment (#576)

30 October 2026. Follow #530's shared protocol. This experiment changes no
production code, model, database or activation policy.

## Question and fixed comparisons

CAL500's original Reading and Studying listener vote fractions directly define
Focus: the equal mean of the two native targets, for predictions and references.
Do not add At-work, calmness or instrumentalness. Keep fractional disagreement.

The prior native MAE-selected two-head export is the named starting candidate.
The earlier broad mapped-ranking search already failed to improve it. The new
hypothesis is narrower: selecting each native conditional mean by squared error,
with bounded embedding/combined features, may improve mapped suitability without
fitting the mapping on observed rankings. Compare these declared procedures:

- Prior native MAE selector, using its retained out-of-fold predictions and exact
  full-source export. Its in-sample export replay is numerical validation only.
- Native MSE selection independently for Reading and Studying, using embeddings,
  or combined embeddings and 27 historical acoustic attributes. Each family has
  five ridge penalties (0.1, 1, 10, 100, 1000) and six 64-feature RFF settings
  (gamma 0.5/2, penalty 1/10/100). Include a training-mean candidate. Select only
  inside three grouped inner folds; retain the original five outer folds.
- A fixed acoustic ridge (penalty 10), training means and training medians.
- Browser-input embedding MSE selection with the same bounded grid/folds. This
  is a separate input domain, not a blend of browser embeddings and historical
  acoustic attributes. The accepted browser cache has no acoustic feature family.
- Frozen product, both #570 refreshed shared controls, and #424's accepted
  browser-prepared shared control, with identical supported recording rows.

The current external cohorts have already been inspected. All new results are
reused-corpus diagnostics; neither a different seed nor another split makes a
new confirmation population. No outer-fold result chooses a new selector or
ensemble. Export each declared procedure, retaining the prior candidate.

## Inputs and exclusions

Reuse the 494 protected CAL500 rows, raw activity votes, historical embeddings,
acoustic attributes, prior OOF predictions and #424 browser embeddings. Pin
recording order, audio/window hashes, feature order, extraction signature,
annotations, groups, folds, artifacts, code and numerical runtime before fits.
Check cached receipts before use. Preserve every previous experiment directory.

Refresh the live labeled corpus with a read-only transaction and copy only
selected captured files under experiments. Keep local outcomes out of all
native fitting and selection. Remove entire known source identity components
anchored to any current local identity before new selection. Reconcile original
outer folds with connected artist/recording/audio groups; fail on leakage.
Evaluate the intersection with #570's shared-training-disjoint mask, and report
all excluded/remaining counts. Keep unknown aliases and backbone-pretraining
overlap explicit. Missing local vibes are unknown, not certified negatives.

## Evaluation and decision

Report native MSE/MAE, mapped tie-aware top-20%-capped-at-ten NDCG, top-list mean
and lift, mapped MSE/MAE, reference/score means and fixed score-bin calibration.
Report same-row paired differences with 1,000 whole-group bootstrap draws
stratified by the original folds. These intervals condition on models, folds and
prior inspection; they exclude retraining/selection uncertainty. Retain every
declared comparison, including losses and ties. Raw shared normalized scores and
independent vote estimates have different meanings; error alone is no adoption
criterion and the old 0.65 threshold is unsupported.

Replay serialized native heads with two implementations, independently check
each head before averaging, record bytes/load/scoring time and peak process RSS,
and declare a 256 KiB artifact, 100 ms warm three-excerpt and 1 GiB process-RSS
budget. These measure the small predictor, excluding the already evaluated
encoder. Keep oversized or failed exports as negative results. Local first/all
capture scores and changes are diagnostics; report human-positive support only
where the label actually supplies it, and separate historical exposure. Never
claim distraction detection from unannotated captures.

Adopt/retain is an explicit report recommendation, never an automatic source
change. Without independently confirmed useful gain, retain the production
shared Focus scorer. A completed no-go includes all comparisons, artifact replay,
resource evidence, local coverage and confirmation-data limits.

## Bounded additional-reference search

Revisit supplied-audio/activity releases first. Acquire the small open
[Moods and activities](https://reshare.ukdataservice.ac.uk/852024/) archive and
documentation; distinguish mood-word associations, nominated-track annotations
and independently listened-to-track judgments. Attempt exact existing-audio joins
before any audio recovery. Also inspect the track identities and actual impact
questions in [Help Me study!](https://zenodo.org/records/10085104); observational
study episodes alone do not establish suitability. Record inaccessible files,
missing audio and zero joins, rather than expanding recovery indefinitely.

[music&reading:Data1](https://zenodo.org/records/17042766) lists two fNIRS
spreadsheets and no audio or per-recording suitability release. The earlier
Music and Cognitive Performance and Music in Use leads remain documented in
DATASET_PLAN.md. No new user listening or pairwise collection is requested.

## Execution

Use `./experiments/run.py focus` for this experiment and the normal shared test
lock for fitting, evaluation and focused checks. Keep detailed evidence in this
report's companion results and #576. #573 and #587 retain their separate
production/output acceptance; this experiment does not repeat those campaigns.
