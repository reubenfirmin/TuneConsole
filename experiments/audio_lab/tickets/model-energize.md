Parent: #530. Priority: **P2**. **In review**. Owns the dedicated Energize experiment; follow #530's shared protocol and reuse #570/#571/#572 and #424.

## Completed author comparison

The 30 October bounded comparison is complete with a **retain** recommendation.
Evidence: `experiments/audio_lab/INDIVIDUAL_EXPERIMENT_RESULTS.md`, frozen
`local/individual-20261030/remaining-1/`, plan SHA256
`c7c69cb680a7c6a7919b35984356b295521a53271d66cf48270677af230829ae`.
The selected PMEmo head improved generated-music NDCG from 0.9187 to 0.9647,
but lost on natural music (0.7979 to 0.7646), Memo (0.9160 to 0.8992) and
Soundtracks (0.8489 to 0.8184). The equal VA ensemble did not resolve transfer.
These are retained author artifacts; independent acceptance remains pending.

The separate label-mapping follow-up #616 is also complete with a retain
recommendation. Its evidence is in `ENERGIZE_CONSENSUS_RESULTS.md` and
`ENERGIZE_LABEL_AUDIT.md`. It adds activation/consensus/exercise-map diagnostics
without replacing this ticket's original evidence or adding acceptance criteria.

## Definition and starting evidence

Use activation/exercise supervision. PMEmo/DEAM arousal, Emotify power/joyful activation and Soundtracks human energy are distinct native tasks. CAL500 Exercising is separate direct activity evidence; party votes/tag-derived quadrants are auxiliary proxies.

Cruise's VA ensemble is not an automatic Energize replacement: historical natural ManyMusic NDCG 0.7575 versus product 0.7960, Memo2496 0.8701 versus 0.9160. Source-selected acoustic PMEmo also transfers poorly to Soundtracks. Browser shared controls on natural/generated ManyMusic, Memo, Soundtracks and CAL500: 0.7710/0.8963/0.9187/0.8488/0.3043.

## Work

1. Freeze a bounded set of embedding, acoustic and combined heads, selected single sources and a predeclared equal VA ensemble. Do not choose a new embedding winner from inspected target results or assume acoustic-only activation transfers best.
2. Evaluate on identical natural/generated ManyMusic, Memo2496 and Soundtracks rows; add grouped native OOF and reciprocal PMEmo/DEAM transfers. Remove complete known identity components before selection and include frozen/refreshed product, constants and browser controls.
3. Report ranking, top-list lift, MSE/source-mean controls, calibration drift and paired uncertainty by domain. Keep generated music and previously inspected exposure explicit.
4. Check supported activation-versus-aggression and one/three-excerpt or changing-arrangement cases using available labels/captures. Shared radio intensity remains a separate contract; its production consumer acceptance belongs to #573.

Deliver a pinned comparison, explicit source/model adopt-or-retain decision, replay-verified artifact if supported, and calibration/bounded inference/resource evidence. No gain means retain the incumbent. Research stays under `experiments/` through `./experiments/run.py`; #587 owns complete-output evaluation. No user listening packet or shadow rollout is required.
