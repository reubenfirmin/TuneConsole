# Cruise: provenance

Selected Oct 29, 2026 (#570–573): equal PMEmo/DEAM native VA experts with fixed
moderate-arousal/positive-valence mapping. This is independent Cruise suitability;
the [shared model](shared.md) still supplies classification and intensity.

## Derivation

The common-target source-only comparison retains 762 [PMEmo](../datasets/pmemo.md)
and 1,798 [DEAM](../datasets/deam.md) rows after connected destination exclusions.
It selects PMEmo ridge and DEAM random-feature ridge on source annotations.
Predict and clip native attributes per excerpt, average native attributes over
available excerpts, then map each source to Cruise and average the two sources.
Do not substitute averaging mapped excerpts: the moderate-arousal map is nonlinear.

Artifact: [cruise-vibes.json](../../../src/yt_playlist/rec/data/cruise-vibes.json),
187,217 bytes, losslessly compressed coefficients, SHA256
`3927a3294ad7a1670ef7f81d5c6119943991a699de60a7d5a60022851ba304eb`.
Source: `local/mapped-vibes-20261029/common-target-1/`; source plan SHA256
`df733b8cd6014e3077a4ee9e43504105d40dbd1e38676f5c0bc10b8413e5fdae`.
Export/replay: `./experiments/run.py cruise-package export --directory NEW_DIRECTORY`.
Retained package evidence: `local/mapped-vibes-20261029/cruise-package-3/`.

## Experiments and decisions

| Date / comparison | Outcome and reason |
| --- | --- |
| Oct 29, common targets | Equal VA NDCG .8358 natural ManyMusic, .8943 generated, .8646 Memo. Emotify was below random on both ManyMusic strata, despite stronger Memo transfer. Advance VA; no universal source winner. |
| Oct 29, shared refresh | On matched cohorts, Cruise gain over shared was +.1689 [.0865,.2274] natural, +.1921 [.0953,.2753] generated, +.0491 [-.0444,.1439] Memo. Refreshed shared models did not remove the primary advantage. |
| Oct 29, score contract | Within-Cruise ranking and strong-match calibration are separate. No global strong-match threshold was established; do not reuse the shared .65 threshold. |
| Oct 29, preprocessing | Advantage survived browser preparation on primary cohorts; supplementary Soundtracks weakness remained. |
| Nov 1, #707/#708/#809/#810 | All-window VA training, final/intermediate MERT and chroma failed advancement requirements. Retain the recipe; see [encoders](encoders.md). |
| Nov 1, #710/#812 | Reserved Memo comparison inconclusive; classical WCMED adds rank association with reconstruction/proxy limits. Neither selects a replacement. |

Intervals condition on reused corpora and fitted predictions. Absolute errors
and mean scores show the independent output remains uncalibrated.
Packaging replay covers 346 recordings / 1,227 excerpts, including separate
source errors (maximum 2.22e-16), and 93 real playback groups. It establishes
coefficient/routing parity. Whole-account startup and rendered consumers require
their own evidence; the old schema-102 cold observation is not current acceptance.

Protocols: [common targets](../CANDIDATE_FOLLOWUP_PLAN.md),
[score semantics](../VIBE_SCORE_CONTRACT.md).
Verify: `candidates verify`, `transfer-repair verify`, `cruise-baselines verify`
through `./experiments/run.py`. Initial direct adoption had no staged-promotion
requirement; runtime off/shadow/active modes remain available.
