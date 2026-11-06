# Independent vibe scores (#572)

This is a research contract, not a serving-policy change. The six definitions in
`rec/home_vibe.py::PRESETS` remain authoritative. Relax, Energize and Lift my mood
retain the shipped shared model. New Focus and Let off steam models remain in
shadow. Activation is not aggression. Existing Radio controls retain their
current meaning.

## Consumer migration inventory

| Consumer | Current assumption | Required boundary before adoption |
| --- | --- | --- |
| `rec/audio_vibe_external.py::scores` | Six positive scores normalized to sum to one | Preserve this output as `shared_scores`. Independently calibrated Cruise must never enter its denominator. |
| `rec/audio_vibe_model.py` | Six-way ranking, shared argmax, threshold 0.65, logit margin around 0.65, top-two margin; the bundled acceptable-NLL artifact selects one top-ranked vibe | Keep classification on the shared model. Do not compute mixed-model argmax, margins, probabilities or strong matches. Corrections and artifact validation remain tied to their original score link/version. |
| `rec/audio_vibe_prediction.py` | Per-excerpt heuristic scores, arc aggregation, three-sample completeness, 0.65 fallback to best available | Preserve provenance and distinguish a ranked candidate from a strong match. A three-sample capture is not proof of whole-song coverage or domain validity. |
| `rec/vibe_preparation.py` | Every prediction ranking entry becomes a measured fit with support 1; model identity versions preparation | Adopt one independently versioned fit at a time, including calibration, aggregation and coverage in identity. Never give an abstention support 1. Rebuild affected immutable measurements and dependent publications. |
| `rec/vibe_prediction.py`, `repos/vibe_predictions.py` | Artist/genre means, squares and support aggregate same-meaning per-vibe scores; own measurement wins over propagation/fallback | Do not combine old/new calibration populations. Version contributions; missing means unknown, not zero. Propagation remains prediction, not new measurement or a calibration target. |
| `rec/home_vibe.py` | Selected-vibe affinity, fixed 0.04 fit buckets, bounded separate taste score, heuristic fallback | Cruise-only ranking is possible without touching five others. Validate bucket meaning after calibration; a score change can change taste tie-breaking even if within-vibe order is monotone. Never silently mix fallback and calibrated scales in one fit bucket. |
| `rec/vibe_pools.py`, prepared Home/Radio publications | Cached affinity and vibe policy identity | Invalidate/rebuild on score-contract change; no stale mixed-version pools. Serving must consume prepared evidence without synchronous inference. |
| `rec/progressive.py`, `repos/musical_profiles.py`, `rec/account_growth.py` | Bounded preparation publishes/overlays fits; roadmap readiness observes the preparation version | Keep rebuild scheduling bounded and surface readiness from the new preparation state. Do not declare ready because an old version exists or add an onboarding checklist. |
| `rec/musical_character.py::_intensity_fit`, `rec/radio_intensity.py` | Shared intensity may choose max of usable Energize/Release fits, then conservative support handling and radio bands | Keep this legacy input route for Cruise-only work. Future Energize/Release specialists require a separate intensity contract; independent purpose scores cannot be compared by max to measure activation. Preserve activation/aggression axes and 0/0.5/1 Radio controls. |
| `core/audio_annotations.py`, `rec/audio_vibe_feedback.py`, `repos/audio_vibes.py` | Frozen prediction ranking, selected vibes and model/taxonomy provenance accompany human labels | Preserve historical predictions with their original semantics. Do not rewrite labels, reclassify snapshots or feed predictions back as human targets. |
| `web/routes/audio_vibes.py`, `web/static/audio-vibe-cards.js` | API exposes ranking; annotation UI defaults to `vibe` or first ranking entry and displays six scores | Preserve legacy guess explicitly. Independent results need per-vibe provenance, unknown state and multiple matches; no implied percentages or forced strongest-vibe badge. |
| `web/static/audio-vibe-inspector.js`, extension preview/worker | Sample/native attribute evidence and embedding provenance | Carry actual intervals, extractor signature and missingness. No new UI meaning for native arousal or aggression. |

The isolated executable example is `audio_lab_tools/vibe_score_contract.py`.
It keeps the legacy vector/classification separately and exposes independent
entries without normalization. Multiple entries may be strong; an empty set is
valid. `strong_match: null` means unevaluated/unsupported, `false` means evaluated
below a supported threshold, and `true` means above it. None forces a fallback
winner. A best available song is a within-vibe retrieval result, not a strong
match. Coverage contains real intervals and their union duration; absent audio
does not produce a numeric score. Partial scores can be provisional ranking
evidence in a validated domain, but cannot inherit a full-capture threshold.

Each independent entry identifies its model, calibrator, score semantics,
domain, threshold evidence and actual capture. No cross-vibe score comparisons
are allowed merely because all numbers lie in [0,1]. A scalar calibrated against
mapped emotion annotations estimates that mapped rating, not the probability
that a listener will choose this listening purpose.

## Frozen protocol (written before this round's evaluation)

Inputs are the existing #570 comparison's common training-disjoint pilot rows,
its fixed/refreshed shared predictions and artifacts, and #571's frozen compact
PMEmo/DEAM Cruise expert. That expert excluded the union of pilot identities
before fitting native models. Its coefficients remain fixed throughout; no pilot
annotation trained the underlying representation. All source/transfer identities
are audited again against its recorded native fits. Unknown aliases and upstream
embedding training identities remain limitations.

Use ManyMusic natural as the **external calibration source**. Preserve its five
artist/group outer folds; select calibration family by mean inner-fold MSE in
three label-independent grouped folds of each outer training partition. Fit the
chosen family only on outer training. Evaluate all families out of fold as
diagnostics; final source-only selection uses three grouped source folds.
Tie-break by the fixed family order: identity, training mean, nonnegative affine
(slope capped at 4), isotonic (weighted PAVA for tied raw scores, interpolation
between fitted points, endpoint clipping). All fits are scalar and bounded.

Transfer the single final source calibrator to ManyMusic generated and Memo2496
without fitting, selecting families or thresholds on their labels. Exclude any
known connection to source calibration identities. These datasets have already
been examined in earlier rounds: this is a frozen follow-up, not a pristine new
holdout. Source nested folds estimate the calibration procedure conditional on
the previously chosen frozen representation and source. Report source and each
transfer separately; do not pool domains to hide a regression.

Compare the raw specialist and nested/transfer calibrated specialist with the
frozen checkpoint, refreshed shared calibration and refreshed native+calibration
controls on identical retained rows. Report fold-mean tie-aware NDCG@top20%,
recording MSE/MAE, ten fixed score-bin mapped-rating calibration gaps, and the
fraction of top20% with mapped rating <=0.30. That last measure is a **low-proxy
intrusion**, not a human judgment of unsuitability. As in the frozen baseline,
top20% is capped at ten rows per fold. Missing positive/negative
support must be explicit. Group bootstrap intervals are conditional on the fixed
representation, folds and prior selection, not independent discovery evidence.

For threshold diagnostics, mapped rating >=0.75 is the predeclared high-proxy
target. Use only inner out-of-fold source predictions to select the lowest
threshold from [0.50, 0.60, 0.70, 0.80, 0.90, 0.95] with >=20 selected rows,
>=15 identity groups and Wilson 95% lower precision bound >=0.80. The bound uses
groups as trials: a group succeeds only if every selected member has high-proxy
rating. Report row precision separately. If none passes,
abstain. Report held-out source and transfer selections even if they fail. The
old 0.65 has no role. This diagnostic cannot establish a product strong match
without direct purpose labels and validated serving-domain/capture coverage.

Partial-capture replay uses the first one, first two and all **actual** available
ten-second excerpts of the frozen annotation interval. Fit no partial-specific
calibrator; source partial predictions use the same outer-training calibrator,
transfer uses the final source calibrator. Replay all three shared controls at
the same coverage. Compare against the full annotation interval explicitly:
this measures missing-coverage error, not truth about an unheard opening or
whole track. Zero capture is unknown. Never pad or treat repeated excerpts as
additional coverage. Memo has six excerpts versus ManyMusic's three; do not
silently equate them.

Predeclared decision gates:

1. Cruise **ranking candidate**, assessed separately for raw and calibrated
   variants: full-capture NDCG gain >=0.02 over each shared
   control on source and both transfer domains; at least one transfer's paired
   group-bootstrap 95% lower gain over checkpoint >0; low-proxy intrusion no
   more than 0.05 worse than checkpoint in any domain. This establishes only
   a candidate for selected-vibe adoption and subsequent product acceptance.
2. Calibrated **mapped scale**: nested-source MSE improves raw by >=0.002; no
   transfer MSE regression >0.005; no domain NDCG loss >0.02 against raw; partial
   one/two-excerpt MAE does not exceed full MAE by >0.05 in any domain. Failure
   keeps the calibrator experimental; do not substitute it just for nicer numbers.
3. **Strong match**: source thresholds available in all five outer folds and
   source/each transfer independently meet the count/group/precision gates;
   additionally direct listening-purpose labels, serving-domain evidence and
   actual capture coverage must support the claim. Missing evidence means no
   strong-match adoption, irrespective of ranking gains.

Use 1,000 paired whole-group bootstrap draws within fixed folds, seed 572029.
Local labels are supplementary only: cite the already reported #570 reserved
eight, without rereading them for this round's selection or fitting. Focus has
no VA mapping and Release requires aggression evidence, so neither is evaluated
or promoted by this Cruise protocol. No production artifact, schema or ranking
policy changes are in scope here.
