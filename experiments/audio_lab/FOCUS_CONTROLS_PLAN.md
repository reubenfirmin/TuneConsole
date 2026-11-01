# Focus controlled follow-ups: #637 and #638

Parent #530; follows #615. Research only, with production adoption deferred.
Freeze this document, executable source closure, runtime, inputs and source
checksums before fitting. Preserve every procedure, control and failed attempt.
All populations have been inspected previously; these are development diagnostics.

## Representation series (#637)

Reuse #615's 494 CAL500 recordings, 483 conservative identity components, five
outer folds (seed 61530), and 450 common product-training-disjoint evaluation
rows. Preserve the original Reading and Studying vote fractions. Train on all
outer-training rows, evaluate on common held-out rows. Report each activity and
their fixed equal mean. Historical and browser inputs remain separate; all fits
use the historical input. One excerpt per CAL500 recording means no temporal
aggregation claim is possible. Reproduce #615's direct predictions exactly.

Fit all nine native Emotify outputs on its 400 recordings. Select each native
head by three-fold grouped MSE from mean and ridge alpha .1, 1, 10, 100, 1000.
Exclude complete global components of every CAL500 validation set from native
training, including mapping inner folds and mapping-training cross-fit folds.
No CAL500 activity is a native emotion target.

Three procedures: nine clipped emotion predictions; the 200-dimensional
embedding residual orthogonal to all selected linear emotion slopes; and their
concatenation. Controls: direct embedding; nine unclipped emotion predictions;
nine training-only centered PCA coordinates; one fixed orthonormal random
nine-dimensional projection (seed 63730); and training mean. PCA uses raw
embedding units. The random projection is independent of all data and labels.

For residuals convert standardized ridge slopes back to raw embedding units,
take the SVD span with relative tolerance 1e-10, and subtract the centered
embedding's projection onto it. Constant heads contribute zero slopes. Center
on the permitted Emotify training mean. Store the rank and basis. Residuals
remain in original embedding coordinates across cross-fit models; do not mix
rotating reduced coordinates. Unclipped emotions plus residual retain the
linear embedding information when the selected slopes span their stated rank.

Select each activity map by grouped three-fold MSE from the same mean/ridge
grid. For predicted representations, each inner-training feature is itself
cross-fitted; fit permitted native heads for inner validation separately.
After selection, cross-fit all outer-training features and fit final native
heads for outer validation. PCA is fitted on each mapping training set only;
its coordinates must be consistent between that training set and its validation.
Every standardizer is training-only. Native outputs and final activities are
clipped to [0,1], except the explicitly unclipped emotion control.

Primary contrast: emotion plus residual versus emotion. A promising signal
requires browser Focus NDCG gain >= .02, positive conditional 95% paired
component-bootstrap lower bound, lower native MSE, and historical improvement
in the same direction. Residual versus mean establishes residual predictive
signal; direct, PCA, random, and unclipped comparisons diagnose alternatives.
Report all eight methods and all controls, not just a winner. Use 1,000 paired
bootstrap draws stratified by outer fold. Intervals condition on fitted models
and reused data; secondary comparisons are descriptive, not independent
confirmatory tests. Do not attribute a gain uniquely to semantic compression:
regularization, clipping, transfer from Emotify, and cross-fit distribution
differences can all contribute. No production selection follows automatically.

## Task and listener series (#638)

On exactly those CAL500 folds compare separate Reading/Studying heads with a
single pooled mean-activity head and a shared-slope head with task-specific
training-mean offsets. For the offset model add offsets to the unbounded ridge
output, then clip activities. Select pooled by mean-activity MSE and offset by
two-activity MSE using the same grid and training-only offsets. A useful
task-slope signal requires separate-head mean two-task MSE improvement >= .0005
over offsets and a conditional paired 95% interval below zero. Report native
metrics and fixed Focus mean; this does not establish user-specific benefit.

Parse individual CAL500 responses. Compare Yes versus No liking and familiarity
within recordings containing both known strata; No_Opinion and Uncertain are
unknown, never negative. Give each recording equal weight and bootstrap global
recording components (2,000 draws). Permute context labels within recording
(2,000 draws), retaining paired Reading/Studying outcomes. Report all four
two-sided tests with Holm correction. Audit partial uid coverage explicitly;
annotation filename suffixes are response numbers, not listener IDs. These are
same-response observational associations, not causal concentration effects or
stable listener histories. Unknown cross-recording listener dependence remains.

Use the retained Moods and Activities tags.csv only for a separate annotation
diagnostic. Its intellectual activity is broader than Reading/Studying. Collapse
repeated user-track pairs by averaging the nine activity judgments. Activity
zero means not selected in the listened-example task. Exclude searches, word
associations, mood-only consistency checks and demographic enrichment. There is
no verified audio/excerpt join, so this is not another audio Focus benchmark.

Compare track/population probability, a generic listener adjustment, and an
intellectual-specific listener adjustment. Use beta-style smoothing of each
training track/user's fractional endorsement count toward the training global
mean, with fixed 20 pseudo-observations. Add to the base track logit either the
user's other-eight-activities logit deviation from their training global mean,
or their intellectual logit deviation from the training intellectual mean.
Unseen users have zero adjustment; unseen tracks use the global mean. This
tests endorsement propensities, not personal musical ranking.

Main five-fold split holds out artists; a second holds out listeners; a third
holds out deduplicated user-track pairs (seed 63830). Report all rows and users
with at least five prior distinct training tracks separately. Repeat with
smoothing 1, 5 and 100 as fixed sensitivity checks, with no outer winner
selection. Main measure is paired squared-error/Brier improvement. For primary
artist-split contrasts use 1,000 crossed Poisson listener/artist bootstrap draws.
For 199 shuffled controls permute user IDs within each track, retaining each
row's complete activity vector, then refit all artist-fold models at smoothing
20. Report permutation p-values for generic versus base and specific versus
generic. These diagnostics do not prove music preferences or causal benefit.

HelpStudy is a support audit only: count participants, tasks and complete
concentration/distraction outcomes from the original episode table/codebook.
Only 18 episodes have completed outcomes in the audited release. Preserve blank
responses as unknown; do not fit a task-person interaction on that support or
assign episode outcomes to individual songs. Report this evidence gap.

## Execution and verification

Run numerical work serially with one BLAS thread, through experiments/run.py;
experiments and focused checks never acquire the global-suite test lock. Save
fold models, training/validation identities, nested selections, predictions,
paired comparisons, audits and receipts. Verify component separation at every
nesting level, direct-baseline identity, independent model replay, raw-vote
aggregation, missingness, repeated-pair grouping, cold-listener fallback,
training-only preprocessing, orthogonal residual arithmetic and independent
metric arithmetic. Fit no live database and change no production code.
