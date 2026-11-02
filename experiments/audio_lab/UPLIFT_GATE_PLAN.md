# Arousal-conditioned Uplift, #712

Freeze before fitting. The only new mapping is a convex two-family mix:
`lambda(a) = sigmoid(b0 + b1 * (a - .5))`, then
`lambda * valence_family + (1-lambda) * happiness_joy_family`.
The nine parameter pairs are b0 in {-1,0,1}, b1 in {-2,0,2}; penalize
`.001 * (b0*b0 + b1*b1)` in source-macro pairwise ranking loss. No source
identity enters inference. The zero pair exactly recovers the fixed mix.
Preserve #618's native labels, references, ridge/constant grid, mean-embedding
training, clipped-native chunk averaging and within-family source weights.
DEAM/PMEmo native arousal heads use that same bounded head procedure solely as
context. Missing happiness/joy labels remain missing.

Use existing eight-source #618 rows. Join their identities to the complete
sixteen-source-version inventory (original thirteen plus three pilots) and local identities. The pre-fit audit finds eight
new component merges; rebuild five outer grouped folds and refit the unchanged
head procedure. Do not reuse old coefficients as unbiased out-of-fold heads.
At every outer/whole-corpus stage, crossfit all heads in three globally grouped
folds before selecting gate parameters. Head hyperparameters use three inner
grouped native-MSE folds. No held-out component enters any preceding stage.
Whole-corpus omissions remove Soundtracks, both ManyMusic strata together, or
Memo and all connected components from every remaining source.

Controls: matched repaired fixed consensus and DEAM anchor; the already frozen
full constant weights DEAM .5 / Soundtracks .25 / Emotify .25, renormalized only
when a source is omitted; exact adopted full artifact; exact prior DEAM cached
predictions where available. The last two have prior exposure and are explicitly
descriptive controls. Also replay the exact frozen full constant artifact,
separately from the matched refit with its frozen weights. Native-head targets/procedure stay fixed; coefficients
change only to enforce the stronger component exclusions. No new Memo data.

Report all native target MSE/MAE, fixed-reference mapped MSE/calibration,
fold-macro NDCG and top-20%-capped-at-ten lift. Use 1,000 paired component
bootstraps within outer folds; single omitted-corpus evaluation still uses the
fixed five-fold metric convention. Keep natural/generated separate.
Primary macro weights: Soundtracks 1, natural .5, generated .5, original Memo 1.
Require macro NDCG gain >=.02 and lift >=.01 versus both fixed and frozen
constant in historical AND browser preparation; at least one positive paired
primary NDCG lower bound per comparison/preparation. Any primary or omission
domain NDCG/lift loss >.01 or mapped MSE increase >.005 fails. Native error is
unchanged between mixers. Require meaningful gain on at least one supported
gentle-positive contrast. Otherwise retain; underpowered contrast is unsupported.

Contrasts fixed before fits: Soundtracks energy<=.35 and happy>=.65 is
gentle-positive; energy>=.65 and valence<=.35 energetic-negative. VA-only sources
use arousal<=.35 and valence>=.65 as an explicitly named positive-valence proxy,
and arousal>=.65 / valence<=.35 as energetic-negative. Require 15 recordings and
10 components to report contrast accuracy; preserve smaller/empty slices and IDs.
Report contrast MSE and mean scores without selecting another model.

Bound fitting/evaluation to two hours, 1 GiB recorded process RSS, 512 KiB full
artifact and 100 ms warm three-chunk replay. Freeze runtime, source, inputs,
identity audit and hashes. Cache stage models/predictions/traces with receipts;
verify independent replay within 1e-12. These inspected populations provide
exploratory evidence. Preserve #618 retain and the later user adoption decision.

Round 2 adds the exact full constant artifact comparison before inspecting any
evaluation outcomes. Round 1's completed fits and interrupted evaluation remain
preserved; neither the gate procedure nor acceptance thresholds change.
