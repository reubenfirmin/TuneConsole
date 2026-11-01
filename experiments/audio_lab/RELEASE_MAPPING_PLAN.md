# Release mapping follow-up #614

30 October 2026. This plan precedes model fitting. Preserve the #577 round and
its exact Jamendo candidate. All new work is under `experiments/`.

The user clarified that experiments do not use the global test-suite lock.
`round-1` preserves the initial, unfitted freeze. `round-2` repeats that freeze
with the lock removed from execution and focused research checks; candidate
procedures, inputs, folds, targets and acceptance criteria are unchanged.

## Questions and fixed targets

Keep explicit Jamendo aggression fractions, Emotify induced power fractions and
Soundtracks anger ratings as three different native references. No VA-only,
tempo, genre or energy rule defines Release. Count annotations and usable audio
separately, retaining rater disagreement and whole-song/excerpt differences.

First audit the original votes and supplied-audio inventory. The raw Jamendo
file has 11,565 annotation records; the existing verified audio cohort has 216.
Inventory unused human-labelled audio and artist overlap without choosing
records by candidate scores. This round uses existing features. Do not expand
acquisition or spend a new holdout to compensate for an unsuccessful mapping.
The audit must identify a concrete bounded next acquisition if confirmation
is still missing. No new user listening collection is part of this work.

The semantic diagnostic uses jointly observed attributes, excluding the target
and all its defining columns:

- Jamendo aggressive: happy, party, relaxed, sad.
- Emotify power: joyful activation, tension, calmness, sadness.
- Soundtracks anger: energy, tension, valence, happy, fear, sad, tender.

Within original grouped outer folds, fit an ordinary native-MSE mapping from
those human attributes to the fixed native target; compare predictions using
observed attributes with the **same mapping** using audio-predicted attributes.
The latter heads are fitted using outer-training rows only. Select the mapping
inside training folds from mean and ridge alpha .1, 1, 10, 100, 1000. This is a
diagnostic of information and prediction loss, not a fourth deployable candidate
or a proof of direct product suitability. An observed-label association cannot
establish that anger, aggression and power are interchangeable.
This linear diagnostic is not an information upper bound: a weak result does
not rule out other label relationships, and applying an observed-label mapping
to noisy predicted inputs does not estimate the best possible end-to-end model.

## Three bounded procedures

1. **Anchor:** exact prior #577 Jamendo embedding head for the primary
   Soundtracks transfer. Retain the source-only native-MSE procedure as the
   anchor for new source OOF and reciprocal comparisons. Reuse all exact
   historical/shared/refreshed/browser controls on the original common rows.
2. **Monotone mapping:** combine two separately predicted native attributes.
   For Soundtracks transfer these are Jamendo aggression and Emotify power;
   select the power weight from 0, .25, .5, .75, 1 using Jamendo training
   aggression MSE only. Fit a nonnegative affine calibration, slope bounded
   to [0, 4], on training cross-fitted inputs. Retain an independently
   reported weight-zero calibration control. All base learners, mapping
   selection and calibration are nested inside source training folds; a
   held-out mapping row cannot contribute to either base training or
   calibration. No target labels choose weights or calibrate scores.
3. **Source-balanced native tasks:** ridge model with a shared coefficient
   vector plus a separate deviation and intercept for each native target.
   Each source contributes equal total MSE weight, irrespective of its row
   count; unobserved labels contribute no loss. Penalize the shared vector
   and each deviation equally. Select regularization from .01, .1, 1, 10
   by three-fold, source-balanced native MSE. Export distinct native heads;
   the Release output remains the declared anchor head, not an average of
   incompatible target scales. Include a training-mean control.

For corpus transfer hold out each of the three sources completely. Train on
the other two; use Jamendo as anchor whenever present, otherwise Soundtracks.
Thus the main Soundtracks comparison trains on Jamendo/Emotify only. The
reciprocal mappings are explicit separate construct transfers. For source OOF,
each source is its own anchor; its auxiliary source is Emotify, or Jamendo for
Emotify. Fit all other sources on their corresponding outer-training folds.
The native tasks model includes all three sources in this OOF analysis.

All audio heads use the existing source-only mean/five-ridge native-MSE
selector. Use three grouped folds for each nested level, seed 61430. Freeze
original five outer folds and all computed inner memberships before fitting.
No RFF, neighbor search, feature-family search, target-weight fitting or new
decoder comparison is included. Embeddings are the only fitted representation;
the old acoustic/combined procedures remain exact historical controls.
For multi-excerpt diagnostics, average each clipped native prediction across
available excerpts before applying the fixed monotone blend and calibration.

## Identity, controls and uncertainty

Start with #577's protected source pools, excluding local, other sources and
all primary target identity components: Jamendo 187, Emotify 255, Soundtracks
467 rows before any refreshed local exclusions. Refresh local identities
read-only if they changed; exclude their complete components before selection.
Verify mutual source separation, original groups and feature signatures. Pin
the original artifacts and every new input, model dependency, code file and
runtime. Use a new frozen directory; never overwrite earlier receipts.

New native comparisons use identical protected rows and original outer folds.
Compare with a newly source-selected anchor on those same training pools;
legacy OOF on the same validation slice remains a separately labelled control
with its original training population. Source training-mean and median controls
remain explicit. For primary Soundtracks use the original common 470 rows,
subject to any new local exclusion applied to **every** method. Do not call the
467 protected source pool the primary 470-row evaluation.

Report native MSE/MAE, five fixed calibration bins, tie-aware NDCG and top-list
mean/lift. Use 1,000 paired whole-identity-group bootstrap draws within frozen
outer folds, conditional on fitted models and previously inspected corpora.
Natural constructs and their scales are never pooled into one winner metric.
Native held-out results and reciprocal transfers are diagnostics, not fresh
confirmation. Unknown artist aliases and encoder-training overlap remain limits.

Preserve fixed high/low thresholds .65/.35. Soundtracks supports comparisons
of high energy/low anger, high anger/estimated tempo <=90 BPM, energetic happy
low-anger, energetic negative-valence and high anger/low energy. Estimated
tempo is not a human tempo annotation. Empty/small slices remain unestablished;
no slice or local positive label selects a model. Local labels are supplementary
and unselected vibes remain unknown. If current captures have incompatible
features, report the limit instead of silently substituting a feature policy.

## Decision and artifacts

A follow-up candidate must improve primary Soundtracks NDCG by at least .02
and top-list lift by at least .01 over the exact Jamendo anchor, with paired
interval lower bounds above zero for both; it must also beat the incumbent.
For reciprocal native references, point losses above .02 NDCG or .01 lift
against the corresponding source anchor are material and must be explained;
an unexplained material loss prevents a general replacement recommendation.
These thresholds are research decision rules, not established user-benefit
units. Calibration gains alone do not establish better selection. A new
confirmation population is required before claiming a confirmed general gain;
missing confirmation yields retain with a specific next step.

Serialize every fitted transfer procedure, independently replay its heads and
mapping, and measure file size, load time, 100 warm three-excerpt calls and
process peak RSS. Bounds: 256 KiB per single scorer, 512 KiB per two-head mapping,
100 ms warm inference, 1 GiB RSS. Exclude the encoder and application startup
from these Python figures. New Jamendo-derived artifacts remain research-only
under the pinned source terms; they inherit no prior production acceptance.

Deliver an audit, frozen numerical reports, a written diagnosis and explicit
adopt/retain recommendation. No production integration, commit or push is
authorized. #573/#587 and #137 retain their separate scopes.
