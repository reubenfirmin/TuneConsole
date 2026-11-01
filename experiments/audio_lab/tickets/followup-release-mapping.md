Parent: #530. Follows #577. Priority: **P2**. **In review: bounded experiment complete; author recommendation is retain.** Use #530's shared research and label-mapping protocol. This is a new bounded experiment, not a reopening of the completed comparison or permission to change production.

Completed evidence: `experiments/audio_lab/RELEASE_MAPPING_RESULTS.md`, `RELEASE_LABEL_AUDIT.md` and frozen `local/release-614-20261030/round-2/`. The power blend and source-balanced native tasks do not improve #577's exact aggression candidate. Eleven focused checks and receipt verification pass. Preserve the current production scorer and original research candidate; independent acceptance remains outstanding. These are retained workspace artifacts, not committed or pushed changes.

## Question and evidence

Can existing aggression, power and anger annotations establish a transferable Release mapping, starting from the promising Jamendo aggression head?

The 30 October experiment improved Soundtracks anger NDCG from 0.6217 to 0.7111 and top-list lift from 0.2928 to 0.3447. Paired gain intervals were [-0.0082, +0.1911] and [-0.0111, +0.1285]; browser inputs preserved the pattern. Mean aggression score was 0.0820 against mean anger 0.1534. Emotify power alone regressed, and equal averaging weakened the Jamendo result. These observations suggest a useful candidate with unresolved construct, calibration and confirmation questions.

Evidence: `experiments/audio_lab/INDIVIDUAL_EXPERIMENT_RESULTS.md`, frozen round `local/individual-20261030/remaining-1/`, plan SHA256 `c7c69cb680a7c6a7919b35984356b295521a53271d66cf48270677af230829ae`. These are retained workspace artifacts; the retain recommendation has not received independent acceptance.

## Strategy

1. Inventory usable human aggression, power and anger annotations, scales, agreement, excerpt alignment and identity overlap. Keep explicit Jamendo votes, Emotify power and Soundtracks anger distinct; tag-derived Coimbra and mismatched EMMA previews remain supplementary. Count independent recordings and supported contrasts, not just annotation rows.
2. Use existing jointly annotated excerpts to test relationships among force, anger, energy and valence before fitting another audio model. Evaluate mappings against a fixed, separately observed reference; exclude that reference and its defining columns from mapping inputs. If compatible paired labels are absent, record that limit rather than treating related words as equivalent truth.
3. Freeze at most three procedures: the current Jamendo research candidate; a small monotone aggression/power mapping; and a source-balanced model with separate native aggression, power and anger targets. Any learned mapping or calibration uses compatible external training annotations and cross-fitted predictions only. Missing labels are masked, never filled with zero. Keep a zero-contribution power control, since power previously hurt transfer. No target-fitted blend or compulsory tempo, arousal or negative-valence gate.
4. Compare native-label, mapping and audio-prediction failures separately. Retain reciprocal native transfers and fixed anger references. Check high-energy/low-anger music and forceful music with slower estimated tempo; preserve the distinction between a feature estimate and human tempo truth. Release can coexist with other vibes.
5. First audit existing supplied audio and explicit annotations for an unused confirmation population. Expand only to cover a documented missing contrast or precision gap, with a frozen acquisition bound and identity exclusions. Previously inspected Soundtracks remains diagnostic; training on it cannot later be called external confirmation. Record terms for every new Jamendo export.

## Acceptance

- Publish the label crosswalk, fixed mappings, usable counts, candidate freeze, native accuracy, common-reference ranking/top-list results, calibration and paired uncertainty against #577's exact controls and the new Jamendo candidate.
- Explain whether the observed gain survives source balancing and supported force-versus-energy contrasts; identify whether remaining errors arise from prediction, mapping, or missing evidence.
- Give an explicit adopt/retain recommendation under predeclared meaningful-gain and regression limits. A positive point estimate alone is insufficient; a supported retain decision completes the work.
- Replay and resource-check any proposed export. Work stays under `experiments/` through `./experiments/run.py`; production, #405/#587 output acceptance and #137 remain separate. No new listening collection or shadow phase.
