Parent: #530. Follows #580. Priority: **P2**. **In review**. Use #530's shared research and label-mapping protocol. Owns the next Uplift mapping investigation only.

The user authorized work on 30 October and explicitly requested consideration
of a consensus model. The bounded protocol is
`experiments/audio_lab/UPLIFT_CONSENSUS_PLAN.md`: a valence-only anchor, a fixed
source-native consensus, and a learned corpus-balanced consensus. This makes
the proposed source-balanced procedure concrete while retaining the original
references and semantic diagnostics.

**Subsequent user adoption decision:** the user selected the fixed consensus
after reviewing its improvement over the production baseline on three domains
and accepting the generated-music regression. Exact production integration is
owned by #630. The frozen research retain result below remains intact; this is
an explicit product tradeoff, not a retroactive pass under the original rule.

## Completed research handoff, 30 October 2026

**Retain.** Both fixed and learned consensus exceed the frozen ManyMusic
regression limits, despite aggregate gains and improvements on Soundtracks
and Memo. The bounded experiment is complete; independent acceptance remains
pending. The separate adoption decision is implemented under #630.

- Report: `experiments/audio_lab/UPLIFT_CONSENSUS_RESULTS.md`.
- Label crosswalk, rater support and coverage: `experiments/audio_lab/UPLIFT_LABEL_AUDIT.md`.
- Frozen evidence: `experiments/audio_lab/local/uplift-618-20261030/round-1/`.
- Plan SHA256: `2bf73a9f8f1360f50173f2cb3311dd1f31f9584a0d51054749704e5f28bb5b66`.
- Evaluation receipt SHA256: `2e05a7d63d4d1e4aab1e116aa5d5d52d84dd973a361ee913c7c042d40e25fb8b`.

Historical grouped NDCG on identical rows:

| Domain | Shared | Refitted DEAM | Fixed consensus | Learned consensus |
| --- | ---: | ---: | ---: | ---: |
| Soundtracks, 470 | 0.64317 | 0.56594 | 0.70807 | 0.68375 |
| ManyMusic natural, 171 | 0.70656 | 0.81690 | 0.76473 | 0.77984 |
| ManyMusic generated, 95 | 0.91163 | 0.90528 | 0.87718 | 0.89985 |
| Memo, 63 | 0.91971 | 0.88419 | 0.97042 | 0.92403 |

These joint component folds differ from #580; unchanged control predictions
therefore have different fold/top-list summaries. All sources were previously
inspected and now enter development. Separate fits omit Soundtracks, both
ManyMusic strata together, or Memo, including linked rows in other corpora.
Both blends still fail ManyMusic transfer limits. Natural/generated populations
remain separate; local data is supplementary only (84 usable songs, 23 Uplift
positives, no identities or labels changed since #570).

The observed-versus-predicted attribute diagnostic identifies both a native
prediction/domain problem and an incomplete valence-to-happiness bridge; it
does not establish a unique causal explanation. Soundtracks has only one
gentle-happy and no positive-forceful example at the frozen thresholds.

`./experiments/run.py uplift verify` passed.
`./experiments/run.py check --test test_uplift_consensus.py --test test_uplift_replay.py`
passed **8 checks**, including independent held-out coefficient/blend replay.
The complete research export is 148,730 bytes; maximum warm three-excerpt
inference 0.778 ms, peak RSS 653,266,944 bytes, exact local replay. Experiments
did not use the global-suite lock. No full suite, production change, commit
or push was performed for this investigation.

## Question and evidence

Why does a strong valence predictor transfer to some music populations but fail happiness/valence references elsewhere, and can the existing joy/happiness labels resolve that mismatch?

The 30 October DEAM candidate improved natural-music NDCG from 0.7084 to 0.8191, with gain interval [0.0316, 0.1610], and improved Memo. It declined on generated music and regressed on Soundtracks from 0.7318 to 0.5533, with gain interval [-0.2683, -0.0361]. Equal VA averaging did not resolve the problem. The next step is to explain and address this domain failure, not select only the favorable population.

Evidence: `experiments/audio_lab/INDIVIDUAL_EXPERIMENT_RESULTS.md`, frozen `local/individual-20261030/remaining-1/`, plan SHA256 `c7c69cb680a7c6a7919b35984356b295521a53271d66cf48270677af230829ae`. These are retained workspace artifacts; independent acceptance remains pending.

## Strategy

1. Inventory native PMEmo/DEAM valence, Emotify joyful activation, Soundtracks happiness/valence and Jamendo happy votes. Record scales, disagreement, annotation instructions and excerpt coverage. Joyful activation includes activation; it is not interchangeable with gentle positive feeling.
2. Use existing co-annotated excerpts to test relationships among observed valence, happiness, joy and arousal against a fixed independent reference. Exclude reference-defining columns from inputs. Compare with cross-fitted predictions to identify whether the Soundtracks failure comes from native prediction, calibration or the semantic mapping. If paired labels cannot identify the cause, retain that uncertainty.
3. Freeze at most three procedures: the existing valence-only candidate; a small valence-plus-happiness/joy mapping; and source-balanced native training with separate valence and emotion targets. Fit scale conversions, weights and source balancing inside external training folds only. Missing labels remain masked and added signals have zero-weight controls. Do not gate Uplift on high energy or use dataset identity at inference.
4. Keep natural music, generated music, Memo and Soundtracks distinct in evaluation. If Soundtracks annotations become training data, designate them as development and use properly excluded source holdouts/other populations for transfer; do not present the previously inspected set as new confirmation.
5. Test gentle positive music, energetic negative-valence music and positive forceful music where annotations support them. Report sparse/empty slices: the previous frozen Soundtracks thresholds yielded only one gentle-happy excerpt. Seek additional supplied audio only to close a defined coverage or precision gap.

## Acceptance

- Publish the label crosswalk and mapping/prediction diagnosis of the domain failure, with fixed external reference definitions and retained negative results.
- Compare all declared procedures with #580's frozen/refreshed/browser controls, named prior PMEmo head and latest DEAM candidate on identical supported rows. Report native accuracy, ranking/top-list lift, calibration and paired uncertainty by domain.
- Give an explicit adopt/retain recommendation using predeclared meaningful-gain/regression limits. A favorable aggregate must not conceal the Soundtracks failure; Uplift must remain distinct from Energize. These are suitability estimates, not proof of a causal mood improvement.
- Replay/resource-check any proposed export. All work stays under `experiments/` through `./experiments/run.py`; production/output acceptance remains separate. No new listening collection or shadow phase.
