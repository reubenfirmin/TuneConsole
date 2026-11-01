Parent: #530. Follows #576. Priority: **P2**. **In review**. Use #530's shared research and label-mapping protocol. The bounded investigation is complete; independent acceptance remains pending.

## Question and evidence

Do the native attributes we already have explain Reading/Studying suitability better than the current direct audio heads, and is the remaining limit prediction quality or activity-label coverage?

The 30 October experiment reduced native error, but the browser candidate's NDCG gain over the prior was +0.0314 with interval [-0.0437, +0.1558]; top-list lift gain was +0.0283 [-0.0106, +0.0841]. Reading/Studying remain the direct target, with their equal mean defining Focus. A different quietness, instrumentalness or At-work target cannot solve this experiment by changing its meaning.

Evidence: `experiments/audio_lab/FOCUS_EXPERIMENT_RESULTS.md` and `FOCUS_REFERENCE_RESULTS.md`; frozen `local/individual-20261030/focus-1/`, plan SHA256 `5ea7fc8938c9efc01c4efcf5fd28d8af8a034d0326bcc3666803f0a0c0d842d7`. These are retained workspace artifacts; independent acceptance remains pending.

## Current experiment

The user authorized this round on 30 October. The pre-fit plan is `experiments/audio_lab/FOCUS_MAPPING_PLAN.md`: three procedures (direct, attribute consensus, hybrid), ten original CAL500 attributes and eight external features, with nested global component exclusion at both training stages. The refreshed local inventory has 84 usable songs, including 18 Focus positives. It adds no new identities or labels since #570.

The cross-source identity audit joins five CAL500 components across the old #576 folds. This round uses five component-grouped folds and refits the exact #576 historical/browser procedures on them. Archived scores remain labeled legacy diagnostics. The 450 common comparison recordings and original Reading/Studying fractions remain unchanged. The plan was recorded before fitting.

Frozen round: `experiments/audio_lab/local/focus-615-20261030/round-1/`; plan SHA256 `5af390b1c1173e4d2978f6ca34835b955a4d81a6206c71965fb855f2d1545486`. The original-vote audit found 1,677 responses across 494 songs, with 137 Reading-positive and 163 Studying-positive songs. This does not add new independent labels.

## Completed author evidence

Reports: `experiments/audio_lab/FOCUS_MAPPING_RESULTS.md` and `FOCUS_LABEL_AUDIT.md`. Browser Focus NDCG is 0.4986 direct, 0.4358 attribute consensus, 0.4939 hybrid, 0.5019 refitted #576 browser control and 0.3999 shared. Consensus adds no demonstrated improvement over direct audio; hybrid adds complexity without improvement. All three new procedures retain under the frozen criteria because none exceeds the refitted #576 control by 0.01; attributes also fail regression/uncertainty limits. This does not mean direct and shared production perform equally: direct versus shared is +0.0987 NDCG [0.0046, 0.2046], while its top-list lift gain remains uncertain, +0.0388 [-0.0095, 0.0848]. No Focus production change was made.

Observed-human-attribute Focus NDCG is 0.5125 versus 0.4345 for the same ten audio-predicted attributes. Their difference has interval [-0.0586, 0.1903]; Studying MSE provides a clearer prediction-loss signal. The labels contain useful activity information, but native prediction loss and sparse, weakly agreeing activity responses remain limitations. No new reference rows were verified, and unselected local vibes remain unknown.

`./experiments/run.py focus-mapping verify` and `./experiments/run.py focus verify` pass. `./experiments/run.py check --test test_focus_mapping.py`: **6 passed**, including independent actual-artifact arithmetic/metrics and exclusions at every nested stage. The 84-song/232-capture local replay passes per-head and final first/three/all checks. Candidate exports are 33,454 / 431,503 / 464,354 bytes; maximum warm inference is below 2 ms, peak RSS 653,266,944 bytes. All frozen replay/resource bounds pass. No global test suite or lock was used.

Report SHA256 `8947e06c0c847230fb04ea379302307e72d51a2bad74f1ecf092d6278717d425`; direct export SHA256 `39f6741b989bd1d665d4414506230d9b7b936285fbb52bf78b24af30e75da098`. Full receipts hash all artifacts. Runtime: Python 3.14.8, NumPy 2.4.6, SciPy 1.18.1, threadpoolctl 3.7.0, one numerical thread. These are retained workspace author artifacts, not committed/published evidence or independent acceptance. Reviewer should verify the frozen target, two-stage isolation, unchanged common cohort, controls, diagnostic interpretation and retain decision.

## Selected production candidate: direct Reading/Studying

The exact #615 **direct** export was initially retained as a potential candidate while we brainstormed. After the controlled #637/#638 follow-ups, the user explicitly authorized productionization. Implementation and author verification are recorded in #641 and `experiments/audio_lab/FOCUS_PRODUCTIONIZATION.md`, now In review. This later adoption decision does not revise the frozen research retain decision.

The baseline shared scorer and selected candidate use the same 200-dimensional MusiCNN audio embedding. At the #615 baseline, Focus predicted nine Emotify emotions from a model trained on 400 songs, then mapped them to six relative vibe scores using the original 55-song local calibration set. The selected direct candidate fits two ridge heads to the original Reading/Studying listener fractions on 494 CAL500 songs (1,677 responses). It clips each excerpt's two predictions, averages across excerpts, and takes the equal mean of Reading and Studying. #641 uses this output for Focus suitability, while the shared model continues to supply six-way classification. The specialist estimates activity-label fractions and remains uncalibrated for individual listeners.

On the same 450 evaluation songs, browser NDCG is 0.4986 versus shared 0.3999, with paired difference +0.0987 [0.0046, 0.2046]. Top-list lift remains uncertain. Direct does not improve on the refitted #576 browser control (0.5019); neither consensus nor hybrid establishes added value. Previously inspected data, weak activity-label agreement and whole-song labels paired with short excerpts remain limitations.

Exact research artifact: `experiments/audio_lab/local/focus-615-20261030/round-1/exports/direct.json`; SHA256 `39f6741b989bd1d665d4414506230d9b7b936285fbb52bf78b24af30e75da098`. Two ridge heads, alpha 1000 each; 33,454 bytes, maximum warm three-excerpt research inference 0.197 ms, verified replay. #641 packages these exact heads into a 36,994-byte production artifact, SHA256 `695f07272d12d218cefa15aca7ec3ee5105592d7ca428248c99c2a5c6f1ca0a8`. The plan and complete research provenance remain preserved; independent production acceptance is pending.

## Strategy

1. Inventory original CAL500 Reading/Studying fractions, rater support and disagreement, plus other human attributes actually annotated on the same recordings. Quantify label sparsity and the whole-song-label/short-excerpt mismatch. Preserve Reading and Studying separately before the fixed equal mean.
2. In grouped training folds, test whether non-activity native attributes explain the two activity labels when the attributes themselves are observed human annotations. Exclude Reading/Studying and any derived copies from inputs. This diagnostic estimates the information available in those attributes; it is not a deployable scorer or new confirmation.
3. Compare at most three declared procedures: the existing direct audio heads; Reading/Studying heads using cross-fitted predictions of those native attributes; and a small audio-plus-attribute model. Select all attributes, weights and complexity inside source folds. Keep the original activity target and evaluation rows fixed. Native attribute agreement alone is not Focus success.
4. Inspect supported distraction and vocal/instrumental contrasts using actual annotations. Do not invent distraction labels, require silence or ban vocals. Available local positives remain supplementary, with unselected vibes unknown.
5. Reuse the completed reference audit. Moods and activities has 209 artist/title candidate joins but zero verified recording/excerpt matches; its intellectual activity is broader than Reading/Studying. HelpStudy outcomes cover episodes and cannot automatically label each named track. Only a bounded, documented identity/alignment recovery attempt can make these usable; keep broader constructs separate. Seek additional audio only if the inventory demonstrates a specific remaining gap.

## Acceptance

- Report observed-attribute versus predicted-attribute performance on the unchanged Reading/Studying target, alongside the exact prior, latest research heads, shared/refreshed and browser controls.
- Publish native error, ranking, top-list suitability/lift, paired uncertainty, rater/coverage limits and provenance for any newly verified references.
- Conclude whether native attributes improve the direct activity task, whether audio prediction loses that signal, or whether the evidence is insufficient. Retain is a valid result; corpus reuse cannot become a fresh holdout by changing folds.
- Provide a reproducible mapping and replay/resource evidence for any proposed export. All work stays under `experiments/` through `./experiments/run.py`; no production edits, user listening collection or shadow phase.
