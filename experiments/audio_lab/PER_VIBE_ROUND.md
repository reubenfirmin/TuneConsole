# Independent selection for each vibe, round 2

This extends #530 under the user's 29 October instruction. All source edits,
inputs, fits and reports stay under `experiments/`. Earlier models, scores,
reserved labels and consumer results remain unchanged. The approved entrypoint
is `./experiments/run.py specialists`. Isolated experiment execution may use
`--independent-execution` under the user's existing authorization; normal pytest
continues to use its machine lock.

## Question

Can choosing the source, learner and regularization separately for each vibe
improve within-vibe ranking over the current Emotify procedure, while allowing
any vibe to retain that procedure?

The first round allowed different mixture weights for each vibe, but selected
the source subset and regularization using an objective averaged across vibes.
This round removes that selection constraint. It also measures target-specific
model selection on external human annotations without developer vibe labels.

## Evidence and freshness

The earlier 55-song cohort is an exploratory development diagnostic. Its outer
folds remain artist-separated, but previous inspection of its labels and results
prevents calling this prospective validation. The 20 reserved additions and
their existing scores are excluded from this round's fitting, selection and
scoring. They are not a fresh confirmation set. All 76 frozen local identities
remain protected from new native source fitting.

External native annotations remain the principal independent human evidence.
Native emotion/activity accuracy and six-vibe suitability are separate claims.
No ready audio-backed corpus supplies direct independent labels for all six
product vibes. A new independent listening/reference evaluation remains required
before adopting a selected specialist. Regular users are not asked to rate vibes.

## Fixed experiments

1. **Native target specialists.** Use supplied-audio Emotify, original-vote
   CAL500, PMEmo and DEAM. Exclude protected local artist/title identities before
   fitting. Compare a ridge regularizer chosen jointly across native targets
   with source-specific models whose learner and parameters are chosen separately
   for each native target. Five outer and three inner group folds select by
   native MAE. Keep reading, studying, work and exercise as separate targets.
   Artist, recording and known audio-duplicate groups remain intact.
2. **Independent vibe specialists.** Reuse the thirteen protected native experts
   frozen in the first round, plus raw embeddings and the four new native
   specialist representations. Fit independent suitability heads. Each vibe
   chooses its own source, learner and parameters from training-fold predictions.
   New specialist scores are not normalized across the six vibes.
3. **Controls.** Include the current Emotify procedure as a candidate for every
   vibe. Its native head is frozen; its six-vibe calibration is refitted only on
   each training partition, with regularization selected inside that partition.
   Applying the product's full 55-song calibration to those same held-out songs
   would be leakage and is prohibited. Compare independent AP selection with
   one shared choice selected by macro AP, independent balanced-loss selection,
   and a conservative independent selector.
4. **Conservative selector.** Retain the incumbent unless the training OOF AP
   gain is at least 0.02 and the candidate improves AP in at least two evaluable
   inner folds. This is a predeclared practical sensitivity control, not a
   confidence estimate or a guarantee of improvement.
5. **Reproducible export.** Export each full-development selected head with its
   feature source, model, column, selection record and dependency hashes. Replay
   serialization independently on actual saved embeddings. Measure artifact size
   and inference cost. Do not export into the application or fit a new model to
   the old reserved labels.

## Model grid and scoring

Use ridge, bounded random-Fourier RBF ridge and nearest neighbors for native
human targets. Vibe candidates also include independent binary logistic heads.
The random feature map uses a fixed seed, 64 features and training-only input
scaling; it bounds kernel work on the larger native datasets. The exact grid,
splits, input vectors, source/annotation hashes, dependency versions and research
source are frozen by `plan` before any fit. No grid expansion follows inspection
of this round's scores.

For vibes, primary selection uses training OOF average precision. Ties prefer
the incumbent, then a simpler model with stable ordering. Balanced binary loss
selection is a separate control. Report per-vibe AP, AUC, prevalence and top-five
selected fraction, rather than using aggregate acceptable-choice NLL to decide
every vibe together. Unselected historical alternatives are incomplete comparison
labels, not explicit unsuitable-intrusion judgments. Class-balanced outputs are
not calibrated population probabilities.

Native reports retain per-target MAE and correlations. Paired group intervals
describe fixed OOF predictions and exclude repeated-selection uncertainty; they
are exploratory and not simultaneous discovery guarantees. Report all selectors,
fold choices, failures and negative results. A post hoc best head on outer or old
reserved results is not adopted.

## Execution and decision

`plan` freezes immutable arrays, metadata and source before fitting. `run` resumes
only matching code/input provenance and validates stage receipts. It runs one
native dataset or local stage at a time with one numerical thread. Partial
results do not become completed receipts. `report` reads retained results without
fitting or rescoring the first round.

An apparent specialist gain justifies a frozen candidate and a targeted new
validation packet. A negative or unstable result retains the incumbent for that
vibe. Complete-mix, shared-intensity, propagation and release acceptance still
follow independent quality evidence; changing one vibe can also affect Radio's
derived intensity. This round does not change the product or parent tickets to
Done.
