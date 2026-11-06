# Focus: provenance

Selected Oct 30, 2026 (#641): the exact #615 direct Reading/Studying export,
after representation/task/listener follow-ups. Later explicit adoption does
not alter earlier frozen **retain** decisions. The chroma follow-up also retains
this incumbent; no harmonic-feature replacement is selected.

## Derivation

Two alpha-1000 ridge heads use 494 [CAL500](../datasets/cal500.md) songs and
1,677 original listener responses. Existing 200-value MusiCNN embeddings predict
Reading and Studying fractions. Clip native outputs per excerpt, average
available excerpts, then take the equal mean of the two activities.
Activity association is not measured cognitive performance or personal benefit.

Artifact: [focus-vibes.json](../../../src/yt_playlist/rec/data/focus-vibes.json),
36,994 bytes, SHA256
`695f07272d12d218cefa15aca7ec3ee5105592d7ca428248c99c2a5c6f1ca0a8`.
Source `local/focus-615-20261030/round-1/exports/direct.json`, SHA256
`39f6741b989bd1d665d4414506230d9b7b936285fbb52bf78b24af30e75da098`;
plan `5af390b1c1173e4d2978f6ca34835b955a4d81a6206c71965fb855f2d1545486`.
The adjacent notice preserves CAL500 attribution/terms. No refitting at export.

## Experiments and decisions

| Date / comparison | Outcome and reason |
| --- | --- |
| Oct 29–30, mapped round/#576 | Broad feature/model searches did not reliably improve prior direct activity prediction. #576 browser NDCG .4767 versus prior .4453: +.0314 [-.0437,.1558]; lower native error did not establish ranking gain. |
| Oct 30, #615 | Direct browser NDCG .498598 versus shared .399852: +.098747 [.004644,.204600], while lift gain was uncertain. Attribute consensus .4358 and hybrid .4939 did not improve direct/refitted #576. None beat the specified prior by the frozen margin; retain under that protocol. |
| Oct 30, #637/#638 | Residual representation retained useful signal outside linear emotion directions; adding residuals gave uncertain ranking gain. PCA point maxima were not selected. Separate task slopes did not improve combined ranking. User subsequently chose exact #615 direct export. |
| Nov 1, #711 | Historical/browser ridge NDCG .49792/.49860 versus binomial .46068/.48120 and beta-binomial .44771/.45485. Likelihood improved but ranking/lift fell; discard count heads. |
| Nov 1, #810 | Combined chroma on 32 reused songs appeared strong: NDCG .81063 versus matched control .22167. The control was a small nested fit, not an independent test of shipped heads. Advance only to a larger confirmation study. |
| Nov 1, #815 | Protected full-recipe control reproduced accepted #615 predictions. On common450, combined NDCG .514290 versus .497920; gain +.016371 [-.038792,.065672], lift -.007833, MSE +.000123. Frozen gain/uncertainty gates failed. Discard this chroma recipe; no browser port justified. |

The all494 and outside-pilot chroma diagnostics did not recover a reliable gain.
Independent activity confirmation remains missing: text matches, broader
intellectual activity, episode outcomes and session focus are not interchangeable
recording/window labels. See [activity data](../datasets/moods-and-activities.md)
and the [confirmation-source audit](../datasets/leads.md).

Evidence roots: `local/focus-615-20261030/round-1/`,
`local/focus-controls-637-638-20261030/round-1/`,
`local/focus-production-641-20261030/`,
`local/focus-counts-711-20261101/round-1/`,
`local/chroma-810-20261101/round-1/`,
`local/focus-confirmation-815-20261101/round-1/`.
Protocols: [direct mapping](../FOCUS_MAPPING_PLAN.md),
[controls](../FOCUS_CONTROLS_PLAN.md), [counts](../FOCUS_COUNTS_PLAN.md),
[confirmation](../FOCUS_CONFIRMATION_PLAN.md).
Verify through `./experiments/run.py focus-mapping verify`, `focus-counts verify`,
`focus-confirmation verify`; package/runtime checks use `focus-package`,
`focus-production`, `focus-wheel`. The export replay covers 494 external inputs
in both preparations and 159 real-capture cases; it establishes numerical parity.
