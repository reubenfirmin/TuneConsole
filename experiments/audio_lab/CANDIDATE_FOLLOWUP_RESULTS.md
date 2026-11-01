# Common-target candidate results

29 October 2026, #530. These exploratory results strengthen the case for
dimensional Cruise supervision and independent activation/aggression heads.
They do not support preferring acoustic-only activation across music domains or
replacing the existing native Focus selector with a larger winner search.

All work stays under `experiments/`. Local vibe labels and stored library
predictions supply no training targets, model selection or quality references.
The [frozen follow-up plan](CANDIDATE_FOLLOWUP_PLAN.md) defines the comparisons.

## Scope and controls

Artifacts: `local/mapped-vibes-20261029/common-target-1/`.
Plan SHA256: `df733b8cd6014e3077a4ee9e43504105d40dbd1e38676f5c0bc10b8413e5fdae`.

This is a bounded extension: **14 common-target transfer directions**, using
188 natural ManyMusic recordings, 95 generated ManyMusic recordings, 63
Memo2496 recordings and 470 soundtrack excerpts. It does not repeat all thirteen
datasets. Eight source/regime fits produce the fourteen directions; ten stages
including Focus analysis and summary take 21.50 seconds on cached features.
There is no new audio extraction. Python 3.14.8, NumPy 2.4.6, SciPy 1.18.1 and
threadpoolctl 3.7.0 match the earlier numerical runtime.

The pilot evaluation covers all 1,227 consecutive ten-second chunks of the
declared rated intervals, but contributes **one row per recording**. Its cache
has 200-value embeddings and lacks the 27 acoustic attributes. All three pilot
sources use the same embedding-only 21-candidate grid. The soundtrack experiment
compares all three feature families with the original 59-candidate grid.
Missing acoustic attributes are not filled with invented values.

Before source-only three-fold selection, whole source identity components
connected to any target artist, recording or audio anchor are removed. Title-only
matches remain conservative row exclusions. Pilot training counts are PMEmo 762,
DEAM 1,798 and Emotify 398; the soundtrack sources retain 766, 1,800, 400, 216
and 900 rows respectively. Exclusions, grouped folds, matrices, rules, extraction
signatures, chunk hashes, source and runtime were frozen before the fits.
Unknown aliases and missing artist identities remain limits.

Each reference is the previously declared fixed rule applied to human external
annotations. Candidate selection uses source annotations only. There is no
target-fitted rescaling, target-dependent source choice or learned mixture.
These corpora have previously inspected results and are not untouched
confirmation data. Comparisons below are descriptive, without transfer-level
significance claims.

## Cruise on identical target recordings

Primary policy: average native predictions over the full rated interval, then
apply the fixed mapping. Cells contain NDCG; compare columns within a row.
The VA ensemble is an equal PMEmo/DEAM average fixed before target scoring.

| Target cohort | Rows | Random | PMEmo VA | DEAM VA | Emotify | Equal VA ensemble |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| ManyMusic natural | 188 | 0.7617 | 0.8290 | 0.8199 | 0.7345 | 0.8358 |
| ManyMusic generated | 95 | 0.7855 | 0.8842 | 0.8113 | 0.7270 | 0.8943 |
| Memo2496 pilot | 63 | 0.8039 | 0.8516 | 0.8150 | 0.8763 | 0.8646 |

Both dimensional sources exceed random ranking on all three cohorts. Emotify
falls below random on both ManyMusic cohorts but ranks well on Memo2496. This
supports advancing dimensional arousal/valence Cruise candidates; it does not
establish a universal source winner.

Absolute scores remain a separate concern. Cruise MSE for PMEmo, DEAM, Emotify
and the fixed VA ensemble respectively is:

| Target cohort | PMEmo | DEAM | Emotify | Equal VA ensemble |
| --- | ---: | ---: | ---: | ---: |
| ManyMusic natural | 0.02803 | 0.02869 | 0.02988 | 0.02738 |
| ManyMusic generated | 0.01367 | 0.01514 | 0.03653 | 0.01243 |
| Memo2496 pilot | 0.01857 | 0.02165 | 0.03374 | 0.01755 |

For example, natural ManyMusic's mean Cruise reference is 0.5788; PMEmo and DEAM
predict means 0.6688 and 0.6746, whereas Emotify predicts 0.4757. PMEmo's MSE is
slightly worse than its source-mean control on natural ManyMusic (0.02768), and
worse on Memo2496 (control 0.01511). Ranking improvements alone do not establish
a calibrated product score or a usable global threshold.

Both predeclared secondary aggregations are retained: prediction from the mean
embedding and mean of chunk-level mapped scores. They are not selected using
target outcomes. For DEAM on generated ManyMusic, primary Cruise NDCG is 0.8113;
mean mapped chunks is 0.8405, and mean-embedding prediction is 0.8202. The
nonlinear moderate-activation mapping makes these different measurements of a
changing interval. One inspected ablation cannot choose a new default.

## Activation and aggression on shared soundtrack references

All scores below use the same 470 excerpts. Energize reference is human energy;
Release reference is human anger. Every model is selected exclusively on its
source. VA-only sources have no Release mapping.

| Source | Selected Energize NDCG | Energize MSE | Selected Release NDCG | Release MSE |
| --- | ---: | ---: | ---: | ---: |
| Random/source-mean ranking | 0.5175 | source-dependent | 0.2191 | source-dependent |
| PMEmo | 0.5992 | 0.07026 | unsupported | unsupported |
| DEAM | 0.8192 | 0.03320 | unsupported | unsupported |
| Emotify | 0.8535 | 0.08999 | 0.6502 | 0.03847 |
| Jamendo human votes | 0.7933 | 0.18803 | 0.6452 | 0.03967 |
| Coimbra tags | 0.7305 | 0.20427 | 0.4872 | 0.05216 |

Emotify transfers useful activation ranking, but its mean prediction is 0.1638
against mean human energy 0.4251; MSE is worse than its 0.08271 source-mean
control. Jamendo party and Coimbra rousing scores are even lower in scale and
also lose to their source-mean controls on MSE. Different annotation scores
cannot become interchangeable product intensities solely by naming a vibe.

The acoustic-only activation hypothesis does not survive as a broad preference.
For PMEmo, source selection chooses acoustic attributes, but its embedding-only
comparator transfers at NDCG 0.8039 and MSE 0.02906, versus 0.5992/0.07026 for
acoustic attributes. DEAM's embedding comparator scores 0.8516 versus 0.7282
for attributes; Emotify scores 0.8619 versus 0.7175. These are already frozen
comparators, not replacements selected using soundtrack labels. Advancing the
embedding alternative requires a new declared comparison rather than rewriting
this round's selected winner.

Keep activation and aggression distinct. Jamendo's selected aggression score
correlates more with human anger (mean fold Spearman 0.4922) than energy (0.3005),
while its activation score correlates more with energy (0.6229) than anger
(0.3894). DEAM activation likewise correlates more with energy (0.6388) than
anger (0.2899). Correlations across different references are descriptive
construct checks, not a model-selection rule or proof of causal separation.
Jamendo's research terms and release gate still apply.

## Direct Focus candidate

The earlier CAL500 OOF rows/folds are reused unchanged; Reading and Studying
fractions directly define Focus. No new Focus training or new confirmation
corpus is claimed. The prior independent native MAE model has NDCG 0.4877;
the mapped-ranking selector has 0.4539, shared configuration 0.4730, and native
MSE diagnostic 0.4376. The shared model has lower MSE (0.01942 versus 0.02039
for the prior), so ranking alone does not settle all objectives.

Paired 1,000-replicate group bootstraps, stratified by existing outer fold,
give these NDCG differences against the prior native model:

| Comparator | Difference | Conditional 95% percentile interval |
| --- | ---: | --- |
| Mapped-ranking selector | -0.0339 | [-0.1159, 0.0687] |
| Shared configuration | -0.0148 | [-0.0886, 0.1014] |
| Native MSE diagnostic | -0.0501 | [-0.1225, 0.0722] |

All intervals include zero. They condition on the existing fitted models and
exclude retraining uncertainty. They provide no clear replacement evidence.
`focus-candidate.json` exports just the prior Reading and Studying heads and
their equal mean as an independent research candidate. Two independent replay
implementations agree exactly on all 494 CAL500 input rows. These full-source
replays check serialization, not held-out quality; quality above uses only OOF.

## Recommendation and evidence

Advance dimensional Cruise, the direct Focus candidate with existing native
heads retained, and independent activation/aggression experts. Preserve the
embedding activation alternative. Keep score calibration and domain transfer as
explicit next questions; this round does not justify automatically adopting a
single source, acoustic-only activation, or target-tuned weights.

All ten completed stage receipts and their outputs verify with
`./experiments/run.py candidates verify`. The connected-component repair has
its own `transfer-repair verify` command and independent real-metadata regression
covering all 32 original directions. Read-only verification checks the frozen
archives/inputs/outputs after the entrypoint evolves; fitting still requires an
exact match to frozen executable code and numerical runtime. Original frozen
plans and reports are preserved. The completed focused check reports 93 passed.

The summary retains every supported vibe, method, aggregation, fold metric,
score/reference mean and fixed pilot ensemble. Per-source reports retain
source-only selections and exclusions; NPZ files retain every recording's
reference and predictions. The exported source experts and Focus candidate are
research artifacts. Browser performance, fresh-user preparation, release rights
and listening acceptance remain outside these comparisons.
