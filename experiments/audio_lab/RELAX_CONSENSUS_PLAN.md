# Relax consensus, #617

The user authorized this bounded comparison on 30 October 2026, after #579's
single-source comparison. #579 remains the original experiment; #617 owns this
follow-up. This plan replaces #617's proposed activation/tenderness-only search
with the agreed anchor, fixed blend and learned consensus comparison. No
production changes or new listening collection are included.

## Data and exposure

Refresh the user's labels with read-only SQL and freeze their real capture files
before fitting. Use the enlarged current set, not the original 55-song diagnostic.
Unselected vibes remain unknown. These labels supply supplementary score and
capture diagnostics, never invented negatives or weight selection.

Reuse the verified #579 inputs: Emotify, Soundtracks, ManyMusic natural and
generated, and the audio-backed Memo pilot. Inventory all supplied annotations
and audio separately from eligible recordings. Extra windows, raters and chunks
are not additional independent songs. Retain original normalized native labels
and fixed references. Keep natural/generated music separate in all tables.

Construct recording/audio/artist/declared-group components jointly across all
five populations. Conservatively include equal normalized titles and remove
whole components matching the new local export. Missing artist identities and
unknown encoder-pretraining overlap remain limitations. Freeze five outer folds
and three nested training folds from identities alone. All selection, including
base-head regularization, excludes the entire validation component.

The inspected external populations remain diagnostics even after new splitting.
ManyMusic and Memo now enter training; their old full-population transfer scores
cannot serve as independent test results for that fitted model. Separately leave
out Soundtracks, Memo, and both ManyMusic strata together, removing every linked
component from the other training sources as well.

## Exactly three procedures

1. **Anchor:** source-selected native Emotify calmness and tension heads, with
   Relax equal to mean(calmness, inverse tension).
2. **Fixed blend:** 0.5 Emotify, 0.25 Soundtracks, 0.125 ManyMusic, 0.125 Memo.
   ManyMusic contributes the mean of its separate natural/generated experts.
   When a corpus is omitted for transfer evaluation, redistribute its auxiliary
   weight proportionally among the remaining auxiliary corpora; keep anchor 0.5.
3. **Learned blend:** nonnegative weights in increments of 0.125, sum one,
   Emotify weight at least 0.5, including the anchor-only zero-auxiliary control.
   Learn weights from fully cross-fitted predictions inside the training data.
   Minimize pairwise ranking logistic loss with temperature 0.1, weighted by
   observed reference differences. Give Emotify, Soundtracks, Memo and ManyMusic
   equal corpus weight, with natural/generated ManyMusic each half a corpus.
   Each corpus retains its own fixed target. Ties prefer more anchor weight.

Native heads use only their observed source annotations: Emotify calmness and
tension; Soundtracks energy, tension and tenderness; VA for each ManyMusic
stratum and Memo. Choose constant or ridge alpha 0.1/1/10/100/1000 independently
per native label using grouped training-fold MSE. No missing labels become zeros.
The experiment learns a combination of source-native experts; it does not assume
that VA, calmness and tenderness are interchangeable co-annotations.

All new procedures use the mean embedding over the exact annotated interval,
one row per recording. Training and scoring use this same aggregation. Preserve
the older controls' original mean-of-clipped-chunk-predictions behavior. Compare
browser-prepared means on the same outer/transfer models without refitting.

Fixed reference definitions remain #579's: mean(calmness, inverse tension) for
Emotify; mean(inverse energy, inverse tension, tenderness) for Soundtracks;
mean(inverse arousal, valence) for Memo and ManyMusic. The VA references are
proxies, not direct Relax truth. Positive valence may support the auxiliary
experts but is never a prerequisite for the anchor or a hard suitability gate.

## Controls, diagnostics and decision

On identical protected rows, retain #579's exact Emotify embedding model,
shared checkpoint, refreshed shared models and browser controls. Retain its
older combined Emotify model wherever compatible features exist. The current
pilot cache contains no acoustic values; report missing historical comparisons
explicitly unless the exact historical feature policy can be reproduced.

Report NDCG, top-list mean/lift, MSE/calibration, per-fold results and 1,000 paired
group-bootstrap draws. Compare each blend with the refitted anchor and exact
older embedding control separately. Do not pool raw scores across references.
Summarize equal-corpus gains only after showing every domain.

Add source-native observed-attribute versus cross-fitted predicted-attribute
diagnostics with every reference-defining column excluded from inputs. Report
quiet/tense, calm/sad and low-arousal/negative-valence coverage using fixed
0.35/0.65 cutoffs, without calling a VA quadrant observed calmness. Retain local
positive-only score tables and compare first/complete capture stability.

A blend merits an adoption recommendation only if its mean NDCG gain is at least
0.02 over both anchor controls on the four primary strata, at least one paired
domain interval excludes zero, no domain loses more than 0.02 NDCG or 0.01 lift,
and leave-corpus-out/browser comparisons preserve that lack of material domain
regression. This is a research recommendation, not independent confirmation or
automatic adoption. Otherwise retain and explain the failure.

Export the research fit with native heads, mappings and learned weights; verify
independent arithmetic, size (at most 512 KiB), warm three-excerpt inference
(100 ms), and process RSS (1 GiB). A failed blend is a useful completed result.
All artifacts stay under experiments; no global-suite lock is used.
