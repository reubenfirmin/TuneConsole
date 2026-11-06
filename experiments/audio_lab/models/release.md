# Release: provenance

Selected Oct 30, 2026 (#623): the exact #577 Jamendo human-aggression head.
The original research recommendation was **retain**; later explicit adoption
accepted the uncertainty. Anger/aggression labels do not establish anger relief.

## Derivation

One 200-input ridge head, alpha 1000, trained on 187 transfer-protected
[Jamendo](../datasets/jamendo.md) rows. Predict and clip aggression per excerpt,
then average available predictions. No power blending, new source weighting or
refitting during export. Independent suitability does not enter shared argmax,
classification thresholds or radio intensity.

Artifact: [release-vibes.json](../../../src/yt_playlist/rec/data/release-vibes.json),
20,516 bytes, SHA256
`3155a3a5dd44776f950688f643eff4fae9ded38029dea9af7c34eead634cc0ac`.
Source: `local/individual-20261030/remaining-1/` (original multi-attribute model),
SHA256 `4591b47b47775fe095a5260feda4e9819abd61b9b663834595fa6c9bffebe415`.
Source plan: `c7c69cb680a7c6a7919b35984356b295521a53271d66cf48270677af230829ae`.
Original dataset terms remain in the adjacent product notice.

## Experiments and decisions

| Date / comparison | Outcome and reason |
| --- | --- |
| Oct 30, #577 | Soundtracks anger NDCG .7111 versus shared .6217, paired gain [-.0082,.1911]; browser .7112. Favorable point estimate did not establish a reliable gain, hence retain. |
| Oct 30, #614 | Learned aggression/power blend and source-balanced native tasks failed ranking/lift gates over exact #577. Browser blend NDCG .5342; difference -.1770 [-.2503,-.0240]. Lower MSE did not rescue ranking. |
| Oct 30, adoption | User selected the exact aggression predictor after those comparisons. Coefficient replay covers 470 external excerpts and 159 real-capture cases from 84 recordings; error at most 1.11e-16. |
| Nov 1, #709 | New-artist acquisition returned zero audio after four HTTP503 responses. Extension is inconclusive; an empty extension's identical refits are not evidence of additional-artist benefit. |
| Nov 1, #708/#809/#810/#811 | Alternative encoders, chroma and predicted mid-level properties supplied no qualifying replacement. Intermediate MERT's Soundtracks gain had a wide interval and did not improve Jamendo ranking. |

Evidence: `local/release-614-20261030/round-2/`,
`local/release-production-20261030/package-2/`,
`local/release-extension-709-20261101/round-3/`.
Protocols: [original individual fits](../INDIVIDUAL_EXPERIMENT_PLAN.md),
[mapping](../RELEASE_MAPPING_PLAN.md), [extension](../RELEASE_EXTENSION_PLAN.md).
Reproduce/verify through `./experiments/run.py release`, `release-package`,
`release-production` and `release-wheel`; choose a new output directory for a
new export. Package parity does not resolve source rights or listening efficacy.
