# Energize activation, consensus and exercise mapping (#616)

Frozen before fitting on 30 October 2026. Follow #530 and reuse #578's archived
features, native labels and exact controls. This round has three declared
procedures, fixed ablations, grouped evaluation and omitted-corpus transfer.
All populations were previously inspected. This is development evidence.

## Procedures and supervision

1. **Activation:** equal source-family mean of DEAM arousal, PMEmo arousal,
   ManyMusic arousal and Memo arousal. ManyMusic natural/generated have equal
   shares within their family regardless of row count.
2. **Consensus:** 50% activation, 25% Soundtracks energy, 12.5% Emotify power and
   12.5% Emotify joyful activation. Fixed zero-power and zero-joy ablations
   transfer the removed eighth to the other Emotify attribute. They are
   diagnostics, not extra candidates selected by outer performance.
3. **Activity:** a ridge mapping with predeclared alpha 100 from the four
   separately represented consensus inputs to CAL500's unfiltered Exercising
   listener fraction. Fit on cross-fitted native predictions. Fixed diagnostic
   variants retain activation alone, omit power, or omit joy. No mapping
   hyperparameter or feature selection uses outer/transfer results.

Each native head uses the existing training-mean plus ridge alpha
0.1/1/10/100/1000 grid, selected independently by native MSE in three grouped
training folds. Fit native heads to recording mean embeddings; clip each
excerpt's native prediction before averaging for serving and evaluation.
The activity map is applied to those averaged native predictions. It represents
an exercise-vote scale, not calibrated arousal or universal Energize probability.
Valence is never a required input. Power alone is never an activation substitute.

Eight source tables: DEAM, PMEmo, Emotify, Soundtracks, natural/generated
ManyMusic, Memo and CAL500. CAL500's direct audio-to-Exercising head is a
refitted control, not a fourth new procedure. Use complete known artist,
recording, audio, declared-group and conservative title components across all
sources and the refreshed local snapshot. Remove every locally overlapping
component. Missing identities and unknown encoder pretraining remain limits.

## Evaluation and isolation

Five joint grouped outer folds. Within each outer training partition, produce
three-fold cross-fitted source predictions for CAL500 mapping training. Remove
the entire validation component from every native source before native model
selection. The fixed-alpha activity regression needs no additional selection.
Refit native heads on the full outer training partition after mapping fit.

Separate transfer fits omit Soundtracks, both ManyMusic strata together, Memo,
or CAL500, including linked components in every remaining source. Renormalize
activation equally over remaining source families. When Soundtracks is absent,
its 25% transfers equally to Emotify power/joy. CAL500-omitted activity mappings
and the direct activity control are **not estimable**; report them absent, never
substitute another target or claim exercise-corpus transfer for that procedure.

Primary references remain fixed: Soundtracks energy, natural ManyMusic arousal,
generated ManyMusic arousal, Memo arousal, and CAL500 Exercising. Evaluate all
methods on #578's exact compatible common rows after the refreshed exclusions.
Keep historical and #424 browser preparations distinct. Include exact shared,
refreshed-calibration, refreshed-both, PMEmo/DEAM, Emotify, equal-VA, source-mean,
named prior and direct-activity controls wherever saved compatible evidence
exists. Changing joint folds changes fold-specific NDCG/top lists; show that
explicitly and never compare absolute results across different fold plans.

Report NDCG, top-list suitability/lift, MSE, MAE, score/reference means and five
fixed calibration bins. Paired 95% intervals use 1,000 identity-group bootstrap
draws within fixed outer folds. They condition on fitted procedures and reused
populations. For primary gains, weight Soundtracks/Memo/CAL500 as one each and
natural/generated ManyMusic as one half each. Do not pool native constructs.

## Mapping diagnosis and coverage

Reconstruct original CAL500 listener fractions for Exercising; verify exact
agreement with the frozen activity labels. On the same recordings reconstruct
seven co-annotated attributes: High Energy, Heavy Beat, Fast Tempo, Very
Danceable, Powerful/Strong, Cheerful/Festive and Arousing/Awakening. The raw
files contain integer values 1–5 for all seven; normalize these ordinal values
with `(value - 1) / 4` and average listeners. The release README describes
emotions as three levels despite five-level raw files. Preserve this discrepancy:
the diagnostic uses the observed raw encoding, not a claim of interval-scale
or cross-corpus equivalence. Do not use any Usage column as an input.

Compare fixed-alpha-100 exercise mappings from observed attributes versus
cross-fitted audio predictions of those attributes. Select native attribute
heads within each mapping crossfit training split; no outer component enters
either stage. Report rater support, variance, split-half descriptive agreement
and nonzero exercise-vote counts. These same-rater associations are descriptive,
not causal effects or an independent exercise truth.

Describe energetic-cheerful, energetic-unhappy and forceful-but-slower slices
using thresholds >=0.65 / <=0.35 on compatible observed native labels. Empty
slices remain empty. CAL500 uses its own normalized energy/power/tempo and
cheerfulness labels; Soundtracks anger/energy is only a forcefulness proxy.
For Memo compare the first 1, 3 and 6 ten-second chunks against their aligned
arousal annotations. For local captures compare first/three/all predictions;
unselected vibes are unknown, and local labels do not select a model.

## Decision and resource limits

Each of the three procedures needs primary historical macro NDCG gain >=0.02
over the shared control and at least one positive paired NDCG interval. No
primary historical/browser domain may lose more than 0.02 NDCG or 0.01 top-list
lift against shared or the exact prior PMEmo procedure. Apply the same loss
limits to estimable omitted-corpus results. Consensus and activity must also
improve historical macro NDCG over activation by >=0.01. Otherwise retain and
report every failed rule. A later product decision can accept an explicit
tradeoff; it cannot turn this frozen rule into a passing experiment.

Export the complete research fit, native preprocessing and activity maps.
Independently replay all saved outer/transfer and local predictions. Bounds:
512 KiB artifact, maximum warm three-excerpt inference 100 ms, peak process
RSS 1 GiB. Use one numerical thread. All work stays under `experiments/`;
experiments and focused checks do not use the global-suite lock. No production
changes, new listening phase, commits or pushes. Preserve source notices and
unresolved distribution status.
