# Common-target candidate follow-up

29 October 2026, #530. This bounded follow-up preserves the original mapped-vibe
round and its connected-component correction. It uses external annotations and
our cached audio embeddings/attributes. Local vibe judgments and stored library
predictions do not enter selection or evaluation.

1. Compare the direct Reading/Studying Focus candidates on their existing,
   identical CAL500 held-out rows. Retain the prior independently selected native
   MAE heads, mapped-ranking selector, shared configuration and native-MSE
   diagnostic. Export the prior Reading and Studying heads as an additional
   research candidate. Do not select a new winner using these reused outcomes.
   Report paired 1,000-replicate group bootstrap differences against the prior
   native model, stratified by the existing five folds. These intervals condition
   on the existing fitted models and do not include retraining uncertainty.
2. Compare PMEmo, DEAM and Emotify on each of three common audio-backed pilot
   cohorts: natural ManyMusic, generated ManyMusic, and Memo2496. All three use
   the same 200-value embeddings and 21-candidate grid (mean/median plus the
   existing 19 learners). The pilot cache lacks the 27 acoustic attributes;
   do not impute an entire missing feature family or claim to test it here.
3. Compare PMEmo, DEAM, Emotify, Jamendo human votes and Coimbra on the same
   soundtrack rows, using the original 59-candidate grid. Soundtrack human
   energy supplies the activation reference; anger supplies the Release
   reference. Keep other supported fixed mappings as secondary results. Compare
   acoustic, embedding, combined and selected heads on identical target rows.
   VA-only sources remain unsupported for Release. Report activation/anger
   cross-responses as descriptive construct checks, not new selection criteria.

For each source/regime, remove whole source identity components anchored to the
union of target artist/recording/audio identities, plus conservative title-only
rows, before three-fold source-only selection. Do not use target labels to choose
hyperparameters, sources, mapping weights, aggregation or calibration. Freeze
source exclusions, targets, exact matrices, grouped folds, code, runtime and
rules before fitting. Never promote this exploratory round as untouched
confirmation: these pilots and corpora have previous inspected results.

The pilot primary score averages native predictions over every consecutive
ten-second chunk of the rated interval, then applies the fixed mapping. Report
two predeclared secondary policies: predict from mean embeddings, and average
chunk-level mapped scores. Moderate-arousal mapping is nonlinear, so those
policies are different questions; target outcomes must not choose between them.
Each recording contributes one evaluation row. Existing supplied fractional
annotations stay fractional. Soundtracks retain their original aligned excerpt.

References use the same previously frozen VA, GEMS, aggression and activity
rules. Keep separate natural/generated cohorts; no cross-corpus raw-score winner
table. Report same-row NDCG/random baseline, top lift, MSE/source-mean control,
and score/reference means. An equal PMEmo/DEAM ensemble is fixed in advance for
each pilot cohort; no fitted target calibration or outcome-weighted mixture.

Preserve original extraction provenance, signature and chunk hashes; verify all
planned rows/errors are accounted for. Focus export replay must match two
independent numerical implementations. Keep model artifacts under experiments/;
product integration, browser performance and listening acceptance stay separate.
