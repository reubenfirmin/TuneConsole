# Uplift consensus, #618

The user authorized this research on 30 October 2026, including a consensus
candidate. #580's completed original comparison remains a retain result and
supplies exact controls. This follow-up does not change production or require
new listening collection. Freeze this protocol, code, inputs and references
before fitting; preserve every attempt.

## Labels, exposure and identity

Refresh the enlarged local labeled corpus read-only and copy its exact capture
files. Local positives are supplementary, unselected vibes are unknown, and no
local scores or labels select the model. Inventory changes since #570.

Reuse the verified #580 caches for DEAM, PMEmo, Emotify, Soundtracks, Jamendo
human votes, ManyMusic natural/generated and the Memo pilot. Retain supplied
versus usable counts, target instructions, scales, rater support/disagreement,
audio/excerpt alignment and missing artist identities. Do not acquire new audio
for this bounded round. Missing fields are unknown, not negative observations.

Build recording, artist, audio, declared-group and conservative equal-title
components jointly across all sources and the current local set. Exclude whole
components touching local recordings. Five outer folds and three nested training
folds are determined from identities only. Every native fit and source-weight
selection excludes complete validation components across all sources.

Previously inspected populations remain diagnostics after resplitting. All
sources now enter development, so also withhold Soundtracks, both ManyMusic
strata together, and Memo in three separate transfer fits. Exclude every linked
component from the remaining training corpora. No dataset identity is supplied
to the scorer; every input receives the same experts and weights.

## Three procedures

1. **Valence anchor:** DEAM native valence, refitted within the joint training
   folds. Retain the exact previous DEAM model separately as a frozen control.
2. **Fixed consensus:** half the weight divided equally among the four valence
   experts (DEAM, PMEmo, ManyMusic and Memo), and half assigned to Soundtracks
   happiness/valence (0.25), Jamendo happy (0.125), and Emotify joyful activation
   (0.125). ManyMusic is the equal mean of natural/generated experts. When a
   corpus is omitted, renormalize within each of these two families, keeping
   half the total weight per family. Joyful activation is supporting evidence;
   it does not gate Uplift on high energy.
3. **Learned consensus:** nonnegative source weights summing to one, in steps
   of 0.25, plus the fixed blend as a candidate. At least half the weight stays
   on valence experts; Emotify joyful activation receives at most 0.25. The
   DEAM-only zero-auxiliary control is included. Select weights using fully
   cross-fitted native predictions within the training data, minimizing
   within-corpus pairwise logistic ranking loss (temperature 0.1), weighted by
   reference differences. Give each corpus equal weight and each ManyMusic
   stratum half a corpus. Ties prefer DEAM then the declared expert order.

Native heads predict valence for DEAM/PMEmo/ManyMusic/Memo, valence and happiness
separately for Soundtracks, happy for Jamendo and joyful activation for Emotify.
Use only each source's observed targets. Select constant or ridge alpha
0.1/1/10/100/1000 per native label by grouped source-training MSE. Source-specific
preprocessing is learned in those folds. No fabricated co-annotations, common
numeric target pool or target-corpus calibration is introduced.

Each annotated recording trains on its mean embedding; repeated excerpts do not
duplicate its label. Serving and source-weight selection predict/clip each
excerpt and then average, consistently with Cruise, Release and Relax. Native
parameter selection evaluates mean-embedding rows; that explicit training versus
serving distinction is shared by all three candidates. Browser excerpts are
scored with the same fitted models, without refitting.

## Fixed references and diagnosis

References remain #580's: valence on VA corpora, mean(happiness, valence) on
Soundtracks, happy votes on Jamendo and joyful activation on Emotify. These
constructs are not interchangeable and do not prove induced mood improvement.
Primary strata are Soundtracks, natural/generated ManyMusic and Memo, on their
same supported control rows after any additional local exclusions. Retain
frozen/refreshed/shared/browser controls, the named prior PMEmo head, the exact
previous DEAM head and the refitted anchor. Preserve prior comparisons when
features are unavailable; do not substitute a different feature policy.

Diagnose the Soundtracks failure in two complementary ways: report valence and
happiness prediction errors separately, and compare observed versus cross-fitted
predicted attributes against fixed references. For the composite reference,
exclude both happiness and valence from diagnostic inputs. Separately predict
human happiness from valence alone; happiness is excluded from those inputs,
and that diagnostic is not a replacement primary reference. Diagnostic mappings
use fixed ridge alpha 100, with source-native predictors selected only inside
their training folds. Diagnostics never select consensus weights.

Report 0.35/0.65 fixed slices for low-energy positive, energetic negative and
positive forceful music, with identities and empty/sparse counts. Use Memo's
aligned first/three/all-excerpt human valence targets where available. Whole-
window ManyMusic annotations do not establish chunk-level truth. Include
positive-only local first/all capture stability without false-positive claims.

## Decision and budgets

Report native errors, NDCG, top-list mean/lift, MSE/calibration, every fold and
1,000 paired whole-component bootstrap draws on fixed fits. Show each domain
before an equal-corpus mean (natural/generated ManyMusic each half a corpus).

A consensus merits an adoption recommendation only if historical primary mean
NDCG improves by at least 0.02 over both the refitted anchor and shared checkpoint,
at least one domain's paired NDCG interval excludes zero against each control,
and no historical/browser outer or omitted-corpus comparison loses over 0.02
NDCG or 0.01 top-list lift against the anchor, exact prior DEAM or corresponding
shared control. The existing Soundtracks failure must not be hidden by a mean.
Otherwise retain and report which limits failed. No threshold is relaxed after
observing results.

Export the complete research fit, mappings and fixed/learned weights. Check every
held-out prediction using independent arithmetic, artifact size at most 512 KiB,
maximum warm three-excerpt inference 100 ms and peak process RSS 1 GiB. Use one
numerical thread and bounded pair-loss blocks. All outputs stay under
`experiments/`; experiments and focused checks do not use the global-suite lock.
Preserve original source notices and unresolved distribution status.
