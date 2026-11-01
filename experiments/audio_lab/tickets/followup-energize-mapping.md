Parent: #530. Follows #578. Priority: **P2**. **In review**. Use #530's shared research and label-mapping protocol. Owns the next Energize mapping investigation only.

The user authorized a range of Energize experiments on 30 October. Frozen plan:
`experiments/audio_lab/ENERGIZE_CONSENSUS_PLAN.md`, with activation-only,
fixed source consensus and an exercise-anchored mapping, plus fixed power/joy
ablations, grouped and omitted-corpus comparisons, original-label diagnostics,
browser controls and temporal checks. Plan SHA256:
`0500878ae3cc15b57cb840b3e5c4f0723029c026da7938ef53d0ae523cb35d42`.
The refreshed local snapshot has 84 usable songs and 40 Energize positives,
with no new identities or changed labels since #570. All fitting and evaluation
remain under `experiments/`. This research left production unchanged; the later
authorized integration is tracked separately under #631.

## Completed author investigation

The user subsequently selected the **no-joy consensus** for production under
#631, favoring broader labeled-source coverage and accepting the reported
tradeoffs. `experiments/audio_lab/ENERGIZE_PRODUCTIONIZATION.md` records the
exact export and integration. The original research recommendation below remains
intact; the product decision does not change its frozen evaluation result.

**Retain the current production Energize scorer.** Three predeclared procedures
and five component-removal controls were evaluated using five grouped outer
fits, four omitted-corpus fits and one full research fit. The 4,276 native-source
rows yield 1,227 common primary comparison rows across the five references.

Current browser-preparation NDCG, on identical rows:

| Reference | Shared production | Activation | Fixed consensus | Exercise mapping |
| --- | ---: | ---: | ---: | ---: |
| Soundtracks energy | 0.84344 | 0.82019 | 0.87594 | 0.85903 |
| Natural ManyMusic arousal | 0.79242 | 0.79216 | 0.77513 | 0.76498 |
| Generated ManyMusic arousal | 0.90020 | 0.90568 | 0.90373 | 0.90390 |
| Memo arousal | 0.93122 | 0.91199 | 0.90587 | 0.91089 |
| CAL500 Exercising | 0.25251 | 0.28759 | 0.33073 | 0.30710 |

All main procedures fail the frozen improvement/nonregression limits. Consensus
helps Soundtracks, with a positive browser ranking interval, but loses natural
music and Memo. Exercise mapping does not repair the losses. Removing joy is a
useful diagnostic lead, with historical natural/exercise and top-list losses
still unresolved. It is not a validated production replacement.

The original-label audit and observed-versus-predicted attribute diagnostic
show useful exercise-mapping signal, sparse and inconsistent exercise votes,
and loss of information in audio predictions. They do not establish one unique
cause or an accuracy ceiling. All populations remain previously inspected
development evidence; no new local identities or labels appeared in the refresh.

Evidence: `experiments/audio_lab/ENERGIZE_CONSENSUS_RESULTS.md`,
`ENERGIZE_LABEL_AUDIT.md`, `ENERGIZE_CONSENSUS_PLAN.md` and frozen
`local/energize-616-20261030/round-1/`. Receipt verification passed; six focused
checks passed, including independent coefficient/metric replay and cross-stage
identity isolation. Research export/resource checks passed. Independent ticket
acceptance remains pending; the retain decision requires no production export
or new listening phase. Detailed commands, runtime, hashes and limitations are
in the results report and handoff comment.

## Question and evidence

Which combination of activation, energy, power and direct exercise suitability represents Energize across ordinary music, rather than only matching one corpus's arousal scale?

The 30 October PMEmo head improved generated-music NDCG (0.9187 to 0.9647) but declined on natural music (0.7979 to 0.7646), Memo (0.9160 to 0.8992) and Soundtracks energy (0.8489 to 0.8184). The equal VA ensemble did not fix transfer. Direct CAL500 Exercising supervision provided useful signal, but did not establish a general replacement.

Evidence: `experiments/audio_lab/INDIVIDUAL_EXPERIMENT_RESULTS.md`, frozen `local/individual-20261030/remaining-1/`, plan SHA256 `c7c69cb680a7c6a7919b35984356b295521a53271d66cf48270677af230829ae`. These are retained workspace artifacts; independent acceptance remains pending.

## Strategy

1. Inventory human arousal, energy, power, joyful activation and Exercising annotations, including source scales, co-annotation, agreement and excerpt support. Keep Exercising as direct activity evidence and party/tag-derived labels as proxies. High arousal does not automatically mean useful exercise music.
2. On supported co-annotated recordings, test how observed native attributes explain held-out exercise suitability, independently of audio-prediction error. Retain separate fixed arousal and energy references for transfer; do not redefine them to favor an activity mapping.
3. Freeze at most three mapping procedures: activation alone; activation with separately represented power/joy; and a small activity-anchored combination trained on original CAL500 Exercising votes. Use cross-fitted native predictions, grouped source-only selection and source-specific scale calibration. Missing annotations stay masked. Include each added signal's zero-weight control. Positive valence is not required, and aggression is not an automatic substitute for activation.
4. Determine whether source-balanced native training reduces the existing natural/generated discrepancy without using target labels or corpus identity as a scoring shortcut. Keep native target losses and activity evidence distinguishable; a good exercise result cannot erase failures on the other declared references.
5. Evaluate supported energetic-cheerful, energetic-unhappy and forceful-but-slower cases, plus first/three/all-excerpt behavior using Memo's aligned human trajectories. Whole-window labels do not establish chunk-level truth. Acquire more supplied audio only for an identified coverage or precision gap.

## Acceptance

- Publish the label crosswalk and observed-label mapping diagnostic, then native and mapped prediction results on identical supported rows for natural/generated ManyMusic, Memo, Soundtracks and CAL500 separately.
- Include #578's frozen/refreshed/browser and named prior controls, the latest direct activity head, ranking/top-list metrics, error/calibration and paired uncertainty. Predeclare meaningful improvements and tolerated regressions.
- Identify whether the main failure is native prediction, the activation-to-activity mapping, or domain coverage; give an explicit adopt/retain recommendation with failures preserved.
- Replay and resource-check any proposed export. All work stays under `experiments/` through `./experiments/run.py`. Shared radio intensity and production/output acceptance retain their existing owners; no new listening collection or shadow phase.
