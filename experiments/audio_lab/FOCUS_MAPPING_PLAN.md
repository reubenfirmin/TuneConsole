# Focus native-attribute mapping experiment (#615)

30 October 2026. Freeze before fitting. Keep CAL500's original Reading and
Studying listener fractions separate; their equal mean is the Focus reference.
Reuse #576 and #530 evidence. No production changes, new listening, commits or
pushes. Research and focused checks do not use the global-suite lock.

## Inventory and identities

Use the 494 CAL500 recordings and original response files; the existing common
comparison has 450 product-training-disjoint recordings. Audit original votes,
rater support, disagreement and ten complete ordinal attributes: energy,
changing energy, beat, tempo, calming, arousing, pleasant, tender, powerful and
mellow. Normalize each observed 1–5 response as `(value - 1) / 4`, retaining
the README's inconsistent three-level emotion description as a limitation.
Reconstruct vocal presence for coverage only: any known present/prominent vocal
instrument gives presence; all absent gives absence; unresolved responses stay
unknown. Do not treat vocals as an exclusion or invent distraction labels.

External native sources: DEAM, PMEmo, natural/generated ManyMusic and Memo
valence/arousal; Soundtracks energy/tension/tenderness; Emotify calmness/tension/
power. Unpaired sources remain separate supervised tasks. Refresh local labels
and exclude complete known identity components across every source and local
capture before fitting. Local positives are supplementary; unselected vibes are
unknown. Record artist/recording/audio identities, conservative title edges,
declared groups, missing identity information and unknown encoder exposure.

Cross-source components merge several original CAL500 fold groups. Use five new
grouped CAL500 outer folds (seed 61530), removing each outer validation component
from every native source. Refit the exact #576 historical/browser direct
selection procedures on these folds as current comparison controls. Preserve
archived #576 predictions as labeled legacy diagnostics; they used weaker
grouping and do not establish the new component-isolation guarantee. Unchanged
shared controls also get different fold-specific ranking summaries.

## Three procedures

1. **Direct:** separate Reading/Studying ridge heads from the audio embedding.
2. **Attributes:** map cross-fitted native predictions to Reading/Studying.
   Inputs are the ten CAL500 attributes plus eight external features: equally
   source-family-balanced arousal and valence, Soundtracks energy/tension/tender,
   and Emotify calmness/tension/power. ManyMusic's natural/generated strata split
   their family's weight equally. This is an attribute consensus calibrated to
   the original activity labels, not a quietness or emotion replacement target.
3. **Hybrid:** concatenate the 200-dimensional audio embedding and those same
   18 attribute features, then fit the two activity heads.

Native heads and new activity maps select a training mean or ridge alpha
0.1/1/10/100/1000 by native MSE in three grouped training folds. Attribute lists
and consensus weights are fixed here; no outer-result feature search. For map
selection, every inner validation component is removed globally before native
selection. Mapping-training inputs are themselves three-fold cross-fitted with
complete global exclusions. Only after selecting map regularization on these
held-out predictions are native heads refitted on the outer training partition.
Do not tune a map on features produced using its validation recordings.

Fit native heads on mean annotated-recording embeddings. At inference clip each
excerpt's native prediction, average native attributes, construct the fixed
consensus features and map them to activities; then average Reading/Studying.
The direct scorer clips each excerpt before averaging. The hybrid additionally
uses the mean raw embedding, explicitly recorded as its own serving rule.
Use historical features for new-procedure training and evaluate historical and
current browser features separately. The #576 browser control trains on browser
features, as originally declared; do not silently describe it as the same fit.

## Diagnostics and controls

- CAL500-only and external-only attribute maps are fixed zero-input-block
  controls with the same nested selection; they cannot be promoted by picking
  the highest outer result. Direct is the hybrid's zero-attribute control.
- Fit observed-human-attribute maps from the same ten CAL500 attributes to the
  same activity labels. Compare with the CAL500-only audio-predicted maps to
  separate available semantic signal from prediction loss. Exclude all Usage
  columns from attribute inputs. These are same-rater descriptive associations,
  not causal evidence or an accuracy ceiling.
- Retain exact archived prior, latest research, shared/refreshed and browser
  controls; report the two newly refitted #576 procedures separately. Include
  training means. Report CAL500 native attribute error and selection CV error
  for external native heads, labeling the latter as selection diagnostics.
- Report vocal/nonvocal, high/low-energy and Reading/Studying-disagreement
  contrasts using existing human labels. Empty/unsupported cells stay unknown.
  Check first/three/all local captures without converting positive-only labels
  into accuracy. No temporal Focus truth is available for individual chunks.
- Reuse the reference audit: Moods/Activities has 209 unverified candidate joins
  and HelpStudy has episode outcomes. Neither supplies verified new Focus rows;
  no additional acquisition is justified by this round's label inventory alone.

## Metrics, decision and resources

On the identical common rows, report Focus and each native activity's NDCG,
top-list suitability/lift, MSE, MAE, calibration and paired 95% intervals from
1,000 identity-component bootstrap draws within fixed outer folds. All data
were previously inspected; new folds do not create fresh confirmation.

A candidate needs browser Focus NDCG gain >=0.02 over shared, >=0.01 over the
refitted #576 browser control, and a positive paired NDCG interval over shared.
Neither preparation may lose >0.02 NDCG or >0.01 top-list lift against its shared
or refitted #576 control. Each Reading/Studying native reference may not lose
>0.03 NDCG or >0.02 lift versus the corresponding refitted #576 control.
Attributes/hybrid must also gain >=0.01 browser Focus NDCG over direct. Otherwise
retain, preserving every failure. A later explicit product tradeoff does not
change this frozen decision rule.

Export each full research candidate, coefficients, preprocessing, mapping,
aggregation and provenance. Replay per-head and final scores independently on
outer and local inputs. Bounds per candidate: 1 MiB, maximum warm three-excerpt
inference 100 ms, process RSS 1 GiB; one numerical thread. Record source terms.
